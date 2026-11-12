"""Publishing a bundle into a running appliance, and the receipt that says what was published.

Each piece goes where its service reads it: catalogues, profiles, priors and mappings to Yield's
compute (`POST /catalogues?contract=...`, `/profiles`, `/priors`, `/mappings`, the bodies Yield's
seed sends), brains to the appliance's registry through `vitruvio dist push`, declarations into the
directory Lens's declaration source serves. Compute answers an unchanged asset with the version it
already holds, so loading a bundle twice publishes nothing new.

The receipt records every piece's version or digest, and whether the load completed. It is what
`proof spec render` pins run specs to, and part of the engagement's evidence. A load that stops
part way still writes one, marked incomplete with the reason, because the pieces it did publish
are live in the services and a receipt that predates them would claim otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from proof_headless.bundle.manifest import Brain, Bundle, Catalogue

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class Piece(StrEnum):
    """The kinds of piece a receipt names. A `StrEnum`, so the receipt's JSON keeps plain strings
    while every reader and writer spells the kind once: a typo is a type error, not a piece
    reported missing."""

    CATALOGUE = "catalogue"
    PROFILE = "profile"
    PRIOR = "prior"
    MAPPING = "mapping"
    BRAIN = "brain"
    DECLARATION = "declaration"


@dataclass(frozen=True)
class Loaded:
    """One published piece. `carried` marks one this load did not publish but kept from the last."""

    kind: str
    name: str
    version: int | None = None
    digest: str | None = None
    created: bool | None = None
    carried: bool = False


@dataclass
class Receipt:
    """What a load published, in order, and whether it got to the end."""

    client: str
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    loaded: list[Loaded] = field(default_factory=list)
    complete: bool = False
    error: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "at": self.at,
            "complete": self.complete,
            "error": self.error,
            "loaded": [asdict(item) for item in self.loaded],
        }


class LoadFailed(RuntimeError):
    """A service refused a piece of the bundle, could not be reached, or answered nonsense."""


def _post(client: httpx.Client, path: str, body: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        response = client.post(path, json=body)
    except httpx.HTTPError as error:
        raise LoadFailed(f"{what}: compute could not be reached: {error}") from None
    if response.status_code >= 400:
        raise LoadFailed(f"{what}: compute answered {response.status_code}: {response.text[:500]}")
    try:
        answer = response.json()
    except json.JSONDecodeError:
        raise LoadFailed(f"{what}: compute answered no JSON: {response.text[:200]}") from None
    if not isinstance(answer, dict):
        raise LoadFailed(f"{what}: compute answered {type(answer).__name__}, not an object")
    return answer


def catalogue_body(entry: Catalogue) -> dict[str, Any]:
    """The body compute's `POST /catalogues` takes, sidecars included when present."""
    body: dict[str, Any] = {
        "name": entry.name,
        "catalogue": json.loads((entry.path / "catalogue.json").read_text()),
    }
    for filename, key in (
        ("pairs", "pairs"),
        ("delivery", "delivery"),
        ("grounding", "needs_base"),
    ):
        sidecar = entry.path / f"{filename}.json"
        if sidecar.is_file():
            value = json.loads(sidecar.read_text())
            if key == "needs_base":
                if not isinstance(value, dict) or "needs_base" not in value:
                    raise LoadFailed(f"catalogue {entry.name}: grounding.json has no needs_base")
                value = value["needs_base"]
            body[key] = value
    return body


