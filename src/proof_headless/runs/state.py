"""A queue's state on disk: `queue.json`, the `events.jsonl` beside it, and the lock between them.

All three live in the client's state directory, `$PROOF_HOME/bundles/<client>/`, next to the
receipt and the rendered specs, never in the bundle. `queue.json` is the queue's present, rewritten
whole (through a temporary file and a rename, so a crash mid-write leaves the previous one) after
every change; it is what `--resume` continues from. `events.jsonl` is its past, only ever appended
to, so what happened during a night of runs survives the queue that lived it. The lock keeps a
second `proof run queue` from driving the same client's runs: two queues would each count the
other's runs outside their concurrency.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from proof_headless import paths

QUEUE_FILE = "queue.json"
SUBMITTED_DIR = "submitted"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = "queue.lock"


def state_dir(client: str) -> Path:
    """The client's state directory under `$PROOF_HOME`."""
    return paths.home() / "bundles" / client


def specs_dir(client: str) -> Path:
    """Where `proof spec render` writes the client's run specs."""
    return state_dir(client) / "specs"


def stamp(moment: datetime) -> str:
    """A UTC moment as the state files write it."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment)


class RunState(StrEnum):
    """Where a run is in the queue, as opposed to where it is in Yield (its phase)."""

    PENDING = "pending"
    ACTIVE = "active"
    """Submitted, or found in flight, and not closed: the queue polls it."""
    COMPLETE = "complete"
    FAILED = "failed"
    REFUSED = "refused"
    """Yield's gate refused the spec before the queue ever had it accepted."""
    STALLED = "stalled"
    """It stopped advancing before measuring and every re-POST was spent. Yield may still hold it
    in flight; the queue stops waiting so that a night of runs ends."""


TERMINAL = frozenset({RunState.COMPLETE, RunState.FAILED, RunState.REFUSED, RunState.STALLED})


class Entry(BaseModel):
    """One run of the queue."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    spec_sha256: str = ""
    """The digest of the spec file as the queue last read it. A spec re-rendered since submission
    is a different request, which Yield answers with 409 on the next re-POST."""
    state: RunState = RunState.PENDING
    phase: str | None = None
    planned: int | None = None
    closed: int = 0
    failed: int = 0
    liveness: str | None = None
    note: str = ""
    submitted_at: str | None = None
    progress_at: str | None = None
    """The last time the run's phase or counts moved, which is what a stall is measured from."""
    progress: str = ""
    """The phase and counts as of `progress_at`."""
    finished_at: str | None = None
    retries: int = 0
    """Re-POSTs after Yield read the run as failed."""
    relaunch_attempt: str | None = None
    relaunch_at: str | None = None
    """Set by a re-POST of a failed run: the attempt Yield showed then, and when. Yield keeps
    reading `failed` until the relaunched driver announces a new attempt, so until the attempt
    changes (or a grace period passes) `failed` means "relaunch pending", not "failed again"."""
    reposts_blocked: bool = False
    """A re-POST was answered 409: Yield holds a different spec under this id. The run is followed
    without posting it again."""
    stall_resubmits: int = 0
    """Re-POSTs after the run stopped advancing before measuring."""
    submit_failures: int = 0
    """Submissions Yield did not answer, or answered 503."""
    error: str | None = None
    metrics: dict[str, str] = Field(default_factory=dict)
    """Each metric's status in the run's manifest once it closed."""
    lens: str | None = None
    """Once complete, its Lens result: `waiting`, `done`, `launched` (the queue launched Lens's
    job after the bound), or `failed` (nothing could be launched). `None` when not followed."""
    lens_since: str | None = None


class QueueState(BaseModel):
    """The whole queue."""

    model_config = ConfigDict(extra="forbid")

    client: str
    started_at: str
    concurrency: int = Field(ge=1)
    runs: list[Entry]
    observed_at: str | None = None
    """The last time the queue looked at the job containers. A resumed queue reports only log
    lines newer than this, so the night's 429s are not logged a second time."""
    log_marks: dict[str, str] = Field(default_factory=dict)
    """Per job container, the newest log line the queue has read."""

    def entry(self, run_id: str) -> Entry:
        for item in self.runs:
            if item.run_id == run_id:
                return item
        raise KeyError(run_id)

    def active(self) -> list[Entry]:
        return [item for item in self.runs if item.state is RunState.ACTIVE]

    def pending(self) -> list[Entry]:
        return [item for item in self.runs if item.state is RunState.PENDING]

    @property
    def done(self) -> bool:
        """Every run closed, and no complete run still waiting for its Lens result."""
        return all(item.state in TERMINAL and item.lens != "waiting" for item in self.runs)

    @property
    def succeeded(self) -> bool:
        return all(item.state is RunState.COMPLETE for item in self.runs)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Event(BaseModel):
    """One line of `events.jsonl`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    at: str
    kind: str
    severity: Severity = Severity.INFO
    run_id: str | None = None
    subject: str | None = None
    message: str


class QueueBusy(RuntimeError):
    """Another `proof run queue` holds this client's lock."""


class Store:
    """A client's queue files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.queue_path = directory / QUEUE_FILE
        self.events_path = directory / EVENTS_FILE
        self.lock_path = directory / LOCK_FILE
        self.submitted_dir = directory / SUBMITTED_DIR

    def submitted(self, run_id: str) -> Path:
        """The spec exactly as Yield first accepted it for `run_id`. Every re-POST sends these
        bytes: Yield compares a retry with the frozen spec whole, so a spec re-rendered since would
        be answered 409."""
        return self.submitted_dir / f"{run_id}.json"

    def read(self) -> QueueState | None:
        if not self.queue_path.exists():
            return None
        return QueueState.model_validate_json(self.queue_path.read_text())

    def write(self, state: QueueState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = self.queue_path.with_suffix(".json.partial")
        partial.write_text(state.model_dump_json(indent=2) + "\n")
        os.replace(partial, self.queue_path)

    def append(self, event: Event) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a") as handle:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")

    def events(self, tail: int | None = None) -> list[Event]:
        """The logged events, oldest first; the last `tail` of them when given. A line that does
        not parse (a write cut short by a crash) is skipped, not fatal."""
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text().splitlines()
        if tail is not None:
            lines = lines[-tail:]
        events = []
        for line in lines:
            try:
                events.append(Event.model_validate(json.loads(line)))
            except (ValueError, TypeError):
                continue
        return events

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the client's queue lock, or refuse at once if another queue holds it.

        `flock` is released by the kernel when the process dies, so a crashed queue never leaves
        a lock behind for `--resume` to trip on.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QueueBusy(
                    f"another proof run queue holds {self.lock_path}; wait for it, or stop it"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
