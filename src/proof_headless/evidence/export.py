"""Writing the package: collect into a staging directory, scan it, then seal it in place.

Nothing reaches the destination until the scan passed and `SHA256SUMS` was written: the package is
gathered in a sibling directory and renamed into place as a whole, so a refused or interrupted
export leaves no half-package that could be mistaken for the evidence. A refused one is deleted
with everything it had copied.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from proof_headless.evidence import package
from proof_headless.evidence.collect import Collector
from proof_headless.evidence.package import Leak

LAYOUT: tuple[tuple[str, str], ...] = (
    ("index.json", "The client, when and by which `proof` it was exported, each run, every gap"),
    ("SHA256SUMS", "The SHA-256 of every other file: `sha256sum -c` or `proof evidence verify`"),
    ("bundle/receipt.json", "What the bundle's last load published: versions and digests"),
    ("bundle/specs/<run>.json", "The Yield run specs as rendered and submitted"),
    ("bundle/queue.json", "The queue state, when the runs were queued with `proof run queue`"),
    ("appliance/models.yaml", "The models manifest the host served"),
    ("appliance/models-compose.yaml", "The compose file the models ran with"),
    ("appliance/appliance.lock", "The stack's image digests, when `proof stack` wrote one"),
    ("appliance/stack/", "The stack's rendered compose files, when present"),
    ("appliance/images.json", "Every running container's image, image id and repository digests"),
    ("appliance/vllm-versions.json", "What each model server answered at `/version`"),
    ("doctor/doctor-*.json", "Every `proof doctor` probe report"),
    ("runs/<run>/yield/status.json", "Yield's status of the run (`GET /runs/{id}`)"),
    ("runs/<run>/yield/manifest.json", "Yield's manifest (`GET /runs/{id}/result`)"),
    (
        "runs/<run>/yield/store/",
        "The run's objects in Yield's store: spec, traces (`jsonl.zst`), dataset, sessions, and "
        "metric outputs not yet delivered",
    ),
    ("runs/<run>/lens/status.json", "Lens's status of the run the delivery derived"),
    ("runs/<run>/lens/result.json", "The Lens result (`GET /artifacts/{result_ref}`)"),
)

NOTES = """\
Secrets: the export was refused if any value of the operator's secrets file appeared in any file,
as written or JSON-escaped. Traces are stored zstd-compressed and were scanned as stored, so a
value inside a compressed stream is not covered by that check.

Not in this package: the raw metric outputs Yield already delivered to Lens (Yield removes them
from its store once delivered; they live in Lens's inbox, which its API does not serve), and
anything a gap below names.
"""


def readme(index: Mapping[str, Any]) -> str:
    """The package's README: what it is, its layout, and its gaps."""
    runs = index["runs"]
    gaps = index["gaps"]
    lines = [
        f"# Evidence: {index['client']}",
        "",
        f"Exported {index['exported_at']} by proof {index['proof']}, "
        f"{len(runs)} run{'s' if len(runs) != 1 else ''}.",
        "",
        "## Layout",
        "",
        "| Path | What it is |",
        "|---|---|",
        *(f"| `{path}` | {what} |" for path, what in LAYOUT),
        "",
        "## Runs",
        "",
    ]
    for run in runs:
        lens = run["lens"]
        result = lens["result"] or "no result"
        lines.append(
            f"- `{run['run_id']}`: Yield manifest {'yes' if run['yield_manifest'] else 'no'}, "
            f"{run['store_objects']} store objects, Lens `{lens['project_id']}/{lens['run_id']}` "
            f"{result}"
        )
    lines += ["", "## Gaps", ""]
    lines += [f"- {gap['what']}: {gap['why']}" for gap in gaps] or ["None."]
    lines += ["", "## Notes", "", NOTES]
    return "\n".join(lines)


class Refused(RuntimeError):
    """The package was not written. `leaks` names each file and secret that stopped it."""

    def __init__(self, message: str, leaks: list[Leak] | None = None) -> None:
        super().__init__(message)
        self.leaks = leaks or []


def export(collector: Collector, destination: Path, secrets: Mapping[str, str]) -> dict[str, Any]:
    """Collect, scan and seal the package at `destination`, which must not exist yet. Returns the
    index."""
    if destination.exists():
        raise Refused(f"{destination} already exists; an evidence package is never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        collector.root = staging
        collector.collect()
        index = collector.index()
        (staging / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
        (staging / "README.md").write_text(readme(index))
        leaks = package.scan(staging, secrets)
        if leaks:
            raise Refused("a secret's value appears in the exported files", leaks)
        package.write_sums(staging)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index
