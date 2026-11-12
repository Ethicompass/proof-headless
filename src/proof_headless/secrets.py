"""The operator's secrets file: names the appliance may read, values it never writes anywhere.

A dotenv file (`NAME=value` per line) readable only by its owner. `proof` refuses a file other
users can read rather than warn, because a token that leaked is not un-leaked by a warning. The
file is handed to `docker compose --env-file`, so compose substitutes `${NAME}` where a service
declares it and nowhere else; `proof` itself only ever asks which names are present.
"""

from __future__ import annotations

import stat
from pathlib import Path


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
        if name.strip() and value.strip().strip("'\"").strip():
            defined.add(name.strip())
    return frozenset(defined)
