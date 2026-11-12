"""What the queue and the dashboard look at: run statuses, job containers, their logs, the models.

An `Observation` is a plain snapshot, so the anomaly rules are pure functions over it and a test
builds one by hand. The `Observer` is the one place that asks the outside world: Yield's API for
each run, the docker daemon for Yield's job containers and their logs, and the models' compose
project for their health. Each source can fail on its own (no daemon on a laptop, a models project
that was never started) and the snapshot then says so instead of failing whole: a dashboard with
the runs and no containers is still a dashboard.

Yield's job containers are not compose services. The control plane launches them by name,
`yield-driver-<run id>` and `yield-meas-<run id>`, labels them `yield.role`, and no longer removes
them when they exit, so the last lines of a run that died are still there to read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from proof_headless import docker
from proof_headless.docker import Compose, ComposeFailed, Runner, ServiceState
from proof_headless.runs.client import RunStatus, Unreachable, Yield

DRIVER_PREFIX = "yield-driver-"
MEASURE_PREFIX = "yield-meas-"
GENERATE_PREFIX = "yield-gen-"
ROLES = {DRIVER_PREFIX: "driver", MEASURE_PREFIX: "measure", GENERATE_PREFIX: "generate"}
"""Yield's `JOB_PREFIX` per role: the driver attacks, compute's generation job builds the probes,
the measure scores the dataset."""
LOG_TAIL = 1000
STACK_PROJECT = "proof-stack"
"""The compose project `proof stack` brings up, whose services the observer watches."""


@dataclass(frozen=True)
class LogLine:
    at: datetime | None
    text: str


@dataclass(frozen=True)
class Container:
    """One of Yield's job containers."""

    name: str
    state: str
    """`running`, `exited`, `dead`, `restarting`, ..."""
    exit_code: int | None = None

    @property
    def role(self) -> str:
        for prefix, role in ROLES.items():
            if self.name.startswith(prefix):
                return role
        return "job"

    @property
    def run_id(self) -> str:
        for prefix in ROLES:
            if self.name.startswith(prefix):
                return self.name[len(prefix) :]
        return self.name


@dataclass(frozen=True)
class Service:
    """One container of the stack's compose project: Yield's API and compute, Lens, the stores,
    the registry, and the one-shot jobs that seed them."""

    service: str
    state: str
    health: str = ""
    """`healthy`, `unhealthy`, `starting`, or empty without a health check."""
    exit_code: int | None = None


@dataclass(frozen=True)
class Observation:
    at: datetime
    runs: dict[str, RunStatus | None] = field(default_factory=dict)
    """Each queued run's status, `None` for a run Yield does not know."""
    yield_error: str | None = None
    """Why the run statuses are missing, when Yield did not answer."""
    containers: dict[str, Container] = field(default_factory=dict)
    logs: dict[str, list[LogLine]] = field(default_factory=dict)
    containers_read: bool = False
    """Whether the job containers were listed, so that a run with none has none rather than
    an unknown number."""
    docker_error: str | None = None
    models: dict[str, ServiceState | None] | None = None
    """Each declared model's container, `None` for one that has none. `None` as a whole when no
    models manifest is known."""
    models_error: str | None = None
    stack: dict[str, Service] | None = None
    """The stack's containers by service, `None` when the stack was not asked about."""

    def driver(self, run_id: str) -> str | None:
        """The state of the run's driver container, `absent` when the listing has none, and
        `None` when the containers were not read."""
        if not self.containers_read:
            return None
        found = self.containers.get(f"{DRIVER_PREFIX}{run_id}")
        return found.state if found is not None else "absent"


_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?\s?(.*)$")
_EXITED = re.compile(r"Exited \((-?\d+)\)")
_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
_SERVICE_LABEL = re.compile(r"com\.docker\.compose\.service=([^,]+)")


def parse_logs(output: str) -> list[LogLine]:
    """`docker logs --timestamps` output as lines with their moment.

    Docker stamps in nanoseconds; Python keeps microseconds, so the fraction is cut to six digits.
    A line without a stamp (a test's fake log, a multi-line traceback joined oddly) keeps its text
    and no moment.
    """
    lines = []
    for raw in output.splitlines():
        match = _TIMESTAMP.match(raw)
        if match is None:
            lines.append(LogLine(None, raw))
            continue
        base, fraction, zone, text = match.groups()
        micros = (fraction or ".0")[1:7].ljust(6, "0")
        offset = "+00:00" if zone in (None, "Z") else zone
        lines.append(LogLine(datetime.fromisoformat(f"{base}.{micros}{offset}"), text))
    return lines


