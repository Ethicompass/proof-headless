"""Launching `lens-run` by hand, the way lens-api's docker dispatcher would.

The fallback for when lens-api's own relaunch cannot start the job, which on the last two
engagements meant `docker run ... lens-run run <project> <run>` typed by hand. The container is
named, labelled and networked as lens-api names, labels and networks it (Lens
`lens_dispatch.docker`), so the daemon's name uniqueness refuses a second job for one run whoever
launches it, and Lens's status reads this container as its own. The network is
`LENS_DOCKER_NETWORK` or none (docker's default bridge), and no host entry is added, exactly as
the dispatcher creates it: a job this launch starts runs where lens-api's own would, so a hand
launch that works says the dispatcher's would have too, and one that does not fails for the same
reason. `--network` overrides it deliberately.

A dead container under the run's name is what lens-api's next launch reads its exit and last lines
from, then records as the dead attempt's failure (Lens ADR 0027) before it removes it. This
launcher cannot write Lens's store, so it keeps the same record, redacted, in the client's state
directory before removing the container, where the operator and `proof evidence` find it.

The job's environment is lens-api's: every `LENS_` setting in the running lens-api container
except the ones only the API reads (Lens `deps.API_ONLY`), and, under the `env` secrets backend,
the variables those settings' `*_ref` names point at. lens-api forwards its resolved settings, and
a setting absent from its environment resolves to the default the job resolves too, so the two
environments configure the same job. The values travel through a file readable only by its owner,
handed to `docker run --env-file` and deleted afterwards; they are never printed, logged or kept,
and any that a docker error echoes is redacted before the error is shown.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from proof_headless.docker import Runner

API_SERVICE = "lens-api"
"""The compose service whose container holds the environment to copy."""

API_ONLY = frozenset(
    {
        "LENS_DISPATCH_BACKEND",
        "LENS_RUN_IMAGE",
        "LENS_DOCKER_NETWORK",
        "LENS_DOCKER_HOST",
        "LENS_RUN_STALE_AFTER_S",
        "LENS_PUBLIC_BASE_URL",
        "LENS_DEFAULT_RULES_REF",
        "LENS_DEFAULT_CONTRACT_REF",
        "LENS_DEFAULT_JUDGE_CALIBRATION_REF",
    }
)
"""The settings lens-api keeps back from the job it launches (Lens `deps.API_ONLY`)."""

_ALIVE = frozenset({"running", "restarting", "paused"})
CREATE_TO_START_S = 60.0
"""How long a `created` container that never started counts as a launch in flight, as Lens's
dispatcher bounds it (`lens_dispatch.docker.CREATE_TO_START_S`); older, it is a launch that died
between its create and its start."""
REDACT_MIN_LENGTH = 8
"""The shortest value redacted from a kept log tail, the floor `proof evidence`'s scan uses."""
LOG_TAIL = 50
"""The lines of a dead container kept with its record, as Lens keeps them (`LOG_TAIL`)."""


class LaunchRefused(RuntimeError):
    """The manual launch cannot proceed; the message names what is missing, never a value."""


def container_name(project_id: str, run_id: str) -> str:
    """`lens-run-<run_id>-<8 hex of the project id>`, the name lens-api's dispatcher gives the
    run's container."""
    return f"lens-run-{run_id}-{hashlib.sha256(project_id.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class ApiEnvironment:
    """What a launch needs from the running lens-api container. `env` holds values: it is never
    printed, and its repr says only how many there are."""

    container: str
    env: Mapping[str, str]
    image: str | None
    network: str | None

    def __repr__(self) -> str:
        return (
            f"ApiEnvironment(container={self.container!r}, {len(self.env)} variables, "
            f"image={self.image!r}, network={self.network!r})"
        )


def _call(
    runner: Runner, args: list[str], what: str, secrets: Mapping[str, str] | None = None
) -> str:
    result = runner(args)
    if result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip()[-500:], (secrets or {}).values())
        raise LaunchRefused(f"{what}: `{' '.join(args[:3])}` exited {result.returncode}: {detail}")
    return result.stdout


