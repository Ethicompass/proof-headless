"""The `proof` command surface.

Typer parses, Rich renders every result, and `proof` with no command opens the Textual shell. Each
area of the appliance (models, stack, bundle, runs, lens, evidence) adds its own command group to
`app` as it lands, so this module stays the map of what the appliance can do.
"""

from __future__ import annotations

import json
import platform

import typer
from rich.table import Table

from proof_headless import __version__
from proof_headless.console import out

app = typer.Typer(
    name="proof",
    help="Run an Ethi Proof engagement on one host: models, stack, bundle, runs, evidence.",
    rich_markup_mode="rich",
    no_args_is_help=False,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    """Open the interactive shell when no command is given."""
    if ctx.invoked_subcommand is None:
        from proof_headless.tui.app import ProofApp

        ProofApp().run()


@app.command()
def version(
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show the version of proof and of the host it runs on."""
    facts = {
        "proof": __version__,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
    }
    if as_json:
        out.print_json(json.dumps(facts))
        return
    table = Table(show_header=False, box=None)
    for key, value in facts.items():
        table.add_row(f"[name]{key}[/name]", value)
    out.print(table)


def main() -> None:
    """The console script's entry point."""
    app()
