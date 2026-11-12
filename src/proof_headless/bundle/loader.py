"""Publishing a bundle into a running appliance, and the receipt that says what was published.

Each piece goes where its service reads it: catalogues, profiles, priors and mappings to Yield's
compute (`POST /catalogues`, `/profiles`, `/priors`, `/mappings`, the same bodies Yield's seed
sends), brains to the appliance's registry through `vitruvio dist push`, declarations into the
directory Lens's declaration source serves. Compute answers an unchanged asset with the version it
already holds, so loading a bundle twice publishes nothing new.

The receipt records every piece's version or digest. It is what `proof spec render` pins run specs
to, and part of the engagement's evidence: which catalogue version measured, which brain digest
grounded the probes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from proof_headless.bundle.manifest import Brain, Bundle, Entry

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Loaded:
    """One published piece."""

    kind: str
    name: str
    version: int | None = None
    digest: str | None = None
    created: bool | None = None


@dataclass
class Receipt:
    """What a load published, in order."""

    client: str
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    loaded: list[Loaded] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "at": self.at,
            "loaded": [asdict(item) for item in self.loaded],
        }


class LoadFailed(RuntimeError):
    """A service refused a piece of the bundle, or could not be reached."""


def _post(client: httpx.Client, path: str, body: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        response = client.post(path, json=body)
    except httpx.HTTPError as error:
        raise LoadFailed(f"{what}: compute could not be reached: {error}") from None
    if response.status_code >= 400:
        raise LoadFailed(f"{what}: compute answered {response.status_code}: {response.text[:500]}")
    answer: dict[str, Any] = response.json()
    return answer


def catalogue_body(entry: Entry) -> dict[str, Any]:
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
            body[key] = value["needs_base"] if key == "needs_base" else value
    return body


def push_brain(brain: Brain, registry: str, runner: Runner) -> Loaded:
    """`vitruvio dist push` the brain to `registry/<repository>` and read back its digest."""
    reference = f"{registry}/{brain.repository}"
    args = [
        "vitruvio",
        "dist",
        "push",
        "--config",
        str(brain.config),
        "--brain",
        brain.name,
        "--json",
        reference,
    ]
    result = runner(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise LoadFailed(f"brain {brain.name}: vitruvio exited {result.returncode}: {detail}")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise LoadFailed(f"brain {brain.name}: vitruvio answered no JSON") from None
    data = envelope.get("data", envelope) if isinstance(envelope, dict) else {}
    digest = data.get("digest") or data.get("manifest_digest") if isinstance(data, dict) else None
    return Loaded("brain", brain.name, digest=str(digest) if digest else None)


def load(
    bundle: Bundle,
    client: httpx.Client,
    *,
    registry: str,
    declarations_dir: Path,
    runner: Runner,
    skip_brains: bool = False,
    progress: Callable[[str], None] = lambda _: None,
) -> Receipt:
    """Publish every piece of `bundle`, stopping at the first refusal."""
    receipt = Receipt(client=bundle.client)

    def published(kind: str, name: str, answer: dict[str, Any]) -> None:
        receipt.loaded.append(
            Loaded(kind, name, answer.get("version"), answer.get("digest"), answer.get("created"))
        )

    for entry in bundle.catalogues:
        progress(f"catalogue {entry.name}")
        published(
            "catalogue",
            entry.name,
            _post(client, "/catalogues", catalogue_body(entry), f"catalogue {entry.name}"),
        )
    for entry in bundle.profiles:
        progress(f"profile {entry.name}")
        body = {"name": entry.name, "profile": json.loads(entry.path.read_text())}
        published("profile", entry.name, _post(client, "/profiles", body, f"profile {entry.name}"))
    for entry in bundle.priors:
        progress(f"prior {entry.name}")
        body = {"name": entry.name, "phrasings": json.loads(entry.path.read_text())}
        published("prior", entry.name, _post(client, "/priors", body, f"prior {entry.name}"))
    for entry in bundle.mappings:
        progress(f"mapping {entry.name}")
        body = {"name": entry.name, "mapping": json.loads(entry.path.read_text())}
        published("mapping", entry.name, _post(client, "/mappings", body, f"mapping {entry.name}"))
    if not skip_brains:
        for brain in bundle.brains:
            progress(f"brain {brain.name}")
            receipt.loaded.append(push_brain(brain, registry, runner))
    declarations_dir.mkdir(parents=True, exist_ok=True)
    for declaration in bundle.declarations:
        progress(f"declaration {declaration.path.name[:12]}")
        shutil.copyfile(declaration.path, declarations_dir / declaration.path.name)
        receipt.loaded.append(
            Loaded("declaration", declaration.path.name, digest=f"sha256:{declaration.path.name}")
        )
    return receipt
