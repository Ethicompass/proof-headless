"""The queue: a bundle's runs through Yield, a bounded number at a time, until each one closes.

It replaces the serial queue the last engagement wrote for itself. That one ran one run at a time
because concurrent grading against a hosted judge drew 429s; since compute 0.1.10 the judge's rate
limit is shared through Valkey, so runs can overlap and `concurrency` bounds how many do.

Each `tick` polls the runs in flight, then fills the free slots. A run is never posted blind:
Yield is asked first, and a run it already closed is adopted as complete, one it is still running
is adopted as active, and only a run it does not know or reads as failed is posted. That is what
makes `--resume` safe after a crash at any point, including between a POST and the write that
recorded it: Yield's retry contract answers an identical re-POST with 202, but a re-POST of a
closed run would launch a driver again.

A run that Yield reads as failed is posted again, identically, up to `retries` times: the
relaunched driver resumes from what the store holds and replays closed traces without calling the
target. A run whose phase and counts stop moving before it measures is posted again the same way
(a no-op while its driver is alive, a relaunch when it is not), up to `stall_resubmits` times. A
measuring run is never re-posted: a stalled measure is reported, and `proof run remeasure` is the
operator's call.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from proof_headless.runs.anomalies import STALL_AFTER, Anomaly, Detector, signature
from proof_headless.runs.client import (
    BEFORE_MEASURE,
    IN_FLIGHT,
    Answer,
    Phase,
    RunStatus,
    Unreachable,
    Yield,
    metric_outcomes,
)
from proof_headless.runs.observe import Observation
from proof_headless.runs.state import (
    Entry,
    Event,
    QueueState,
    RunState,
    Severity,
    Store,
    parse,
    stamp,
)


@dataclass(frozen=True)
class Policy:
    concurrency: int = 2
    retries: int = 1
    """Identical re-POSTs of a run Yield reads as failed, before the queue gives up on it."""
    stall_after: timedelta = STALL_AFTER
    stall_resubmits: int = 2
    submit_attempts: int = 5
    """Submissions answered 503, or not answered, before the run is marked failed."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_specs(directory: Path, only: Iterable[str] = ()) -> dict[str, Path]:
    """The rendered specs in `directory` by run id, in name order, narrowed to `only`.

    Raises:
        KeyError: A run named in `only` has no spec.
    """
    found = {path.stem: path for path in sorted(directory.glob("*.json"))}
    wanted = list(only)
    missing = [run for run in wanted if run not in found]
    if missing:
        raise KeyError(f"no rendered spec for {missing} in {directory}")
    return {run: found[run] for run in wanted} if wanted else found


def new_state(client: str, specs: dict[str, Path], concurrency: int, now: datetime) -> QueueState:
    return QueueState(
        client=client,
        started_at=stamp(now),
        concurrency=concurrency,
        runs=[Entry(run_id=run, spec_sha256=digest(path)) for run, path in specs.items()],
    )


def _now() -> datetime:
    return datetime.now(UTC)


Observe = Callable[[list[str], dict[str, RunStatus | None]], Observation]


