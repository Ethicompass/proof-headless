"""Anomalies: what an operator used to find by tailing logs at three in the morning.

Each rule is a small pure function from what was observed to an `Anomaly` or nothing, which is
what lets them be tested as tables. They generalize the poller the last engagement ran beside its
queue: a run whose phase and counts stop moving while its driver logs nothing, failed traces that
keep growing, rate limits and timeouts in a driver's or a measure's log, a measure that has made
no model call for minutes, a job container that exited with an error, a model that is not healthy.

The `Detector` is the only part with memory. It keeps what the rules need from the previous
observation (when each run last moved, how many traces had failed, how far each log was read) and
reports an anomaly only when it is new: a condition that holds (a stall, a model down) once when
it starts and again only after it cleared, a count (failed traces, 429 lines) every time it grows.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from proof_headless.docker import ServiceState
from proof_headless.runs.client import IN_FLIGHT, Phase, RunStatus
from proof_headless.runs.observe import Container, LogLine, Observation, Service
from proof_headless.runs.state import Severity

STALL_AFTER = timedelta(minutes=45)
"""How long a run in flight may go without its counts moving and without a new line in its
driver's log before it reads as stalled.

Fifteen minutes was too short. On the appliance's load test of three concurrent runs (#27) the
last conversations of a run kept its closed count flat for more than 15 minutes, each of them
carrying many judge calls, while its driver called the model every 10 to 15 seconds; the queue
re-posted two busy runs and would have closed them. A new driver log line now counts as progress,
so where the containers can be read a busy run never comes near the bound. It matters where they
cannot, and there the counts are the only signal: three times the measured stretch leaves room
for a slower target or judge. A run whose budget allows less wall time is held to its budget."""
MEASURE_IDLE_AFTER = timedelta(minutes=6)
"""What the last engagement's poller used: a measure that had made no model call for six minutes
was worth a look, though its own timeout and retry usually healed it."""

RATE_LIMITED = re.compile(r"HTTP/[0-9.]+ 429|429 Too Many|TooManyRequests|RateLimitError", re.I)
TIMED_OUT = re.compile(
    r"ReadTimeout|ConnectTimeout|WriteTimeout|PoolTimeout|TimeoutException|timed out|"
    r"deadline exceeded",
    re.I,
)
TARGET_ERROR = re.compile(
    r"TargetServerError|ProfileTooThin|EmptySelection|connection refused", re.I
)
MODEL_CALL = re.compile(r"chat/completions|/embeddings|/rerank")


class Kind(StrEnum):
    STALLED = "stalled"
    LIVENESS = "liveness"
    FAILED_TRACES = "failed_traces"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TARGET_ERROR = "target_error"
    MEASURE_IDLE = "measure_idle"
    CONTAINER_EXITED = "container_exited"
    MODEL_DOWN = "model_down"
    STACK_DOWN = "stack_down"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Anomaly:
    kind: Kind
    severity: Severity
    subject: str
    """The run, container, model or service it is about."""
    message: str
    run_id: str | None = None
    tally: int | None = None
    """For a count that grew, the count: it makes each growth a new anomaly."""

    @property
    def key(self) -> str:
        base = f"{self.kind}:{self.subject}"
        return base if self.tally is None else f"{base}:{self.tally}"


def signature(run: RunStatus) -> str:
    """What has to change for a run to count as advancing.

    The counts while it attacks; while it measures they stand still for hours, and the measure's
    own note (`measuring toxicity: N texts embedded`, refreshed per request) is what moves.
    """
    progress = f"{run.phase} {run.closed}/{run.planned} failed {run.failed}"
    if run.phase is Phase.MEASURING and run.health is not None:
        progress += f" {run.health.note}"
    return progress


def stalled(
    run: RunStatus, *, since: datetime, now: datetime, limit: timedelta = STALL_AFTER
) -> Anomaly | None:
    """A run in flight whose `signature` has not changed for `limit`."""
    if run.phase not in IN_FLIGHT or now - since < limit:
        return None
    minutes = int((now - since).total_seconds() // 60)
    return Anomaly(
        Kind.STALLED,
        Severity.WARNING,
        run.run_id,
        f"no progress for {minutes} min ({signature(run)})",
        run_id=run.run_id,
    )


def liveness(run: RunStatus) -> Anomaly | None:
    """What Yield itself says about the process behind the run: `stalled` is its suspicion,
    `dead` its verdict."""
    health = run.health
    if health is None or health.liveness not in ("stalled", "dead"):
        return None
    severity = Severity.ERROR if health.liveness == "dead" else Severity.WARNING
    why = health.detail or health.note
    return Anomaly(
        Kind.LIVENESS,
        severity,
        run.run_id,
        f"Yield reads the {health.role or 'job'} as {health.liveness}"
        + (f": {why}" if why else ""),
        run_id=run.run_id,
    )


def failed_traces(before: int, run: RunStatus) -> Anomaly | None:
    """Failed units that grew since the last look."""
    if run.failed <= before:
        return None
    return Anomaly(
        Kind.FAILED_TRACES,
        Severity.WARNING,
        run.run_id,
        f"{run.failed} failed traces (+{run.failed - before}), {run.closed} closed of "
        f"{run.planned if run.planned is not None else '?'}",
        run_id=run.run_id,
        tally=run.failed,
    )


LOG_RULES: tuple[tuple[Kind, Severity, re.Pattern[str], str], ...] = (
    (Kind.RATE_LIMITED, Severity.ERROR, RATE_LIMITED, "rate-limit (429) lines"),
    (Kind.TIMEOUT, Severity.WARNING, TIMED_OUT, "timeout lines"),
    (Kind.TARGET_ERROR, Severity.WARNING, TARGET_ERROR, "target or driver error lines"),
)


def log_problems(
    container: Container, lines: Sequence[LogLine], *, seen: Mapping[Kind, int] | None = None
) -> list[Anomaly]:
    """Rate limits, timeouts and target errors in `lines`, the part of a job's log not read
    before. `seen` is how many matching lines of each kind were reported already, so the tally
    keeps growing across observations."""
    seen = seen or {}
    found = []
    for kind, severity, pattern, what in LOG_RULES:
        count = sum(1 for line in lines if pattern.search(line.text))
        if count:
            found.append(
                Anomaly(
                    kind,
                    severity,
                    container.name,
                    f"{count} new {what} in the {container.role}'s log",
                    run_id=container.run_id,
                    tally=seen.get(kind, 0) + count,
                )
            )
    return found


def measure_idle(
    container: Container,
    lines: Sequence[LogLine],
    *,
    now: datetime,
    limit: timedelta = MEASURE_IDLE_AFTER,
) -> Anomaly | None:
    """A running measure whose last model call is older than `limit`.

    A measure that has not called a model at all yet is left alone: it may still be loading the
    dataset, and there is no call to measure the silence from.
    """
    if container.role != "measure" or container.state != "running":
        return None
    calls = [line.at for line in lines if line.at is not None and MODEL_CALL.search(line.text)]
    if not calls or now - max(calls) < limit:
        return None
    minutes = int((now - max(calls)).total_seconds() // 60)
    return Anomaly(
        Kind.MEASURE_IDLE,
        Severity.WARNING,
        container.name,
        f"no model call for {minutes} min",
        run_id=container.run_id,
    )


def container_exited(container: Container) -> Anomaly | None:
    """A job container that ended badly. A driver that exits 0 has handed off to the measure, and
    a measure that exits 0 has closed the run: both are how a run is meant to end."""
    if container.state == "dead" or (
        container.state == "exited" and container.exit_code not in (0, None)
    ):
        return Anomaly(
            Kind.CONTAINER_EXITED,
            Severity.ERROR,
            container.name,
            f"the {container.role} container is {container.state}"
            + (f" with code {container.exit_code}" if container.exit_code is not None else ""),
            run_id=container.run_id,
        )
    if container.state == "restarting":
        return Anomaly(
            Kind.CONTAINER_EXITED,
            Severity.ERROR,
            container.name,
            f"the {container.role} container is restarting",
            run_id=container.run_id,
        )
    return None


def model_down(name: str, state: ServiceState | None) -> Anomaly | None:
    """A declared model that is not running and healthy."""
    if state is None:
        return Anomaly(Kind.MODEL_DOWN, Severity.ERROR, name, "the model has no container")
    if state.state != "running":
        return Anomaly(Kind.MODEL_DOWN, Severity.ERROR, name, f"the model is {state.state}")
    if state.health == "unhealthy":
        return Anomaly(Kind.MODEL_DOWN, Severity.ERROR, name, "the model's health check fails")
    return None


def stack_down(service: Service) -> Anomaly | None:
    """A service of the stack that is not running and healthy. A one-shot service (the seeds,
    the bucket makers) that exited 0 did its job; one that exited otherwise did not, and a dead
    `lens-api` silently loses every delivery."""
    if service.state == "running":
        if service.health == "unhealthy":
            return Anomaly(
                Kind.STACK_DOWN, Severity.ERROR, service.service, "the health check fails"
            )
        return None
    if service.state == "exited" and service.exit_code == 0:
        return None
    if service.state == "created":
        return None
    detail = f" with code {service.exit_code}" if service.exit_code is not None else ""
    return Anomaly(Kind.STACK_DOWN, Severity.ERROR, service.service, f"is {service.state}{detail}")


def stack_empty(stack: dict[str, Service] | None) -> Anomaly | None:
    if stack is None or stack:
        return None
    return Anomaly(Kind.STACK_DOWN, Severity.ERROR, "stack", "the stack has no containers")


def unreachable(subject: str, error: str | None) -> Anomaly | None:
    if error is None:
        return None
    return Anomaly(Kind.UNREACHABLE, Severity.ERROR, subject, f"cannot be read: {error}")


@dataclass
class Detector:
    """Runs every rule over each observation and reports what is new."""

    stall_after: timedelta = STALL_AFTER
    measure_idle_after: timedelta = MEASURE_IDLE_AFTER
    progress: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    read_until: dict[str, datetime] = field(default_factory=dict)
    tallies: dict[str, int] = field(default_factory=dict)
    holding: set[str] = field(default_factory=set)
    since: datetime | None = None
    """Log lines at or before this moment are history, not news: a container first seen has its
    tail read only past it. A queue starts it at its own start, a resumed one at its last look, a
    watcher at its launch; the history is in the queue's event log. `None` reads every line."""
    stall_limits: dict[str, timedelta] = field(default_factory=dict)
    """Per run, a stall bound shorter than `stall_after`, from the run's own budget."""
    driver_moved: set[str] = field(default_factory=set)
    """The runs whose driver logged a new line in the last observation: a driver that is still
    talking is a run that is still working, whatever its counts say."""

    def forget(self, run_id: str) -> None:
        """Measure the run's progress afresh, as after it was posted again."""
        self.progress.pop(run_id, None)

    def seed(self, run_id: str, progress: str, since: datetime, failed: int) -> None:
        """Start from what a queue recorded, so a resumed queue neither forgets how long a run
        has been stuck nor reports its old failed traces again."""
        self.progress[run_id] = (progress, since)
        self.failed[run_id] = failed

    def _new_lines(self, name: str, lines: Sequence[LogLine]) -> list[LogLine]:
        until = self.read_until.get(name) or self.since
        stamped = [line.at for line in lines if line.at is not None]
        if stamped:
            self.read_until[name] = max([*stamped, *([until] if until else [])])
        if until is None:
            return list(lines)
        return [line for line in lines if line.at is not None and line.at > until]

    def _runs(self, observation: Observation) -> list[Anomaly]:
        found: list[Anomaly] = []
        for run_id, run in observation.runs.items():
            if run is None:
                continue
            progress = signature(run)
            previous = self.progress.get(run_id)
            if previous is None or previous[0] != progress or run_id in self.driver_moved:
                self.progress[run_id] = (progress, observation.at)
            since = self.progress[run_id][1]
            limit = min(self.stall_after, self.stall_limits.get(run_id, self.stall_after))
            stall = stalled(run, since=since, now=observation.at, limit=limit)
            grown = failed_traces(self.failed.get(run_id, 0), run)
            self.failed[run_id] = max(run.failed, self.failed.get(run_id, 0))
            found += [a for a in (stall, liveness(run), grown) if a is not None]
        return found

    def _containers(self, observation: Observation) -> list[Anomaly]:
        found: list[Anomaly] = []
        self.driver_moved = set()
        for name, container in observation.containers.items():
            lines = observation.logs.get(name, [])
            seen = {kind: self.tallies.get(f"{kind}:{name}", 0) for kind, *_ in LOG_RULES}
            new = self._new_lines(name, lines)
            if container.role == "driver" and new:
                self.driver_moved.add(container.run_id)
            found += log_problems(container, new, seen=seen)
            idle = measure_idle(container, lines, now=observation.at, limit=self.measure_idle_after)
            found += [a for a in (idle, container_exited(container)) if a is not None]
        return found

    def examine(self, observation: Observation) -> list[Anomaly]:
        """Every anomaly in `observation` that was not already holding at the last one."""
        containers = self._containers(observation)
        current = self._runs(observation) + containers
        for model, state in (observation.models or {}).items():
            down = model_down(model, state)
            if down is not None:
                current.append(down)
        stack = [stack_down(service) for service in (observation.stack or {}).values()]
        current += [a for a in [*stack, stack_empty(observation.stack)] if a is not None]
        for subject, error in (
            ("yield", observation.yield_error),
            ("docker", observation.docker_error),
            ("models", observation.models_error),
        ):
            gone = unreachable(subject, error)
            if gone is not None:
                current.append(gone)
        for anomaly in current:
            if anomaly.tally is not None and anomaly.kind is not Kind.FAILED_TRACES:
                self.tallies[f"{anomaly.kind}:{anomaly.subject}"] = anomaly.tally
        new = [a for a in current if a.tally is not None or a.key not in self.holding]
        self.holding = {a.key for a in current if a.tally is None}
        return new
