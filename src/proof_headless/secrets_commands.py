"""`proof secrets`: put a credential in the operator's secrets file, and say which names it holds.

A value arrives through a hidden prompt, or on standard input for a script
(`pass show x | proof secrets set NAME`), never as an argument: an argument lands in the shell's
history and in `ps` for every user of the host. Nothing here prints a value, including back to
the operator who typed it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable

import typer
from rich.table import Table

from proof_headless import paths, secrets
from proof_headless.console import err, out

app = typer.Typer(
    help="The operator's secrets file: set a value, list the names.", no_args_is_help=True
)


def present() -> frozenset[str]:
    """The names the secrets file defines, or a refusal when other users can read it: the one
    check every command that hands the file to compose makes first."""
    try:
        return secrets.names(paths.secrets_file())
    except secrets.SecretsFileExposed as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None


def require(needed: Iterable[str], why: str) -> None:
    """Refuse unless the secrets file defines every name in `needed`, saying who needs them."""
    missing = sorted(set(needed) - present())
    if missing:
        err.print(
            f"[fail]Refused.[/fail] {why} need {missing}, and {paths.secrets_file()} does not "
            "define them. Add each with `proof secrets set NAME`."
        )
        raise typer.Exit(2)


def _read_value(name: str) -> str:
    if sys.stdin.isatty():
        value: str = typer.prompt(f"{name}", hide_input=True, confirmation_prompt=True)
        return value
    return sys.stdin.read().rstrip("\r\n")


@app.command(name="set")
def set_secret(name: str) -> None:
    """Set NAME in the secrets file, reading its value from a hidden prompt or standard input."""
    path = paths.secrets_file()
    try:
        secrets.write(path, name, _read_value(name))
    except (secrets.SecretRefused, secrets.SecretsFileExposed) as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None
    err.print(f"[ok]{name} set[/ok] in {path}. A running stack reads it on its next `up`.")


@app.command(name="list")
def list_secrets(
    as_json: bool = typer.Option(False, "--json", help="Print the names as JSON."),
) -> None:
    """List the names the secrets file defines. Never their values."""
    path = paths.secrets_file()
    defined = sorted(present())
    if as_json:
        out.print_json(json.dumps({"file": str(path), "names": defined}))
        return
    table = Table(title=str(path), title_justify="left")
    table.add_column("name")
    for name in defined:
        table.add_row(f"[name]{name}[/name]")
    out.print(table)
    if not defined:
        err.print("[muted]No secrets defined.[/muted]")
