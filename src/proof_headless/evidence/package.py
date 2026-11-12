"""The package's integrity: `SHA256SUMS`, the check that re-reads it, and the secret scan.

`SHA256SUMS` is in the format `sha256sum` writes, so a recipient can check the package with
`sha256sum -c SHA256SUMS` on any machine, without `proof`. It lists every file of the package but
itself. Verification also fails on a file the list does not name, because a file slipped in after
the export is as much a change to the evidence as an edited one.

The scan holds each value of the operator's secrets file in memory and nowhere else, and looks for
it in every file in the forms a value usually leaks in: as written, JSON-escaped, percent-encoded,
and base64 (standard and URL-safe, padded or not, at each of the three byte alignments). A zstd
file, which is how Yield stores its traces, is scanned decompressed, as a stream; one that does not
decompress cannot be scanned and refuses the package as surely as a hit. A hit names the file and
the secret, never the value. Values shorter than `MIN_SECRET_LENGTH` are not matched: a short
value (a default access key id, a user name) is a substring of ordinary text, and matching it
would refuse every package; such a secret is named as unscanned instead. Hex, and a value split
across fields, are not looked for.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, quote_plus

import zstandard

MIN_SECRET_LENGTH = 8
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

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
    """`changed`, `missing`, `unlisted`, `symlink` (a package holds none), or `malformed` for a
    line that does not read or names a path outside the package."""


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
        name = name.removeprefix("*")
        relative = Path(name)
        if (
            not separator
            or len(expected) != 64
            or not name
            or relative.is_absolute()
            or name.startswith(("/", "\\"))
            or ".." in relative.parts
        ):
            problems.append(Mismatch(f"{SUMS}:{number}", "malformed"))
            continue
        listed[name] = expected
    for name, expected in listed.items():
        path = root / name
        if path.is_symlink():
            continue
        if not path.is_file():
            problems.append(Mismatch(name, "missing"))
        elif digest(path) != expected:
            problems.append(Mismatch(name, "changed"))
    for present in files(root):
        posix = present.as_posix()
        if posix != SUMS and posix not in listed:
            problems.append(Mismatch(posix, "unlisted"))
    for link in sorted(root.rglob("*")):
        if link.is_symlink():
            problems.append(Mismatch(link.relative_to(root).as_posix(), "symlink"))
    return problems


@dataclass(frozen=True)
class Leak:
    """A secret's value found in an exported file. Carries the secret's name, never its value."""

    path: str
    secret: str


@dataclass
class Findings:
    """What the scan found: leaks, files it could not read, secrets too short to look for."""

    leaks: list[Leak] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    """Files that claim to be zstd and do not decompress, so were never scanned."""
    unscanned: list[str] = field(default_factory=list)
    """Secret names whose value is shorter than `MIN_SECRET_LENGTH`."""

    @property
    def clean(self) -> bool:
        return not self.leaks and not self.unreadable


def _base64(raw: bytes) -> set[bytes]:
    """The part of `raw`'s base64 that does not depend on what precedes or follows it, at each of
    the three alignments it can start at, standard and URL-safe. Without padding, so it matches a
    padded and an unpadded encoding alike."""
    forms: set[bytes] = set()
    for lead, skip in ((b"", 0), (b"x", 2), (b"xx", 3)):
        size = len(lead) + len(raw)
        encoded = base64.b64encode(lead + raw).rstrip(b"=")
        if size % 3:
            encoded = encoded[:-1]
        middle = encoded[skip:]
        if middle:
            forms.add(middle)
            forms.add(middle.replace(b"+", b"-").replace(b"/", b"_"))
    return forms


def forms(value: str) -> set[bytes]:
    """Every byte string the scan looks for when it looks for `value`."""
    raw = value.encode()
    found = {
        raw,
        json.dumps(value)[1:-1].encode(),
        json.dumps(value, ensure_ascii=False)[1:-1].encode(),
        quote(value, safe="").encode(),
        quote_plus(value, safe="").encode(),
    }
    return found | _base64(raw)


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    while chunk := handle.read(_CHUNK):
        yield chunk


def _contains(chunks: Iterable[bytes], patterns: Mapping[str, set[bytes]]) -> set[str]:
    """The names whose patterns occur in the stream, read chunk by chunk with an overlap one byte
    shorter than the longest pattern, so a value split across two chunks is still found."""
    overlap = max((len(form) for group in patterns.values() for form in group), default=1) - 1
    found: set[str] = set()
    tail = b""
    for chunk in chunks:
        window = tail + chunk
        for name, group in patterns.items():
            if name not in found and any(form in window for form in group):
                found.add(name)
        tail = window[-overlap:] if overlap else b""
    return found


def scan(root: Path, secrets: Mapping[str, str]) -> Findings:
    """Every (file, secret name) pair where the secret's value appears in the file, every zstd file
    that does not decompress, and every secret too short to look for."""
    findings = Findings()
    patterns: dict[str, set[bytes]] = {}
    for name, value in sorted(secrets.items()):
        if not value:
            continue
        if len(value) < MIN_SECRET_LENGTH:
            findings.unscanned.append(name)
            continue
        patterns[name] = forms(value)
    for relative in files(root):
        path = root / relative
        with path.open("rb") as handle:
            compressed = handle.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC
            handle.seek(0)
            if compressed or relative.suffix == ".zst":
                try:
                    reader = zstandard.ZstdDecompressor().stream_reader(
                        handle, read_across_frames=True
                    )
                    hits = _contains(_chunks(reader), patterns)
                except zstandard.ZstdError:
                    findings.unreadable.append(relative.as_posix())
                    continue
            else:
                hits = _contains(_chunks(handle), patterns)
        findings.leaks += [Leak(relative.as_posix(), name) for name in sorted(hits)]
    return findings