def redact(text: str, values: Iterable[str]) -> str:
    """`text` with every non-empty value in `values` replaced by `***`, longest first so a value
    that contains another is not left half shown."""
    for value in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def api_container(runner: Runner) -> str:
    """The one running container of the `lens-api` compose service, refused when there is none or
    more than one."""
    output = _call(
        runner,
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={API_SERVICE}",
            "--format",
            "{{.Names}}",
        ],
        "finding lens-api",
    )
    names = [line.strip() for line in output.splitlines() if line.strip()]
    if len(names) != 1:
        found = ", ".join(names) or "none"
        raise LaunchRefused(
            f"expected one running {API_SERVICE} container, found {found}; name it with "
            f"--api-container"
        )
    return names[0]


def forwarded(env: Mapping[str, str]) -> dict[str, str]:
    """The part of lens-api's environment its job receives."""
    chosen = {
        name: value
        for name, value in env.items()
        if name.startswith("LENS_") and name not in API_ONLY
    }
    if env.get("LENS_SECRETS_BACKEND", "env") == "env":
        for name, ref in list(chosen.items()):
            if name.endswith("_REF") and ref and ref in env:
                chosen.setdefault(ref, env[ref])
    return chosen


def read_api(runner: Runner, container: str) -> ApiEnvironment:
    """lens-api's environment, its run image and its job network, read with `docker inspect`.

    The network is `LENS_DOCKER_NETWORK`, the one its dispatcher creates jobs on, or none.
    """
    output = _call(runner, ["docker", "inspect", container], f"inspecting {container}")
    try:
        documents = json.loads(output)
        config = documents[0]["Config"]
    except (json.JSONDecodeError, LookupError, TypeError, AttributeError):
        raise LaunchRefused(f"`docker inspect {container}` did not answer a container") from None
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        name, separator, value = str(item).partition("=")
        if separator:
            env[name] = value
    network = env.get("LENS_DOCKER_NETWORK") or None
    return ApiEnvironment(container, forwarded(env), env.get("LENS_RUN_IMAGE") or None, network)


@contextlib.contextmanager
def env_file(env: Mapping[str, str]) -> Iterator[Path]:
    """A `--env-file` holding `env`, mode 0600 from its creation, deleted on the way out.

    Docker's env file has no quoting, so a value with a line break cannot be written; it is
    refused by name.
    """
    for name, value in env.items():
        if "\n" in value or "\r" in value:
            raise LaunchRefused(f"{name} holds a line break, which --env-file cannot carry")
    descriptor, raw = tempfile.mkstemp(prefix="proof-lens-run-", suffix=".env")
    path = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.writelines(f"{name}={value}\n" for name, value in env.items())
        yield path
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Launched:
    """What the manual launch did: `launched`, or `already_running` when a live container already
    holds the run's name. `replaced` is the exit code of a dead container it removed first, and
    `record` where that container's kill record was kept."""

    launch: str
    container: str
    replaced: int | None = None
    record: Path | None = None


@dataclass(frozen=True)
class Held:
    """The container holding the run's name, as `docker inspect` describes its state."""

    status: str
    exit_code: int | None
    oom_killed: bool
    finished_at: str | None
    created: float
    """When it was created, as a timestamp; 0 when the daemon's answer does not read, which makes
    a never-started container stale and removable rather than a name held forever."""

    def alive(self, now: float) -> bool:
        """A job is, or may still be, behind it."""
        if self.status in _ALIVE:
            return True
        return self.status == "created" and now - self.created < CREATE_TO_START_S

    @property
    def how(self) -> str:
        if self.status == "created":
            return "created and never started"
        if self.oom_killed:
            return f"OOM-killed (exit {self.exit_code})"
        return f"exited {self.exit_code}"


