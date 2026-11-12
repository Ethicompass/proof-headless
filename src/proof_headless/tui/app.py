"""The Textual shell `proof` opens with no command.

For now a frame: a header, a body that says what the appliance has not been told yet, and the key
bindings. Each area adds its panel here as it lands (models, stack, runs), reading the same state
the commands read, so the shell never knows something the CLI cannot print.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Static

from proof_headless import __version__

EMPTY = (
    "Nothing is configured on this host yet.\n\n"
    "Run [b]proof --help[/b] to see what the appliance can do."
)


class ProofApp(App[None]):
    """The appliance at a glance."""

    TITLE = "proof"
    SUB_TITLE = f"Ethi Proof appliance {__version__}"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(EMPTY, id="body")
        yield Footer()