@dataclass
class Queue:
    client: Yield
    store: Store
    state: QueueState
    specs: dict[str, Path]
    policy: Policy = field(default_factory=Policy)
    now: Callable[[], datetime] = _now
    observe: Observe | None = None
    """Takes a fuller observation (containers, logs, models) for the anomaly rules. Without one
    the rules see the run statuses the queue just read."""
    report: Callable[[Event], None] = lambda event: None
    detector: Detector = field(default_factory=Detector)
    unreachable: bool = False
    """Whether Yield stopped answering, so an outage is logged when it starts and when it ends
    rather than on every poll."""

    def __post_init__(self) -> None:
        self.detector.stall_after = self.policy.stall_after
        for entry in self.state.active():
            if entry.progress_at is not None:
                self.detector.seed(
                    entry.run_id, entry.progress, parse(entry.progress_at), entry.failed
                )

    def log(
        self,
        kind: str,
        message: str,
        *,
        severity: Severity = Severity.INFO,
        run_id: str | None = None,
        subject: str | None = None,
    ) -> None:
        event = Event(
            at=stamp(self.now()),
            kind=kind,
            severity=severity,
            run_id=run_id,
            subject=subject,
            message=message,
        )
        self.store.append(event)
        self.report(event)

    def save(self) -> None:
        self.store.write(self.state)

    def _spec(self, entry: Entry) -> dict[str, Any] | None:
        path = self.specs.get(entry.run_id)
        if path is None or not path.exists():
            self._close(entry, RunState.REFUSED, f"no rendered spec for {entry.run_id}")
            return None
        current = digest(path)
        if entry.spec_sha256 and current != entry.spec_sha256 and entry.submitted_at is not None:
            self.log(
                "spec_changed",
                f"{path.name} changed since it was submitted; Yield will refuse it under the "
                f"same run id (409). Render a new run id to change a run.",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
        entry.spec_sha256 = current
        document: dict[str, Any] = json.loads(path.read_text())
        return document

    def _close(self, entry: Entry, state: RunState, error: str | None = None) -> None:
        entry.state = state
        entry.error = error
        entry.finished_at = stamp(self.now())
        severity = {
            RunState.COMPLETE: Severity.INFO,
            RunState.FAILED: Severity.ERROR,
            RunState.REFUSED: Severity.ERROR,
        }[state]
        if state is RunState.COMPLETE and any(s == "failed" for s in entry.metrics.values()):
            severity = Severity.WARNING
        message = f"{state}" + (f": {error}" if error else "")
        if entry.metrics:
            message += " (metrics: " + ", ".join(f"{m} {s}" for m, s in entry.metrics.items()) + ")"
        self.log(str(state), message, severity=severity, run_id=entry.run_id)

    def _post(self, entry: Entry, why: str) -> bool:
        """POST the run's spec. True when Yield accepted it."""
        spec = self._spec(entry)
        if spec is None:
            return False
        submitted = self.client.submit(spec)
        if submitted.answer is Answer.ACCEPTED:
            moment = stamp(self.now())
            entry.state = RunState.ACTIVE
            entry.submitted_at = entry.submitted_at or moment
            entry.progress_at = moment
            entry.submit_failures = 0
            self.detector.progress.pop(entry.run_id, None)
            self.log("submitted", why, run_id=entry.run_id)
            return True
        if submitted.answer in (Answer.CONFLICT, Answer.REFUSED):
            self._close(
                entry,
                RunState.REFUSED,
                f"Yield answered {submitted.status_code}: {submitted.detail}",
            )
            return False
        entry.submit_failures += 1
        if entry.submit_failures >= self.policy.submit_attempts:
            self._close(
                entry,
                RunState.FAILED,
                f"{entry.submit_failures} submissions unanswered: {submitted.detail}",
            )
        else:
            self.log(
                "submit_unavailable",
                f"Yield did not accept the run ({submitted.status_code or 'no answer'}: "
                f"{submitted.detail}); trying again",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
        return False

    def _record(self, entry: Entry, status: RunStatus) -> None:
        entry.phase = status.phase
        entry.planned = status.planned
        entry.closed = status.closed
        entry.failed = status.failed
        entry.liveness = status.health.liveness if status.health else None
        entry.note = status.health.note if status.health else ""
        progress = signature(status)
        if progress != entry.progress or entry.progress_at is None:
            entry.progress = progress
            entry.progress_at = stamp(self.now())

    def _complete(self, entry: Entry) -> None:
        try:
            manifest = self.client.result(entry.run_id)
        except Unreachable:
            manifest = None
        entry.metrics = metric_outcomes(manifest or {})
        self._close(entry, RunState.COMPLETE)

    def _start(self, entry: Entry) -> None:
        """Adopt a run Yield already has, or post it."""
        status = self.client.status(entry.run_id)
        if status is not None:
            self._record(entry, status)
        if status is not None and status.phase is Phase.COMPLETE:
            self.log("adopted", "already complete in Yield; not posted again", run_id=entry.run_id)
            self._complete(entry)
        elif status is not None and status.phase in IN_FLIGHT:
            entry.state = RunState.ACTIVE
            entry.submitted_at = entry.submitted_at or stamp(self.now())
            self.log("adopted", f"already {status.phase} in Yield", run_id=entry.run_id)
        else:
            why = "posted" if status is None else "posted again over a failed earlier attempt"
            self._post(entry, why)

    def _poll(self, entry: Entry, status: RunStatus | None) -> None:
        if status is None:
            self.log(
                "lost",
                "Yield no longer knows this run; posting it again",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
            self._post(entry, "posted again after Yield lost it")
            return
        before = entry.phase
        self._record(entry, status)
        if before is not None and before != status.phase:
            self.log("phase", f"{before} -> {status.phase}", run_id=entry.run_id)
        if status.phase is Phase.COMPLETE:
            self._complete(entry)
        elif status.phase is Phase.FAILED:
            why = status.health.detail if status.health and status.health.detail else ""
            if entry.retries < self.policy.retries:
                entry.retries += 1
                self.log(
                    "retry",
                    f"Yield reads the run as failed{': ' + why if why else ''}; posting it again "
                    f"({entry.retries} of {self.policy.retries})",
                    severity=Severity.WARNING,
                    run_id=entry.run_id,
                )
                self._post(entry, "posted again to resume")
            else:
                self._close(entry, RunState.FAILED, why or "Yield reads the run as failed")
        elif status.phase in BEFORE_MEASURE and entry.progress_at is not None:
            quiet = self.now() - parse(entry.progress_at)
            if (
                quiet >= self.policy.stall_after
                and entry.stall_resubmits < self.policy.stall_resubmits
            ):
                entry.stall_resubmits += 1
                self._post(
                    entry,
                    f"posted again after {int(quiet.total_seconds() // 60)} min without "
                    f"progress ({entry.stall_resubmits} of {self.policy.stall_resubmits})",
                )

    def _anomalies(self, statuses: dict[str, RunStatus | None]) -> list[Anomaly]:
        ids = list(dict.fromkeys([*statuses, *(e.run_id for e in self.state.active())]))
        if self.observe is not None:
            observation = self.observe(ids, statuses)
        else:
            observation = Observation(at=self.now(), runs=statuses)
        return self.detector.examine(observation)

    def tick(self) -> None:
        """Poll the runs in flight, fill the free slots, look for anomalies, save."""
        statuses: dict[str, RunStatus | None] = {}
        try:
            for entry in self.state.active():
                status = self.client.status(entry.run_id)
                statuses[entry.run_id] = status
                self._poll(entry, status)
            free = self.state.concurrency - len(self.state.active())
            for entry in self.state.pending():
                if free <= 0:
                    break
                self._start(entry)
                if entry.state is RunState.ACTIVE:
                    free -= 1
            if self.unreachable:
                self.unreachable = False
                self.log("reachable", "Yield answers again", subject="yield")
        except Unreachable as error:
            if not self.unreachable:
                self.unreachable = True
                self.log("unreachable", str(error), severity=Severity.ERROR, subject="yield")
        finally:
            self.save()
        for anomaly in self._anomalies(statuses):
            self.log(
                f"anomaly.{anomaly.kind}",
                anomaly.message,
                severity=anomaly.severity,
                run_id=anomaly.run_id,
                subject=anomaly.subject,
            )

    def run(self, sleep: Callable[[float], None], poll_seconds: float) -> QueueState:
        """Tick until every run is complete, failed or refused."""
        self.log(
            "queue",
            f"{len(self.state.runs)} runs, {self.state.concurrency} at a time; "
            f"{sum(1 for e in self.state.runs if e.state is RunState.PENDING)} to start",
        )
        while True:
            self.tick()
            if self.state.done:
                break
            sleep(poll_seconds)
        closed = {s: sum(1 for e in self.state.runs if e.state is s) for s in RunState}
        self.log(
            "queue_done",
            ", ".join(f"{n} {s}" for s, n in closed.items() if n),
            severity=Severity.INFO if self.state.succeeded else Severity.WARNING,
        )
        return self.state