def _moment(value: str) -> float:
    """Docker's RFC 3339 time, nanoseconds and all, as a timestamp; 0 when it does not read."""
    text = value.strip().replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = "".join(ch for ch in rest if ch.isdigit())
        text = f"{head}.{digits[:6]}{rest[len(digits) :]}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def container_state(runner: Runner, name: str) -> Held | None:
    """The state of the container named `name`, or `None` when there is none. Only `.State` and
    `.Created` are asked for: the container's configuration holds the job's environment."""
    result = runner(["docker", "inspect", "--format", "{{json .State}}\t{{.Created}}", name])
    if result.returncode != 0:
        return None
    raw_state, _, created = result.stdout.strip().partition("\t")
    try:
        state = json.loads(raw_state)
    except json.JSONDecodeError:
        state = {}
    code = state.get("ExitCode")
    return Held(
        status=str(state.get("Status", "")),
        exit_code=int(code) if code is not None else None,
        oom_killed=bool(state.get("OOMKilled")),
        finished_at=state.get("FinishedAt") or None,
        created=_moment(created),
    )


def keep_kill(
    runner: Runner,
    api: ApiEnvironment,
    held: Held,
    name: str,
    project_id: str,
    run_id: str,
    directory: Path,
) -> Path:
    """Write the dead container's record, as Lens's `record_kill` writes it, under `directory`.

    The log tail is redacted of every value of the job's environment at least
    `REDACT_MIN_LENGTH` characters long before it is written, and the file is readable only by its
    owner: the job is not supposed to log a secret, and this is not where to find out it did. The
    floor is the one the evidence scan uses: most of lens-api's settings are short, ordinary words
    (`s3`, `env`, `lens`, `false`), and redacting those would turn every path and URL in the
    traceback into `***`, which is the diagnosis the record exists to keep.
    """
    logs = runner(["docker", "logs", "--tail", str(LOG_TAIL), name])
    tail = (
        redact(
            (logs.stdout or "") + (logs.stderr or ""),
            (value for value in api.env.values() if len(value) >= REDACT_MIN_LENGTH),
        )
        if logs.returncode == 0
        else None
    )
    record = {
        "project_id": project_id,
        "run_id": run_id,
        "container": name,
        "error": f"lens-run killed by the runtime: {held.how}",
        "exit_code": held.exit_code,
        "oom_killed": held.oom_killed,
        "finished_at": held.finished_at,
        "log_tail": tail,
        "recorded_by": "proof lens run, before removing the container",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{name}-{stamp}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")
    return path


def launch(
    runner: Runner,
    api: ApiEnvironment,
    project_id: str,
    run_id: str,
    *,
    image: str | None = None,
    network: str | None = None,
    kills: Path | None = None,
    now: Callable[[], float] = time.time,
) -> Launched:
    """Start the run's `lens-run` container, detached, unless a live one already holds its name.

    A dead container under the name is the last attempt, kept by Lens for its diagnosis (ADR 0027);
    its record is kept under `kills` first, then it is removed so the name is free. Without
    `kills` a dead holder is refused rather than removed, since its record would be lost.
    """
    chosen_image = image or api.image
    if not chosen_image:
        raise LaunchRefused(
            f"{api.container} sets no LENS_RUN_IMAGE; name the lens-run image with --image"
        )
    name = container_name(project_id, run_id)
    replaced: int | None = None
    record: Path | None = None
    held = container_state(runner, name)
    if held is not None:
        if held.alive(now()):
            return Launched("already_running", name)
        if kills is None:
            raise LaunchRefused(
                f"{name} is dead ({held.how}) and there is nowhere to keep its record"
            )
        record = keep_kill(runner, api, held, name, project_id, run_id, kills)
        _call(runner, ["docker", "rm", "--force", name], f"removing the dead {name}")
        replaced = held.exit_code
    args = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"lens.project_id={project_id}",
        "--label",
        f"lens.run_id={run_id}",
    ]
    chosen_network = network or api.network
    if chosen_network:
        args += ["--network", chosen_network]
    with env_file(api.env) as path:
        args += ["--env-file", str(path), chosen_image, "run", project_id, run_id]
        result = runner(args)
    if result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip()[-500:], api.env.values())
        if "is already in use" in detail:
            return Launched("already_running", name, replaced, record)
        raise LaunchRefused(f"launching {name}: `docker run` exited {result.returncode}: {detail}")
    return Launched("launched", name, replaced, record)
