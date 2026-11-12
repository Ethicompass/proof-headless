"""A client's Yield runs, and the Lens run each one's delivery derives.

The runs come from the queue's state when `proof run queue` has written one, since that is what was
actually submitted, and otherwise from the specs `proof spec render` wrote. Either way the run's
declaration comes from its spec: Yield copies the spec's `target_declaration` into the manifest
and the delivery, and Lens names the derived project after it (Lens `derive.project_id_for`). A
derived run keeps the Yield run's id, so both ids are known without asking Lens anything.

`proof` never registers a Lens run of its own, so every run here is a derived one. A run some other
consumer registered under its own project is not found by these ids, and reads as not received.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proof_headless import paths

DECLARED_PREFIX = "decl-"
UNDECLARED_PREFIX = "undeclared-"
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class RunsUnknown(ValueError):
    """The client has neither a queue state nor rendered specs, or one of them does not read."""


@dataclass(frozen=True)
class YieldRun:
    """One Yield run of the client and the Lens ids its delivery derives."""

    run_id: str
    declaration: str | None
    """The `sha256:` digest the run's spec pins as `target_declaration`, or `None`."""

    @property
    def project_id(self) -> str:
        """`decl-<first 20 hex of the declaration>`, or `undeclared-<16 hex of the run id>` for a
        run that pins none, as Lens derives it."""
        if self.declaration:
            match = _DIGEST.match(self.declaration)
            if match is None:
                raise RunsUnknown(
                    f"run {self.run_id}: target_declaration {self.declaration!r} is not a "
                    f"sha256 digest"
                )
            return DECLARED_PREFIX + match.group(1)[:20]
        return UNDECLARED_PREFIX + hashlib.sha256(self.run_id.encode()).hexdigest()[:16]

    @property
    def lens_run_id(self) -> str:
        """A derived Lens run takes the Yield run's id."""
        return self.run_id


def state_dir(client: str) -> Path:
    """The client's state directory under `$PROOF_HOME`."""
    return paths.home() / "bundles" / client


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunsUnknown(f"{path} does not read as JSON: {error}") from None


def _queued(document: Any, path: Path) -> list[str]:
    """The run ids a queue state lists: a list of ids or of objects with `run_id`, bare or under
    `runs`, in order and once each."""
    entries = document.get("runs") if isinstance(document, dict) else document
    if isinstance(entries, dict):
        entries = [
            {"run_id": key, **value} if isinstance(value, dict) else key
            for key, value in entries.items()
        ]
    if not isinstance(entries, list):
        raise RunsUnknown(f"{path} lists no runs")
    ids: list[str] = []
    for entry in entries:
        run_id = entry.get("run_id") if isinstance(entry, dict) else entry
        if not isinstance(run_id, str) or not run_id:
            raise RunsUnknown(f"{path}: an entry has no run_id: {entry!r}")
        if run_id not in ids:
            ids.append(run_id)
    return ids


def client_runs(client: str) -> list[YieldRun]:
    """Every Yield run of `client`, in queue order or, without a queue, in spec file order."""
    directory = state_dir(client)
    specs = directory / "specs"
    queue = directory / "queue.json"
    if queue.is_file():
        ids = _queued(_read(queue), queue)
    elif specs.is_dir():
        ids = [path.stem for path in sorted(specs.glob("*.json"))]
    else:
        raise RunsUnknown(
            f"client {client} has no queue state at {queue} and no specs at {specs}; render them "
            f"with `proof spec render` first"
        )
    return [YieldRun(run_id, _declaration(specs / f"{run_id}.json")) for run_id in ids]


def _declaration(spec: Path) -> str | None:
    if not spec.is_file():
        return None
    document = _read(spec)
    value = document.get("target_declaration") if isinstance(document, dict) else None
    return str(value) if value else None


def find(client: str, run_id: str) -> YieldRun:
    """The client's run `run_id`, refused when the client has no such run."""
    runs = client_runs(client)
    for run in runs:
        if run.run_id == run_id:
            return run
    raise RunsUnknown(f"client {client} has no run {run_id}; it has {[r.run_id for r in runs]}")
