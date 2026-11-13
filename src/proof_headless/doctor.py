"""`proof doctor`: prove every served model answers the way a run will need, before it runs.

Each model is probed on its loopback port with the probes its roles call for. With `--stack`,
Yield, Lens, their stores, Valkey and the brain registry are probed on theirs as well, and the
report names the image lock they run from. The outcome is a
Rich table for the operator and a JSON report under `$PROOF_HOME/evidence/`, because what the
models answered on the day is part of an engagement's evidence: the report says which model,
served under which name, passed which check. The exit code is non-zero when any probe failed, so a
script can gate a run on it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.table import Table

from proof_headless import __version__, paths
from proof_headless.console import err, out
from proof_headless.models import manifest as manifests
from proof_headless.models import probes
from proof_headless.models.manifest import Manifest, ManifestInvalid, ModelEntry
from proof_headless.stack import lock as locks
from proof_headless.stack import probes as stack_probes

ClientFor = Callable[[ModelEntry, str, float], httpx.Client]


def client_for(model: ModelEntry, host: str, timeout: float) -> httpx.Client:
    """An HTTP client for one model's host port."""
    return httpx.Client(base_url=f"http://{host}:{model.port}", timeout=timeout)


def examine(
    manifest: Manifest,
    names: list[str],
    *,
    host: str,
    timeout: float,
    connect: ClientFor | None = None,
) -> list[probes.Result]:
    """Every probe for every chosen model, in manifest order."""
    chosen = [manifest.named(name) for name in names] if names else list(manifest.models)
    results: list[probes.Result] = []
    for model in chosen:
        with (connect or client_for)(model, host, timeout) as client:
            for probe in probes.probes_for(model):
                with err.status(f"[name]{model.name}[/name]: {probes.name_of(probe)}"):
                    results.append(probes.run(client, model, probe))
    return results


def report(
    manifest: Manifest | None, results: list[probes.Result], lock: locks.Lock | None = None
) -> dict[str, object]:
    """The JSON report: when, with what, and every probe's outcome."""
    document: dict[str, object] = {
        "proof": __version__,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {
            model.name: {
                "hf_id": model.hf_id,
                "served_name": model.served_name,
                "image": model.image,
                "gpu_memory_utilization": model.gpu_memory_utilization,
            }
            for model in (manifest.models if manifest else ())
        },
    }
    if lock is not None:
        document["stack"] = {
            "channel": lock.channel,
            "images": {name: image.pinned for name, image in lock.images.items()},
        }
    document["results"] = [asdict(result) for result in results]
    document["passed"] = all(result.ok for result in results)
    return document


def _stack_lock() -> locks.Lock | None:
    """The lock the stack runs from, for the report; a stack started from another lock path
    still gets probed, it just goes unnamed."""
    try:
        return locks.load(paths.default_lock())
    except locks.LockInvalid:
        err.print(
            "[warn]No readable $PROOF_HOME/appliance.lock; the report names no images.[/warn]"
        )
        return None


def write_report(document: dict[str, object]) -> Path:
    """Save the report under `$PROOF_HOME/evidence/` and return its path."""
    directory = paths.home() / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"doctor-{stamp}.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


def doctor(
    manifest: Path | None = typer.Option(
        None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
    ),
    only: list[str] = typer.Option([], "--only", help="Probe only these models (repeatable)."),
    models: bool = typer.Option(True, "--models/--no-models", help="Probe the served models."),
    stack: bool = typer.Option(
        False, "--stack", help="Also probe Yield, Lens, MinIO, Valkey and the brain registry."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Where the ports are bound."),
    timeout: float = typer.Option(300.0, "--timeout", help="Seconds per request."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
) -> None:
    """Probe every served model the way Yield and Lens will use it, and with --stack the stack."""
    loaded: Manifest | None = None
    results: list[probes.Result] = []
    if models:
        try:
            loaded = manifests.load(manifest or paths.default_manifest())
            results = examine(loaded, only, host=host, timeout=timeout)
        except ManifestInvalid as error:
            err.print(f"[fail]Invalid manifest.[/fail] {error}")
            raise typer.Exit(2) from None
        except KeyError as error:
            err.print(f"[fail]{error.args[0]}[/fail]")
            raise typer.Exit(2) from None
    lock = None
    if stack:
        lock = _stack_lock()
        with err.status("Probing the stack"):
            results += stack_probes.examine(host, min(timeout, 10.0))
    if not results:
        err.print("[fail]Nothing to probe.[/fail] Drop --no-models or add --stack.")
        raise typer.Exit(2)
    document = report(loaded, results, lock)
    path = write_report(document)
    if as_json:
        out.print_json(json.dumps(document))
    else:
        table = Table(title="Doctor", title_justify="left")
        for column in ("model", "probe", "result", "detail"):
            table.add_column(column)
        for result in results:
            mark = "[ok]pass[/ok]" if result.ok else "[fail]FAIL[/fail]"
            table.add_row(f"[name]{result.model}[/name]", result.probe, mark, result.detail)
        out.print(table)
    err.print(f"[muted]Report: {path}[/muted]")
    if not document["passed"]:
        raise typer.Exit(1)
