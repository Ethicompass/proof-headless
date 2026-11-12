"""Launching `lens-run` by hand, the way lens-api's docker dispatcher would.

The fallback for when lens-api's own relaunch cannot start the job, which on the last two
engagements meant `docker run ... lens-run run <project> <run>` typed by hand. The container is
named, labelled and networked as lens-api names, labels and networks it (Lens
`lens_dispatch.docker`), so the daemon's name uniqueness refuses a second job for one run whoever
launches it, and Lens's status reads this container as its own.

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
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
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

HOST_GATEWAY = "host.docker.internal"
_ALIVE = frozenset({"running", "restarting", "paused", "created"})


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

    The network is `LENS_DOCKER_NETWORK` when lens-api sets one, as its dispatcher uses; otherwise
    the first network lens-api itself is on, so the job reaches the store and the models by the
    names lens-api reaches them.
    """
    output = _call(runner, ["docker", "inspect", container], f"inspecting {container}")
    try:
        documents = json.loads(output)
        config = documents[0]["Config"]
        networks = documents[0].get("NetworkSettings", {}).get("Networks") or {}
    except (json.JSONDecodeError, LookupError, TypeError, AttributeError):
        raise LaunchRefused(f"`docker inspect {container}` did not answer a container") from None
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        name, separator, value = str(item).partition("=")
        if separator:
            env[name] = value
    network = env.get("LENS_DOCKER_NETWORK") or next(iter(networks), None)
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
    holds the run's name. `replaced` is the exit code of a dead container it removed first."""

    launch: str
    container: str
    replaced: int | None = None


def container_state(runner: Runner, name: str) -> tuple[str, int] | None:
    """`(status, exit code)` of the container named `name`, or `None` when there is none."""
    result = runner(
        ["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name]
    )
    if result.returncode != 0:
        return None
    status, _, code = result.stdout.strip().partition(" ")
    return status, int(code or 0)


def launch(
    runner: Runner,
    api: ApiEnvironment,
    project_id: str,
    run_id: str,
    *,
    image: str | None = None,
    network: str | None = None,
) -> Launched:
    """Start the run's `lens-run` container, detached, unless a live one already holds its name.

    A dead container under the name is the last attempt, kept by Lens for its diagnosis (ADR 0027);
    it is removed so the name is free, and its exit code is returned so the operator still sees it.
    """
    chosen_image = image or api.image
    if not chosen_image:
        raise LaunchRefused(
            f"{api.container} sets no LENS_RUN_IMAGE; name the lens-run image with --image"
        )
    name = container_name(project_id, run_id)
    replaced: int | None = None
    held = container_state(runner, name)
    if held is not None:
        status, code = held
        if status in _ALIVE:
            return Launched("already_running", name)
        _call(runner, ["docker", "rm", "--force", name], f"removing the dead {name}")
        replaced = code
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
    if any(HOST_GATEWAY in value for value in api.env.values()):
        args += ["--add-host", f"{HOST_GATEWAY}:host-gateway"]
    with env_file(api.env) as path:
        args += ["--env-file", str(path), chosen_image, "run", project_id, run_id]
        result = runner(args)
    if result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip()[-500:], api.env.values())
        if "is already in use" in detail:
            return Launched("already_running", name, replaced)
        raise LaunchRefused(f"launching {name}: `docker run` exited {result.returncode}: {detail}")
    return Launched("launched", name, replaced)
