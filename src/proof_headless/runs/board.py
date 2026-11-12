"""The board `proof run watch` shows, and its plain rendering for a session without a terminal.

A `Board` is one sample: each model's state, each run's place in the queue and in Yield, the
anomalies new since the previous sample and the queue's events logged since then. The Textual
dashboard and `--plain` draw the same board, so the two can never disagree about a run. Sampling
reads only: Yield's API, the docker daemon, the queue's files. Watching never posts, never writes
the queue's state and never appends to its log; the anomalies a watcher finds are its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from rich.markup import escape

from proof_headless.docker import ServiceState
from proof_headless.runs.anomalies import Anomaly, Detector
from proof_headless.runs.observe import Observation
from proof_headless.runs.state import Event, Severity, Store

FEED_BACKLOG = 20
"""How many of the queue's past events the first sample shows."""

STYLE = {Severity.INFO: "muted", Severity.WARNING: "warn", Severity.ERROR: "fail"}


@dataclass(frozen=True)
class RunRow:
    run_id: str
    queue: str
    """The queue's state for the run, `-` when no queue has it."""
    phase: str
    closed: int
    planned: int | None
    failed: int
    liveness: str
    note: str

    @property
    def counts(self) -> str:
        planned = "?" if self.planned is None else str(self.planned)
        return f"{self.closed}/{planned}"


@dataclass(frozen=True)
class Board:
    at: datetime
    client: str
    runs: list[RunRow]
    models: dict[str, str] | None = None
    """Each model's `state/health`, `None` when no models manifest is known."""
    stack: dict[str, str] | None = None
    """Each stack service's `state/health`, `None` when the stack was not asked about."""
    anomalies: list[Anomaly] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


def _model(state: ServiceState | None) -> str:
    if state is None:
        return "absent"
    return f"{state.state}/{state.health or 'no check'}"


@dataclass
class Sampler:
    """Takes successive boards of one client's runs."""

    store: Store
    client: str
    run_ids: list[str]
    """The runs to show when no queue has been recorded: the rendered specs."""
    observe: Callable[[list[str]], Observation]
    detector: Detector = field(default_factory=Detector)
    events_read: int | None = None

    def _events(self) -> list[Event]:
        every = self.store.events()
        start = -FEED_BACKLOG if self.events_read is None else self.events_read
        fresh = every[start:]
        self.events_read = len(every)
        return fresh

    def sample(self) -> Board:
        state = self.store.read()
        ids = [entry.run_id for entry in state.runs] if state else list(self.run_ids)
        observation = self.observe(ids)
        anomalies = self.detector.examine(observation)
        rows = []
        for run_id in ids:
            status = observation.runs.get(run_id)
            entry = state.entry(run_id) if state else None
            health = status.health if status else None
            rows.append(
                RunRow(
                    run_id=run_id,
                    queue=str(entry.state) if entry else "-",
                    phase=str(status.phase) if status else ((entry.phase or "-") if entry else "-"),
                    closed=status.closed if status else (entry.closed if entry else 0),
                    planned=status.planned if status else (entry.planned if entry else None),
                    failed=status.failed if status else (entry.failed if entry else 0),
                    liveness=health.liveness if health else "-",
                    note=health.note if health else "",
                )
            )
        models = None
        if observation.models is not None:
            models = {name: _model(state) for name, state in observation.models.items()}
        stack = None
        if observation.stack is not None:
            stack = {
                name: f"{svc.state}/{svc.health or 'no check'}"
                + (f" ({svc.exit_code})" if svc.exit_code is not None else "")
                for name, svc in observation.stack.items()
            }
        return Board(
            at=observation.at,
            client=self.client,
            runs=rows,
            models=models,
            stack=stack,
            anomalies=anomalies,
            events=self._events(),
        )


def run_line(row: RunRow) -> str:
    style = {"complete": "ok", "failed": "fail", "refused": "fail"}.get(row.phase, "")
    phase = f"[{style}]{row.phase}[/{style}]" if style else row.phase
    line = (
        f"[name]{escape(row.run_id)}[/name] {phase} {row.counts} failed {row.failed} "
        f"queue {row.queue} liveness {row.liveness}"
    )
    return line + (f" [muted]{escape(row.note)}[/muted]" if row.note else "")


def anomaly_line(anomaly: Anomaly, at: datetime) -> str:
    style = STYLE[anomaly.severity]
    return (
        f"{at.strftime('%H:%M:%S')} [{style}]{anomaly.severity.upper()}[/{style}] "
        f"{escape(anomaly.subject)} {anomaly.kind}: {escape(anomaly.message)}"
    )


def event_line(event: Event) -> str:
    style = STYLE[event.severity]
    about = event.run_id or event.subject or "queue"
    clock = event.at[11:19] if len(event.at) >= 19 else event.at
    return (
        f"{clock} [{style}]{event.severity.upper()}[/{style}] {escape(about)} "
        f"{escape(event.kind)}: {escape(event.message)}"
    )


def models_line(models: dict[str, str], label: str = "models") -> str:
    parts = []
    for name, state in models.items():
        fine = state in ("running/healthy", "running/no check", "exited/no check (0)")
        style = "ok" if fine else "fail"
        parts.append(f"{escape(name)} [{style}]{escape(state)}[/{style}]")
    return f"{label}: " + (", ".join(parts) if parts else "no containers")


def plain(board: Board, previous: Board | None) -> list[str]:
    """The lines `--plain` prints for `board`: what changed since `previous`, and everything the
    first time, so a log of a night's watch reads as a history rather than a screen per minute."""
    lines: list[str] = []
    stamp = board.at.strftime("%H:%M:%S")
    if board.models is not None and (previous is None or previous.models != board.models):
        lines.append(f"{stamp} {models_line(board.models)}")
    if board.stack is not None and (previous is None or previous.stack != board.stack):
        lines.append(f"{stamp} {models_line(board.stack, 'stack')}")
    before = {row.run_id: row for row in previous.runs} if previous else {}
    for row in board.runs:
        if before.get(row.run_id) != row:
            lines.append(f"{stamp} {run_line(row)}")
    lines += [event_line(event) for event in board.events]
    lines += [anomaly_line(anomaly, board.at) for anomaly in board.anomalies]
    return lines
