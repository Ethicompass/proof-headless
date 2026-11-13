"""`docker compose`, as the appliance drives it: through one seam a test can replace.

Every call goes through a `Runner`, a function from arguments to a completed process, so the
order of operations (one model at a time, wait for health, then the next) is tested without a
daemon. The real runner is `subprocess.run` with output captured, never a shell.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proof_headless import paths

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ONE_OFF_LABEL = "com.docker.compose.oneoff"


def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, capturing its output."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def docker(runner: Runner, *args: str) -> str:
    """One `docker` command outside compose, raising `ComposeFailed` on a non-zero exit."""
    full = ["docker", *args]
    result = runner(full)
    if result.returncode != 0:
        raise ComposeFailed(full, result)
    return result.stdout


def ensure_network(runner: Runner, name: str) -> bool:
    """Create the network `name` unless it exists, and say whether it was created.

    `proof-models` is shared by the models' project and the stack's, and either may come up
    first. Compose refuses a network another project (or `docker network create`) made unless it
    is declared `external`, so both projects declare it external and whichever starts first
    creates it here. Neither `down` removes it, which is right: the other project may still be
    using it.
    """
    try:
        docker(runner, "network", "inspect", name)
    except ComposeFailed:
        docker(runner, "network", "create", name)
        return True
    return False


def _published(publishers: object) -> tuple[str, ...]:
    """`docker compose ps` publishers as `host:port->container/protocol`, published ones only."""
    ports: list[str] = []
    for entry in publishers if isinstance(publishers, list) else []:
        if not isinstance(entry, dict) or not entry.get("PublishedPort"):
            continue
        host = f"{entry.get('URL') or '0.0.0.0'}:{entry['PublishedPort']}"
        ports.append(f"{host}->{entry.get('TargetPort')}/{entry.get('Protocol') or 'tcp'}")
    return tuple(dict.fromkeys(ports))


def _one_off(labels: object) -> bool:
    """Whether `ps` labels (a `k=v,k=v` string, or a mapping) mark a one-off `run` container."""
    if isinstance(labels, dict):
        return str(labels.get(ONE_OFF_LABEL, "")).lower() == "true"
    if isinstance(labels, str):
        return f"{ONE_OFF_LABEL}=True" in labels.split(",")
    return False


class ComposeFailed(RuntimeError):
    """`docker compose` exited non-zero."""

    def __init__(self, args: Sequence[str], result: subprocess.CompletedProcess[str]) -> None:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        super().__init__(f"`{' '.join(args)}` exited {result.returncode}: {detail}")


@dataclass(frozen=True)
class ServiceState:
    """One service as `docker compose ps` reports it."""

    service: str
    state: str
    """`running`, `exited`, `restarting`, ..."""
    health: str
    """`healthy`, `starting`, `unhealthy`, or empty when the service has no health check."""
    image: str = ""
    """The image reference the container was created from, as the compose file names it."""
    ports: tuple[str, ...] = ()
    """Published ports, `host:port->container/protocol`."""
    exit_code: int | None = None
    """The exit code of a container that has exited; `None` while it runs."""


@dataclass(frozen=True)
class Restarts:
    """A container's restart count, which means nothing apart from the container it counts."""

    container: str | None
    count: int

    def since(self, baseline: Restarts) -> int:
        """Restarts after `baseline`: all of them when the container was recreated since."""
        if self.container != baseline.container:
            return self.count
        return max(0, self.count - baseline.count)


@dataclass(frozen=True)
class Compose:
    """One compose project on this host."""

    project: str
    file: Path
    env_file: Path | None = None
    runner: Runner = run

    def _base(self) -> list[str]:
        args = ["docker", "compose", "-p", self.project, "-f", str(self.file)]
        if self.env_file is not None and self.env_file.exists():
            args += ["--env-file", str(self.env_file)]
        return args

    def _call(self, *args: str) -> str:
        full = [*self._base(), *args]
        result = self.runner(full)
        if result.returncode != 0:
            raise ComposeFailed(full, result)
        return result.stdout

    def up(self, service: str) -> None:
        """Create and start one service, detached, leaving the others as they are."""
        self._call("up", "--detach", "--no-deps", service)

    def down(self) -> None:
        """Stop and remove every service of the project, including services a newer manifest no
        longer declares, keeping caches and volumes."""
        self._call("down", "--remove-orphans")

    def _rows(self, *services: str) -> list[dict[str, Any]]:
        """`ps --all` rows of the services' own containers, one-off `run` containers left out.

        A `run` container (`proof models pull`) carries its service's name, so without this a
        status read while a download runs could report the downloader in place of the server.
        Compose's `--filter` only knows `status`, so the one-off label is read from each row.
        """
        output = self._call("ps", "--all", "--format", "json", *services)
        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            for row in parsed if isinstance(parsed, list) else [parsed]:
                if not _one_off(row.get("Labels")):
                    rows.append(row)
        return rows

    def ps(self) -> dict[str, ServiceState]:
        """Every service that has a container, by name."""
        states: dict[str, ServiceState] = {}
        for row in self._rows():
            state = str(row.get("State", ""))
            states[row["Service"]] = ServiceState(
                service=row["Service"],
                state=state,
                health=str(row.get("Health", "")),
                image=str(row.get("Image", "")),
                ports=_published(row.get("Publishers")),
                exit_code=int(row.get("ExitCode", 0)) if state in {"exited", "dead"} else None,
            )
        return states

    def up_all(self) -> None:
        """Create and start every service, detached. Compose itself waits on each `depends_on`
        condition (a store healthy, a seed exited 0) before starting what depends on it."""
        self._call("up", "--detach", "--remove-orphans")

    def pull(self) -> None:
        """Pull every service's image, so a slow download is not mistaken for a slow start."""
        self._call("pull", "--quiet")

    def run_once(self, service: str, entrypoint: str, *args: str) -> str:
        """Run `entrypoint args` once in a throwaway container of `service`, with its image,
        environment and volumes, and return what it printed. No ports are published and the
        service's own container, if any, is left alone; `ps` and `restarts` leave the one-off
        container out, so a status read meanwhile still describes the server."""
        return self._call(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", entrypoint, service, *args
        )

    def restarts(self, service: str) -> Restarts:
        """The service's container and how many times its restart policy restarted it.

        The one reliable sign of a crash loop: docker resets its restart backoff after any run of
        ten seconds, so a model that dies minutes into loading is back to `running` almost
        immediately and is seldom caught in `restarting`. The count belongs to one container, so
        it comes with that container's id. No container is no restarts.
        """
        ids = [str(row["ID"]) for row in self._rows(service) if row.get("ID")]
        if not ids:
            return Restarts(container=None, count=0)
        result = self.runner(["docker", "inspect", "--format", "{{.RestartCount}}", ids[0]])
        if result.returncode != 0:
            raise ComposeFailed(["docker", "inspect", ids[0]], result)
        return Restarts(container=ids[0], count=int(result.stdout.strip() or 0))

    def logs(self, service: str, *, tail: int = 200) -> str:
        """The last `tail` lines of one service's logs."""
        return self._call("logs", "--no-color", "--tail", str(tail), service)


def appliance(project: str, file: Path, runner: Runner = run) -> Compose:
    """A compose project of this appliance, with the operator's secrets file as its `--env-file`:
    compose substitutes the `${NAME}` the file references and nothing else reaches a container."""
    return Compose(project=project, file=file, env_file=paths.secrets_file(), runner=runner)