def parse_ps(output: str) -> dict[str, Container]:
    """`docker ps --format '{{json .}}'`, one object per line, as containers by name."""
    found: dict[str, Container] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        name = str(row.get("Names", "")).split(",")[0]
        exited = _EXITED.search(str(row.get("Status", "")))
        found[name] = Container(
            name=name,
            state=str(row.get("State", "")),
            exit_code=int(exited.group(1)) if exited else None,
        )
    return found


def parse_services(output: str) -> dict[str, Service]:
    """`docker ps --format '{{json .}}'` of a compose project, as its containers by service."""
    found: dict[str, Service] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        named = _SERVICE_LABEL.search(str(row.get("Labels", "")))
        service = named.group(1) if named else str(row.get("Names", "")).split(",")[0]
        status = str(row.get("Status", ""))
        exited = _EXITED.search(status)
        health = _HEALTH.search(status)
        found[service] = Service(
            service=service,
            state=str(row.get("State", "")),
            health=(health.group(1).replace("health: ", "") if health else ""),
            exit_code=int(exited.group(1)) if exited else None,
        )
    return found


class DockerFailed(RuntimeError):
    """The docker CLI is missing or exited non-zero."""


@dataclass(frozen=True)
class JobContainers:
    """Yield's job containers on this host's docker daemon."""

    runner: Runner = docker.run

    def _call(self, *args: str) -> str:
        try:
            result = self.runner(["docker", *args])
        except OSError as error:
            raise DockerFailed(str(error)) from error
        if result.returncode != 0:
            raise DockerFailed((result.stderr or result.stdout).strip()[-500:])
        return result.stdout

    def find(self) -> dict[str, Container]:
        return parse_ps(
            self._call("ps", "--all", "--filter", "label=yield.role", "--format", "{{json .}}")
        )

    def stack(self, project: str) -> dict[str, Service]:
        """Every container of the compose project `project`, running or not."""
        label = f"label=com.docker.compose.project={project}"
        listed = self._call("ps", "--all", "--filter", label, "--format", "{{json .}}")
        return parse_services(listed)

    def logs(self, name: str, *, tail: int = LOG_TAIL) -> list[LogLine]:
        """The container's last lines, stdout and stderr together (Yield's jobs log to stderr)."""
        try:
            result = self.runner(["docker", "logs", "--timestamps", "--tail", str(tail), name])
        except OSError as error:
            raise DockerFailed(str(error)) from error
        if result.returncode != 0:
            raise DockerFailed((result.stderr or "").strip()[-500:])
        return parse_logs(result.stdout + result.stderr)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Observer:
    """Takes one `Observation` of a set of runs."""

    client: Yield
    containers: JobContainers | None = None
    models: Compose | None = None
    model_names: tuple[str, ...] = ()
    stack_project: str | None = None
    """The stack's compose project, whose services are watched through `containers`."""
    now: Callable[[], datetime] = _now

    def statuses(self, run_ids: Iterable[str]) -> tuple[dict[str, RunStatus | None], str | None]:
        found: dict[str, RunStatus | None] = {}
        try:
            for run_id in run_ids:
                found[run_id] = self.client.status(run_id)
        except Unreachable as error:
            return found, str(error)
        return found, None

    def __call__(
        self,
        run_ids: Iterable[str],
        statuses: dict[str, RunStatus | None] | None = None,
    ) -> Observation:
        """Observe `run_ids`, reusing `statuses` when the caller has just read them."""
        wanted = list(run_ids)
        yield_error = None
        if statuses is None:
            statuses, yield_error = self.statuses(wanted)
        containers: dict[str, Container] = {}
        logs: dict[str, list[LogLine]] = {}
        stack: dict[str, Service] | None = None
        docker_error = None
        read = False
        if self.containers is not None:
            try:
                if self.stack_project is not None:
                    stack = self.containers.stack(self.stack_project)
                every = self.containers.find()
                containers = {name: c for name, c in every.items() if c.run_id in wanted}
                for name in containers:
                    logs[name] = self.containers.logs(name)
                read = True
            except DockerFailed as error:
                docker_error = str(error)
        models: dict[str, ServiceState | None] | None = None
        models_error = None
        if self.models is not None:
            try:
                states = self.models.ps()
                models = {name: states.get(name) for name in self.model_names}
            except (ComposeFailed, OSError) as error:
                models_error = str(error)
        return Observation(
            at=self.now(),
            runs=statuses,
            yield_error=yield_error,
            containers=containers,
            logs=logs,
            containers_read=read,
            docker_error=docker_error,
            models=models,
            models_error=models_error,
            stack=stack,
        )