def push_brain(brain: Brain, registry: str, runner: Runner, *, insecure: bool | None) -> Loaded:
    """`vitruvio dist push` the brain to `registry/<repository>` and read back its digest.

    `insecure` `None` passes neither flag, so the brain's own `[registry].insecure` decides.
    """
    args = ["vitruvio", "dist", "push", "--config", str(brain.config), "--brain", brain.name]
    if insecure is not None:
        args.append("--insecure" if insecure else "--no-insecure")
    args.append("--json")
    args.append(f"{registry}/{brain.repository}")
    result = runner(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise LoadFailed(f"brain {brain.name}: vitruvio exited {result.returncode}: {detail}")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise LoadFailed(f"brain {brain.name}: vitruvio answered no JSON") from None
    data = envelope.get("data") if isinstance(envelope, dict) else None
    digest = data.get("digest") if isinstance(data, dict) else None
    if not digest:
        raise LoadFailed(f"brain {brain.name}: vitruvio's answer carries no digest")
    return Loaded(Piece.BRAIN, brain.name, digest=str(digest))


def load(
    bundle: Bundle,
    client: httpx.Client,
    *,
    registry: str,
    declarations_dir: Path,
    runner: Runner,
    insecure: bool | None,
    previous: Receipt | None = None,
    skip_brains: bool = False,
    progress: Callable[[str], None] = lambda _: None,
) -> Receipt:
    """Publish every piece of `bundle`, stopping at the first refusal.

    The returned receipt is complete, or carries the refusal as its error. A stopped load keeps
    `previous`'s entries for the pieces it did not reach, marked `carried`: those are still live in
    the services, and a receipt without them would lose digests a run pins. With `skip_brains`,
    every brain the bundle declares must have a digest in `previous`, carried over, or the load
    stops: a complete receipt always pins every brain.
    """
    receipt = Receipt(client=bundle.client)

    def published(kind: Piece, name: str, answer: dict[str, Any]) -> None:
        receipt.loaded.append(
            Loaded(kind, name, answer.get("version"), answer.get("digest"), answer.get("created"))
        )

    try:
        for catalogue in bundle.catalogues:
            progress(f"catalogue {catalogue.name}")
            what = f"catalogue {catalogue.name}"
            route = f"/catalogues?contract={catalogue.contract}"
            published(
                Piece.CATALOGUE,
                catalogue.name,
                _post(client, route, catalogue_body(catalogue), what),
            )
        for entry in bundle.profiles:
            progress(f"profile {entry.name}")
            body = {"name": entry.name, "profile": json.loads(entry.path.read_text())}
            published(
                Piece.PROFILE, entry.name, _post(client, "/profiles", body, f"profile {entry.name}")
            )
        for entry in bundle.priors:
            progress(f"prior {entry.name}")
            body = {"name": entry.name, "phrasings": json.loads(entry.path.read_text())}
            published(
                Piece.PRIOR, entry.name, _post(client, "/priors", body, f"prior {entry.name}")
            )
        for entry in bundle.mappings:
            progress(f"mapping {entry.name}")
            body = {"name": entry.name, "mapping": json.loads(entry.path.read_text())}
            published(
                Piece.MAPPING, entry.name, _post(client, "/mappings", body, f"mapping {entry.name}")
            )
        if skip_brains:
            earlier = previous.loaded if previous else []
            kept = {i.name: i for i in earlier if i.kind == Piece.BRAIN}
            for brain in bundle.brains:
                last = kept.get(brain.name)
                if last is None or not last.digest:
                    raise LoadFailed(
                        f"brain {brain.name}: --skip-brains needs a digest from an earlier load, "
                        f"and there is none; load once without --skip-brains"
                    )
                carried = Loaded(Piece.BRAIN, brain.name, digest=last.digest, carried=True)
                receipt.loaded.append(carried)
        else:
            for brain in bundle.brains:
                progress(f"brain {brain.name}")
                receipt.loaded.append(push_brain(brain, registry, runner, insecure=insecure))
        declarations_dir.mkdir(parents=True, exist_ok=True)
        for declaration in bundle.declarations:
            progress(f"declaration {declaration.path.name[:12]}")
            shutil.copyfile(declaration.path, declarations_dir / declaration.path.name)
            digest = f"sha256:{declaration.path.name}"
            receipt.loaded.append(Loaded(Piece.DECLARATION, declaration.path.name, digest=digest))
    except LoadFailed as error:
        receipt.error = str(error)
        reached = {(item.kind, item.name) for item in receipt.loaded}
        declared = _declared(bundle)
        receipt.loaded += [
            replace(item, carried=True)
            for item in (previous.loaded if previous else [])
            if (item.kind, item.name) not in reached and (item.kind, item.name) in declared
        ]
        return receipt
    receipt.complete = True
    return receipt


def _declared(bundle: Bundle) -> set[tuple[str, str]]:
    """Every piece the bundle declares now, as the receipt names them."""
    pieces: set[tuple[str, str]] = {(Piece.CATALOGUE, c.name) for c in bundle.catalogues}
    pieces |= {(Piece.PROFILE, e.name) for e in bundle.profiles}
    pieces |= {(Piece.PRIOR, e.name) for e in bundle.priors}
    pieces |= {(Piece.MAPPING, e.name) for e in bundle.mappings}
    pieces |= {(Piece.BRAIN, b.name) for b in bundle.brains}
    pieces |= {(Piece.DECLARATION, d.path.name) for d in bundle.declarations}
    return pieces


def read_receipt(path: Path) -> Receipt | None:
    """The receipt at `path`, or `None` when there is none."""
    if not path.is_file():
        return None
    document = json.loads(path.read_text())
    return Receipt(
        client=document["client"],
        at=document["at"],
        loaded=[Loaded(**item) for item in document["loaded"]],
        complete=bool(document.get("complete", True)),
        error=document.get("error"),
    )
