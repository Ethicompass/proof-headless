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
    return frozenset(values(path))


def values(path: Path) -> dict[str, str]:
    """Every secret the file defines with a non-empty value, by name.

    For the two places a value has to be held in memory: the evidence scan, which refuses a
    package that contains one, and the store credentials an export signs its requests with. A
    caller never prints, logs or writes what this returns; it names a secret by its key.
    """
    if not path.exists():
        return {}
    check_private(path)
    defined: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        parsed = _value(value)
        if name.strip() and parsed:
            defined[name.strip()] = parsed
    return defined
