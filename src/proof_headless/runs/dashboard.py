"""`proof run watch`: the Textual dashboard over a client's runs.

It draws the same `Board` `--plain` prints: the models, a table of runs, and a feed of the queue's
events and the anomalies the watcher found. Sampling asks Yield and the docker daemon, which can
take seconds, so it runs in a worker thread and the screen stays responsive meanwhile.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Span, Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from proof_headless.console import THEME
from proof_headless.runs.board import Board, Sampler, anomaly_line, event_line, models_line


def themed(markup: str) -> Text:
    """Markup written with the CLI's theme names (`ok`, `fail`, ...) as a `Text` Textual can draw:
    its console does not know the CLI's theme, so each name is replaced by the style it stands
    for."""
    text = Text.from_markup(markup)
    text.spans = [
        Span(span.start, span.end, THEME.styles.get(str(span.style), span.style))
        for span in text.spans
    ]
    return text


RUN_COLUMNS = ("run", "queue", "phase", "closed", "failed", "liveness", "note")


class WatchApp(App[None]):
    """Each run's phase and counts, the models' and the stack's health and the anomaly feed."""

    TITLE = "proof run watch"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "sample", "Refresh"),
    ]
    CSS = """
    #models { height: auto; padding: 0 1; }
    #runs { height: auto; max-height: 50%; }
    #feed { height: 1fr; border-top: solid $panel; }
    """

    def __init__(self, sampler: Sampler, interval: float) -> None:
        super().__init__()
        self.sampler = sampler
        self.interval = interval
        self.board: Board | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("models: reading", id="models")
            yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
            yield RichLog(id="feed", markup=True, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.sampler.client
        table = self.query_one("#runs", DataTable)
        for column in RUN_COLUMNS:
            table.add_column(column, key=column)
        self.action_sample()
        self.set_interval(self.interval, self.action_sample)

    @work(thread=True, exclusive=True, group="sample")
    def action_sample(self) -> None:
        board = self.sampler.sample()
        self.call_from_thread(self.show, board)

    def show(self, board: Board) -> None:
        self.board = board
        models = self.query_one("#models", Static)
        lines = [models_line(board.models) if board.models is not None else "models: -"]
        if board.stack is not None:
            lines.append(models_line(board.stack, "stack"))
        models.update(themed("\n".join(lines)))
        table = self.query_one("#runs", DataTable)
        table.clear()
        for row in board.runs:
            table.add_row(
                row.run_id,
                row.queue,
                row.phase,
                row.counts,
                str(row.failed),
                row.liveness,
                row.note,
                key=row.run_id,
            )
        feed = self.query_one("#feed", RichLog)
        for event in board.events:
            feed.write(themed(event_line(event)))
        for anomaly in board.anomalies:
            feed.write(themed(anomaly_line(anomaly, board.at)))
