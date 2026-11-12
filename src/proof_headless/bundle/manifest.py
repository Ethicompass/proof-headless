"""`bundle.toml`: what a client's bundle holds, and where in the bundle each piece is.

A bundle is a directory the operator brings to the appliance: catalogues, target profiles, prior
phrasings, dimension mappings, brains, Lens declarations and a run matrix, plus the names (never
the values) of the secrets they need. Paths in the manifest are relative to the bundle's directory.
Nothing here is specific to any client; a client is the data in a bundle (ADR 0001).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from proof_headless.models.manifest import NAME_PATTERN

MANIFEST = "bundle.toml"


class Entry(BaseModel):
    """One named file or directory in the bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    path: Path


class Catalogue(Entry):
    """A catalogue directory, and the behavioral contract compute validates it against."""

    contract: str = Field(default="pi", min_length=1)


class Brain(BaseModel):
    """A vitruvio brain to publish to the appliance's registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    """The brain's name in its project's `vitruvio.toml`."""
    config: Path
    """The project's `vitruvio.toml`."""
    repository: str = Field(min_length=1)
    """`<namespace>/<repo>` under the appliance's registry."""


class Declaration(BaseModel):
    """A Lens target declaration, named by the hex of its SHA-256 the way Lens addresses it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path


class Bundle(BaseModel):
    """A client's bundle, its paths resolved against the bundle's directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: str = Field(pattern=NAME_PATTERN)
    language: str = Field(min_length=2)
    context: dict[str, str] = Field(default_factory=dict)
    """The run context Yield's generator and attackers write in: `domain`, `tone`, ..."""
    secrets: tuple[str, ...] = ()
    """Names the bundle's specs reference by `secret_ref`; each must be in the secrets file."""
    catalogues: tuple[Catalogue, ...] = ()
    profiles: tuple[Entry, ...] = ()
    priors: tuple[Entry, ...] = ()
    mappings: tuple[Entry, ...] = ()
    brains: tuple[Brain, ...] = ()
    declarations: tuple[Declaration, ...] = ()
    matrix: Path | None = None
    """The run matrix `proof spec render` reads."""

    root: Path = Path()
    """The bundle's directory. Set by `load`, not written in the manifest."""


class BundleInvalid(ValueError):
    """The bundle's manifest does not parse or does not validate."""


def _resolve(raw: dict[str, object], root: Path) -> dict[str, object]:
    def under(value: object) -> object:
        return str((root / str(value)).resolve())

    resolved = dict(raw)
    for key in ("catalogues", "profiles", "priors", "mappings", "declarations"):
        items = resolved.get(key)
        if isinstance(items, list):
            resolved[key] = [
                {**item, "path": under(item["path"])}
                if isinstance(item, dict) and "path" in item
                else item
                for item in items
            ]
    brains = resolved.get("brains")
    if isinstance(brains, list):
        resolved["brains"] = [
            {**item, "config": under(item["config"])}
            if isinstance(item, dict) and "config" in item
            else item
            for item in brains
        ]
    if "matrix" in resolved:
        resolved["matrix"] = under(resolved["matrix"])
    return resolved


def load(directory: Path) -> Bundle:
    """Read `directory/bundle.toml`; every path in it resolves against `directory`."""
    path = directory / MANIFEST
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise BundleInvalid(f"no {MANIFEST} in {directory}") from None
    except tomllib.TOMLDecodeError as error:
        raise BundleInvalid(f"{path} is not TOML: {error}") from None
    root = directory.resolve()
    try:
        return Bundle.model_validate({**_resolve(raw, root), "root": str(root)})
    except ValueError as error:
        raise BundleInvalid(f"{path}: {error}") from None
