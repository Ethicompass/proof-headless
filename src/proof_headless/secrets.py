"""The operator's secrets file: names the appliance may read, values it never writes anywhere.

A dotenv file (`NAME=value` per line) readable only by its owner. `proof` refuses a file other
users can read rather than warn, because a token that leaked is not un-leaked by a warning. The
file is handed to `docker compose --env-file`, so compose substitutes `${NAME}` where a service
declares it and nowhere else. `proof` itself asks only which names are present, and
`proof secrets set` writes a value the operator typed without ever reading one back.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretsFileExposed(PermissionError):
    """The secrets file is readable or writable by someone other than its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        super().__init__(
            f"{path} has mode {stat.filemode(mode)}; other users can read it. "
            f"Run `chmod 600 {path}` and try again."
        )


def check_private(path: Path) -> None:
    """Refuse a secrets file that group or others can read or write."""
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretsFileExposed(path, mode)


def _value(raw: str) -> str:
    """A dotenv value as compose reads it, as far as deciding whether it is empty: a quoted value
    up to its closing quote, whatever comment follows; an unquoted one up to a ` #` comment.
    `$NAME` references are not expanded, so one counts as a value."""
    raw = raw.strip()
    if raw[:1] in {"'", '"'}:
        closing = raw.find(raw[0], 1)
        return raw[1:closing] if closing > 0 else raw[1:]
    if raw.startswith("#"):
        return ""
    return raw.split(" #", 1)[0].strip()


def names(path: Path) -> frozenset[str]:
    """The secret names the file defines with a non-empty value; the values are never returned.

    `NAME=` defines nothing: compose would substitute an empty string, and a gated download
    would fail minutes later with a 401 instead of being refused here. An absent file defines none.
    """
    if not path.exists():
        return frozenset()
    check_private(path)
    defined: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        if name.strip() and _value(value):
            defined.add(name.strip())
    return frozenset(defined)


class SecretRefused(ValueError):
    """A name or value the secrets file cannot hold safely."""


def _line_name(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    return line.removeprefix("export ").partition("=")[0].strip()


def write(path: Path, name: str, value: str) -> None:
    """Set `name` to `value` in the secrets file, replacing an earlier definition, keeping every
    other line as it was, and leaving the file at mode 600.

    The value is written single-quoted, because compose interpolates `$` in unquoted and
    double-quoted values and would hand a container a token with part of it blanked. A single
    quote cannot appear inside one, and a newline would end the line, so both are refused. The
    file is replaced through a temporary file created at mode 600 beside it, so there is no moment
    at which the new value sits in a file others can read.
    """
    if not NAME.match(name):
        raise SecretRefused(f"{name!r} is not a variable name (letters, digits and _).")
    if not value:
        raise SecretRefused(f"{name} would be empty; an empty secret defines nothing.")
    if "'" in value or "\n" in value or "\r" in value:
        raise SecretRefused(
            f"{name}'s value has a quote or a line break, which dotenv cannot hold."
        )
    lines: list[str] = []
    if path.exists():
        check_private(path)
        lines = [line for line in path.read_text().splitlines() if _line_name(line) != name]
    lines.append(f"{name}='{value}'")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".secrets-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
