"""The two consoles every command writes through.

Results go to standard output and everything addressed to the operator (progress, warnings,
refusals) goes to standard error, so `proof <command> --json | jq` reads clean JSON while the
operator still sees what happened. One instance of each, shared, so a test can swap them.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "fail": "bold red",
        "muted": "dim",
        "name": "bold cyan",
    }
)

out = Console(theme=THEME)
err = Console(theme=THEME, stderr=True)
