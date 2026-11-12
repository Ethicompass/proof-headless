"""What `proof bundle validate` checks before anything is published.

Each check reads the bundle the way the service that will receive it does, so a problem is found
on the operator's machine instead of as a 422 halfway through a load: the files exist and parse,
a catalogue directory has its `catalogue.json` and well-formed sidecars, a prior is a non-empty
list of phrasings, a declaration is named by its own digest, and every secret the bundle needs is
in the secrets file. The service-side checks (the catalogue's six semantic rejections, a profile's
placeholders) still run when it is published; these are the ones that need no service.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proof_headless.bundle.manifest import Bundle

SIDECARS = ("pairs.json", "delivery.json", "grounding.json")


@dataclass(frozen=True)
class Problem:
    """One thing wrong with the bundle, and where."""

    where: str
    what: str


def _json(path: Path, where: str, problems: list[Problem]) -> Any:
    if not path.is_file():
        problems.append(Problem(where, f"missing file {path}"))
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        problems.append(Problem(where, f"{path.name} is not JSON: {error}"))
        return None


def check(bundle: Bundle, secret_names: frozenset[str]) -> list[Problem]:
    """Every problem found, in manifest order; empty means the bundle may be loaded."""
    problems: list[Problem] = []
    for entry in bundle.catalogues:
        where = f"catalogue {entry.name}"
        if not entry.path.is_dir():
            problems.append(Problem(where, f"not a directory: {entry.path}"))
            continue
        catalogue = _json(entry.path / "catalogue.json", where, problems)
        if catalogue is not None and not isinstance(catalogue, dict):
            problems.append(Problem(where, "catalogue.json must be an object"))
        for sidecar in SIDECARS:
            if (entry.path / sidecar).exists():
                _json(entry.path / sidecar, where, problems)
    for entry in bundle.profiles:
        profile = _json(entry.path, f"profile {entry.name}", problems)
        if profile is not None and not isinstance(profile, dict):
            problems.append(Problem(f"profile {entry.name}", "must be a JSON object"))
    for entry in bundle.priors:
        phrasings = _json(entry.path, f"prior {entry.name}", problems)
        if phrasings is not None and not (
            isinstance(phrasings, list) and phrasings and all(isinstance(p, str) for p in phrasings)
        ):
            problems.append(Problem(f"prior {entry.name}", "must be a non-empty list of strings"))
    for entry in bundle.mappings:
        mapping = _json(entry.path, f"mapping {entry.name}", problems)
        if mapping is not None and not isinstance(mapping, dict):
            problems.append(Problem(f"mapping {entry.name}", "must be a JSON object"))
    for brain in bundle.brains:
        if not brain.config.is_file():
            problems.append(Problem(f"brain {brain.name}", f"missing {brain.config}"))
    for declaration in bundle.declarations:
        where = f"declaration {declaration.path.name}"
        if not declaration.path.is_file():
            problems.append(Problem(where, f"missing file {declaration.path}"))
            continue
        digest = hashlib.sha256(declaration.path.read_bytes()).hexdigest()
        if declaration.path.name != digest:
            problems.append(Problem(where, f"must be named by its SHA-256, {digest}"))
    if bundle.matrix is not None and not bundle.matrix.is_file():
        problems.append(Problem("matrix", f"missing file {bundle.matrix}"))
    for name in bundle.secrets:
        if name not in secret_names:
            problems.append(Problem(f"secret {name}", "not in the secrets file, or empty"))
    return problems
