"""The queue: a bundle's runs through Yield, a bounded number at a time, until each one closes.

It replaces the serial queue the last engagement wrote for itself. That one ran one run at a time
because concurrent grading against a hosted judge drew 429s; since compute 0.1.10 the judge's rate
limit is shared through Valkey, so runs can overlap and `concurrency` bounds how many do.

Each `tick` polls the runs in flight, then fills the free slots. A run is never posted blind:
Yield is asked first. A run it closed is adopted as complete and one it is measuring is adopted as
active; anything else is posted. Yield's retry contract answers an identical re-POST with 202,
launching a driver only when none is running, so posting a run that is accepted or attacking is
safe and is what revives a run whose first dispatch failed. Posting a run that is measuring or
closed would launch a driver for an attack that is over. That is what makes `--resume` safe after
a crash at any point, including between a POST and the write that recorded it.

Every re-POST sends the bytes Yield first accepted, kept beside the queue, since a retry is
compared with the frozen spec whole. A run that Yield reads as failed is posted again up to
`retries` times: the relaunched driver resumes from what the store holds and replays closed traces
without calling the target. Until the relaunch announces a new attempt, Yield still reads the old
one's `failed`; the run keeps its slot meanwhile. A run whose phase and counts stop moving before
it measures is posted again the same way, up to `stall_resubmits` times, and then closed as
`stalled` so the queue ends. A measuring run is never re-posted: a stalled measure is reported.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from proof_headless.runs.anomalies import STALL_AFTER, Anomaly, Detector, signature
from proof_headless.runs.client import (
    BEFORE_MEASURE,
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
    relaunch_grace: timedelta = timedelta(minutes=10)
    """How long after re-posting a failed run its `failed` may still be the old attempt's: the
    relaunched driver announces itself only once its container has booted."""


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
        self.detector.since = parse(self.state.observed_at or self.state.started_at)
        self.detector.read_until.update(
            {name: parse(mark) for name, mark in self.state.log_marks.items()}
        )
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

    def _spec(self, entry: Entry) -> bytes | None:
        """What to POST for `entry`: the bytes Yield first accepted, once it accepted any, and the
        rendered file before that."""
        kept = self.store.submitted(entry.run_id)
        path = self.specs.get(entry.run_id)
        if entry.submitted_at is not None and kept.exists():
            if path is not None and path.exists() and digest(path) != entry.spec_sha256:
                self.log(
                    "spec_changed",
                    f"{path.name} changed since it was submitted; the queue keeps posting what "
                    f"Yield accepted. Render a new run id to change a run.",
                    severity=Severity.WARNING,
                    run_id=entry.run_id,
                )
                entry.spec_sha256 = digest(path)
            return kept.read_bytes()
        if path is None or not path.exists():
            self._close(entry, RunState.REFUSED, f"no rendered spec for {entry.run_id}")
            return None
        entry.spec_sha256 = digest(path)
        return path.read_bytes()

    def _close(self, entry: Entry, state: RunState, error: str | None = None) -> None:
        entry.state = state
        entry.error = error
        entry.finished_at = stamp(self.now())
        severity = Severity.INFO if state is RunState.COMPLETE else Severity.ERROR
        if state is RunState.COMPLETE and any(s == "failed" for s in entry.metrics.values()):
            severity = Severity.WARNING
        message = f"{state}" + (f": {error}" if error else "")
        if entry.metrics:
            message += " (metrics: " + ", ".join(f"{m} {s}" for m, s in entry.metrics.items()) + ")"
        self.log(str(state), message, severity=severity, run_id=entry.run_id)

    def _accepted(self, entry: Entry, body: bytes, why: str) -> None:
        moment = stamp(self.now())
        if entry.submitted_at is None:
            if b'"webhook_url"' not in body:
                self.log(
                    "no_webhook",
                    "the spec has no webhook_url: its results reach no Lens and stay in Yield's "
                    "bucket. Render it again before the first submission to fix that.",
                    severity=Severity.WARNING,
                    run_id=entry.run_id,
                )
            kept = self.store.submitted(entry.run_id)
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(body)
        entry.state = RunState.ACTIVE
        entry.submitted_at = entry.submitted_at or moment
        entry.progress_at = moment
        entry.submit_failures = 0
        self.detector.forget(entry.run_id)
        self.log("submitted", why, run_id=entry.run_id)

    def _conflict(self, entry: Entry, detail: str) -> None:
        """409: Yield froze a different spec under this id. It has the run, so the run is followed
        as Yield holds it rather than closed while it may still be attacking."""
        entry.reposts_blocked = True
        status = self.client.status(entry.run_id)
        message = (
            f"Yield holds a different spec under this id ({detail}); following the run without "
            f"posting it again"
        )
        self.log("conflict", message, severity=Severity.ERROR, run_id=entry.run_id)
        if status is None:
            self._close(entry, RunState.REFUSED, detail)
            return
        self._record(entry, status)
        if status.phase is Phase.COMPLETE:
            self._complete(entry)
        elif status.phase is Phase.FAILED:
            self._close(entry, RunState.FAILED, f"failed, and cannot be posted again: {detail}")
        else:
            entry.state = RunState.ACTIVE
            entry.submitted_at = entry.submitted_at or stamp(self.now())

    def _post(self, entry: Entry, why: str) -> bool:
        """POST the run's spec. True when Yield accepted it."""
        body = self._spec(entry)
        if body is None:
            return False
        submitted = self.client.submit(json.loads(body))
        if submitted.answer is Answer.ACCEPTED:
            self._accepted(entry, body, why)
            return True
        if submitted.answer is Answer.CONFLICT:
            self._conflict(entry, f"Yield answered 409: {submitted.detail}")
            return False
        if submitted.answer is Answer.REFUSED:
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

    def _relaunch(self, entry: Entry, status: RunStatus, why: str) -> None:
        """Post a failed run again, and remember which attempt had failed."""
        if self._post(entry, why):
            entry.relaunch_attempt = status.attempt
            entry.relaunch_at = stamp(self.now())

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
        """Adopt a run Yield is measuring or has closed; post anything else.

        Only `measuring` and `complete` must not be posted: a re-POST would launch a driver for an
        attack that is over. An identical re-POST of a run that is `accepted`, `generating` or
        `attacking` answers 202 while its driver lives and launches one when none does, which is
        the case of a POST whose dispatch failed after the spec was frozen (a 503 that leaves a
        run reading `accepted` with nothing driving it).
        """
        status = self.client.status(entry.run_id)
        if status is not None:
            self._record(entry, status)
        if status is not None and status.phase is Phase.COMPLETE:
            self.log("adopted", "already complete in Yield; not posted again", run_id=entry.run_id)
            self._complete(entry)
        elif status is not None and status.phase is Phase.MEASURING:
            entry.state = RunState.ACTIVE
            entry.submitted_at = entry.submitted_at or stamp(self.now())
            self.log("adopted", "already measuring in Yield", run_id=entry.run_id)
        elif status is not None and status.phase is Phase.FAILED:
            self._relaunch(entry, status, "posted again over a failed earlier attempt")
        else:
            self._post(entry, "posted" if status is None else f"posted while {status.phase}")

    def _relaunch_pending(self, entry: Entry, status: RunStatus) -> bool:
        """Whether a `failed` read is the relaunch not having begun yet.

        Yield reads `failed` while the newest attempt has a failure record, and the relaunched
        driver writes its own attempt marker only once its container boots, so the read right
        after a 202 is usually the old attempt's death. It is "pending" while the attempt is the
        one that failed and the grace period has not passed; a new attempt that failed, or no new
        attempt in time, is a failure of its own.
        """
        if entry.relaunch_at is None:
            return False
        waiting = self.now() - parse(entry.relaunch_at) < self.policy.relaunch_grace
        if status.attempt == entry.relaunch_attempt and waiting:
            return True
        if status.attempt == entry.relaunch_attempt:
            self.log(
                "relaunch_missing",
                f"no new attempt began within {self.policy.relaunch_grace} of the re-POST",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
        entry.relaunch_attempt = None
        entry.relaunch_at = None
        return False

    def _failed(self, entry: Entry, status: RunStatus) -> None:
        if self._relaunch_pending(entry, status):
            return
        why = status.health.detail if status.health and status.health.detail else ""
        if entry.reposts_blocked:
            self._close(entry, RunState.FAILED, why or "failed, and cannot be posted again")
        elif entry.retries < self.policy.retries:
            entry.retries += 1
            self.log(
                "retry",
                f"Yield reads the run as failed{': ' + why if why else ''}; posting it again "
                f"({entry.retries} of {self.policy.retries})",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
            self._relaunch(entry, status, "posted again to resume")
        else:
            self._close(entry, RunState.FAILED, why or "Yield reads the run as failed")

    def _stalled(self, entry: Entry) -> None:
        if entry.progress_at is None:
            return
        quiet = self.now() - parse(entry.progress_at)
        if quiet < self.policy.stall_after:
            return
        minutes = int(quiet.total_seconds() // 60)
        if entry.stall_resubmits < self.policy.stall_resubmits and not entry.reposts_blocked:
            entry.stall_resubmits += 1
            self.log(
                "stalled",
                f"no progress for {minutes} min ({entry.progress})",
                severity=Severity.WARNING,
                run_id=entry.run_id,
            )
            self._post(
                entry,
                f"posted again after {minutes} min without progress "
                f"({entry.stall_resubmits} of {self.policy.stall_resubmits})",
            )
        else:
            self._close(
                entry,
                RunState.STALLED,
                f"no progress for {minutes} min after {entry.stall_resubmits} re-POSTs; "
                f"Yield may still hold it in flight",
            )

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
        if status.phase is not Phase.FAILED and status.attempt != entry.relaunch_attempt:
            entry.relaunch_attempt = None
            entry.relaunch_at = None
        if status.phase is Phase.COMPLETE:
            self._complete(entry)
        elif status.phase is Phase.FAILED:
            self._failed(entry, status)
        elif status.phase in BEFORE_MEASURE:
            self._stalled(entry)

    def _anomalies(self, statuses: dict[str, RunStatus | None]) -> list[Anomaly]:
        ids = list(dict.fromkeys([*statuses, *(e.run_id for e in self.state.active())]))
        if self.observe is not None:
            observation = self.observe(ids, statuses)
        else:
            observation = Observation(at=self.now(), runs=statuses)
        found = self.detector.examine(observation)
        self.state.observed_at = observation.at.isoformat()
        self.state.log_marks = {
            name: mark.isoformat() for name, mark in self.detector.read_until.items()
        }
        return found

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
        self.save()

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
