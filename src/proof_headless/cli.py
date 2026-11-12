"""The `proof` command surface.

Typer parses, Rich renders every result, and `proof` with no command opens the Textual shell. Each
area of the appliance (models, stack, bundle, runs, lens, evidence) adds its own command group to
`app` as it lands, so this module stays the map of what the appliance can do.
"""

from __future__ import annotations

import json
import platform
import sys

import typer
from rich.table import Table

from proof_headless import __version__
from proof_headless.bundle.commands import app as bundle_app
from proof_headless.console import out
from proof_headless.doctor import doctor
from proof_headless.evidence.commands import app as evidence_app
from proof_headless.lens.commands import app as lens_app
from proof_headless.models.commands import app as models_app
from proof_headless.runs.commands import app as run_app
from proof_headless.secrets_commands import app as secrets_app
from proof_headless.spec.commands import app as spec_app
from proof_headless.stack.commands import app as stack_app

app = typer.Typer(
    name="proof",
    help="Run an Ethi Proof engagement on one host: models, stack, bundle, runs, evidence.",
    rich_markup_mode="rich",
    no_args_is_help=False,
    add_completion=False,
)


def interactive() -> bool:
    """Whether a person is at a terminal on both ends.

    A non-interactive SSH command (`ssh host proof`), a pipe or a CI job has no terminal to draw
    the shell on, and Textual would either fail or wait for keys nobody can press.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


app.add_typer(models_app, name="models")
app.add_typer(bundle_app, name="bundle")
app.add_typer(spec_app, name="spec")
app.add_typer(run_app, name="run")
app.add_typer(stack_app, name="stack")
app.add_typer(secrets_app, name="secrets")
app.add_typer(evidence_app, name="evidence")
app.add_typer(lens_app, name="lens")
app.command()(doctor)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    """Open the interactive shell when no command is given, or print the help without a terminal."""
    if ctx.invoked_subcommand is not None:
        return
    if not interactive():
        out.print(ctx.get_help())
        return
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
