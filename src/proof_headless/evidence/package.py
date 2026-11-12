"""The package's integrity: `SHA256SUMS`, the check that re-reads it, and the secret scan.

`SHA256SUMS` is in the format `sha256sum` writes, so a recipient can check the package with
`sha256sum -c SHA256SUMS` on any machine, without `proof`. It lists every file of the package but
itself. Verification also fails on a file the list does not name, because a file slipped in after
the export is as much a change to the evidence as an edited one.

The scan holds each value of the operator's secrets file in memory and nowhere else, and looks for
it in every file, as written and as it reads inside a JSON string. A hit names the file and the
secret, never the value. Compressed objects (Yield's traces are zstd) are scanned as stored: a value
inside the compressed stream is not seen, and the package's README says so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

SUMS = "SHA256SUMS"
_CHUNK = 1 << 20


def files(root: Path) -> Iterator[Path]:
    """Every regular file under `root`, by relative path, in sorted order."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path.relative_to(root)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_sums(root: Path) -> Path:
    """Write `SHA256SUMS` over every other file under `root`."""
    lines = [
        f"{digest(root / name)}  {name.as_posix()}\n"
        for name in files(root)
        if name.as_posix() != SUMS
    ]
    path = root / SUMS
    path.write_text("".join(lines))
    return path


@dataclass(frozen=True)
class Mismatch:
    """One way the package differs from its `SHA256SUMS`."""

    path: str
    problem: str
    """`changed`, `missing`, `unlisted`, or `malformed` for a line that does not read."""


def verify(root: Path) -> list[Mismatch]:
    """Every file whose digest differs from the list, is missing, or is not listed."""
    sums = root / SUMS
    if not sums.is_file():
        return [Mismatch(SUMS, "missing")]
    listed: dict[str, str] = {}
    problems: list[Mismatch] = []
    for number, line in enumerate(sums.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        expected, separator, name = line.partition("  ")
        if not separator or len(expected) != 64 or not name or ".." in Path(name).parts:
            problems.append(Mismatch(f"{SUMS}:{number}", "malformed"))
            continue
        listed[name.removeprefix("*")] = expected
    for name, expected in listed.items():
        path = root / name
        if not path.is_file():
            problems.append(Mismatch(name, "missing"))
        elif digest(path) != expected:
            problems.append(Mismatch(name, "changed"))
    for present in files(root):
        posix = present.as_posix()
        if posix != SUMS and posix not in listed:
            problems.append(Mismatch(posix, "unlisted"))
    return problems


@dataclass(frozen=True)
class Leak:
    """A secret's value found in an exported file. Carries the secret's name, never its value."""

    path: str
    secret: str


def _forms(value: str) -> set[bytes]:
    """The value as written, and as it reads inside a JSON string when escaping changes it."""
    return {
        value.encode(),
        json.dumps(value)[1:-1].encode(),
        json.dumps(value, ensure_ascii=False)[1:-1].encode(),
    }


def scan(root: Path, secrets: Mapping[str, str]) -> list[Leak]:
    """Every (file, secret name) pair where the secret's value appears in the file."""
    patterns = {name: _forms(value) for name, value in secrets.items() if value}
    leaks: list[Leak] = []
    for name in files(root):
        data = (root / name).read_bytes()
        for secret, forms in sorted(patterns.items()):
            if any(form in data for form in forms):
                leaks.append(Leak(name.as_posix(), secret))
    return leaks
