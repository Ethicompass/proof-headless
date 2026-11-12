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
DECLARATION_VERSION = "lens_declaration/"


def canonical_digest(payload: Any) -> str:
    """The hex SHA-256 Lens names a declaration by: of its canonical JSON (sorted keys, compact
    separators, UTF-8 unescaped), not of the file's bytes, so reformatting a file never renames it.
    The definition is Lens's `lens_contracts.digest.canonical_json`."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _paths(bundle: Bundle) -> list[tuple[str, Path]]:
    named: list[tuple[str, Path]] = [(f"catalogue {e.name}", e.path) for e in bundle.catalogues]
    named += [(f"profile {e.name}", e.path) for e in bundle.profiles]
    named += [(f"prior {e.name}", e.path) for e in bundle.priors]
    named += [(f"mapping {e.name}", e.path) for e in bundle.mappings]
    named += [(f"brain {b.name}", b.config) for b in bundle.brains]
    named += [(f"declaration {d.path.name}", d.path) for d in bundle.declarations]
    if bundle.matrix is not None:
        named.append(("matrix", bundle.matrix))
    return named


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
    for where, path in _paths(bundle):
        if not path.resolve().is_relative_to(bundle.root):
            problems.append(Problem(where, f"{path} is outside the bundle"))
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
                value = _json(entry.path / sidecar, where, problems)
                if (
                    sidecar == "grounding.json"
                    and value is not None
                    and (not isinstance(value, dict) or "needs_base" not in value)
                ):
                    problems.append(Problem(where, "grounding.json must have `needs_base`"))
    for profile_entry in bundle.profiles:
        profile = _json(profile_entry.path, f"profile {profile_entry.name}", problems)
        if profile is not None and not isinstance(profile, dict):
            problems.append(Problem(f"profile {profile_entry.name}", "must be a JSON object"))
    for prior_entry in bundle.priors:
        phrasings = _json(prior_entry.path, f"prior {prior_entry.name}", problems)
        if phrasings is not None and not (
            isinstance(phrasings, list) and phrasings and all(isinstance(p, str) for p in phrasings)
        ):
            problems.append(
                Problem(f"prior {prior_entry.name}", "must be a non-empty list of strings")
            )
    for mapping_entry in bundle.mappings:
        mapping = _json(mapping_entry.path, f"mapping {mapping_entry.name}", problems)
        if mapping is not None and not isinstance(mapping, dict):
            problems.append(Problem(f"mapping {mapping_entry.name}", "must be a JSON object"))
    for brain in bundle.brains:
        if not brain.config.is_file():
            problems.append(Problem(f"brain {brain.name}", f"missing {brain.config}"))
    for declaration in bundle.declarations:
        where = f"declaration {declaration.path.name}"
        if not declaration.path.is_file():
            problems.append(Problem(where, f"missing file {declaration.path}"))
            continue
        payload = _json(declaration.path, where, problems)
        if payload is None:
            continue
        version = payload.get("contract_version", "") if isinstance(payload, dict) else ""
        if not str(version).startswith(DECLARATION_VERSION):
            problems.append(Problem(where, f"not a Lens declaration ({DECLARATION_VERSION}...)"))
        digest = canonical_digest(payload)
        if declaration.path.name != digest:
            problems.append(Problem(where, f"must be named by its canonical digest, {digest}"))
    if bundle.matrix is not None and not bundle.matrix.is_file():
        problems.append(Problem("matrix", f"missing file {bundle.matrix}"))
    for name in bundle.secrets:
        if name not in secret_names:
            problems.append(Problem(f"secret {name}", "not in the secrets file, or empty"))
    return problems
