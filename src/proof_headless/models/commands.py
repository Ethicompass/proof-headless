"""`proof models`: validate, render, start, stop and inspect the served models.

`up` is the one with an order to keep. vLLM claims its memory share when a server starts, and two
servers starting together race for the same free memory, so models start one at a time and the
next waits until the previous reports healthy. Before the first starts, the manifest's arithmetic
is checked against the host's available memory, and a gated model without `HF_TOKEN` is refused:
both would otherwise fail minutes in, with a container log as the only explanation.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import typer
import yaml
from rich.table import Table

from proof_headless import paths
from proof_headless.console import err, out
from proof_headless.docker import (
    Compose,
    ComposeFailed,
    Restarts,
    ServiceState,
    appliance,
    ensure_network,
)
from proof_headless.models import manifest as manifests
from proof_headless.models import memory, render
from proof_headless.models.manifest import Manifest, ManifestInvalid, ModelEntry
from proof_headless.models.progress import CacheProgress, cache_dir
from proof_headless.secrets_commands import require

app = typer.Typer(help="Serve the models in a manifest with vLLM.", no_args_is_help=True)

POLL_SECONDS = 10.0
STOPPED = frozenset({"exited", "dead", "restarting"})
"""States that mean a starting model failed. `restarting` is the one `restart: unless-stopped`
turns a crash into: the container never reads as exited, so without it a model that dies at start
would be waited on until the deadline, and its log never shown."""
GRACE_SECONDS = 3600
"""How long past a model's `start_period_seconds` `up` waits for it before giving up.

Generous on purpose. On a DGX Spark with a cold cache every first start but the embedder's took
longer than the 20-minute start period (Llama Guard 4: about 47 minutes for a 24 GB download and
its compilation), and each came up healthy on its own. A model that is genuinely broken exits,
restarts or dies long before this, and that still fails `up` at once."""

ManifestOption = typer.Option(
    None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
)


def _shares_table(path: Path) -> Table | None:
    """The declared shares as a table, read from the raw file, for a manifest the budget refused."""
    try:
        raw = yaml.safe_load(path.read_text())
        models = raw["models"]
        budget = float(raw.get("memory_budget", Manifest.model_fields["memory_budget"].default))
        rows = [(str(m["name"]), float(m["gpu_memory_utilization"])) for m in models]
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError):
        return None
    table = Table(title="Memory shares", title_justify="left")
    table.add_column("model")
    table.add_column("share", justify="right")
    for name, share in rows:
        table.add_row(name, f"{share:.2f}")
    table.add_row("[b]total[/b]", f"[fail]{sum(s for _, s in rows):.2f}[/fail]")
    table.add_row("budget", f"{budget:.2f}")
    return table


def load_manifest(path: Path | None) -> Manifest:
    """The manifest at `path` (default `$PROOF_HOME/models.yaml`), or a refusal saying why."""
    target = path or paths.default_manifest()
    try:
        return manifests.load(target)
    except ManifestInvalid as error:
        err.print(f"[fail]Invalid manifest.[/fail] {error}")
        if "budget" in str(error):
            table = _shares_table(target)
            if table is not None:
                err.print(table)
        raise typer.Exit(2) from None


def compose_for(manifest: Manifest) -> Compose:
    """The compose project for `manifest`, rendered under `$PROOF_HOME/models`."""
    return appliance(render.PROJECT, render.write(manifest, paths.home() / "models"))


@app.command()
def validate(
    manifest: Path | None = ManifestOption,
    as_json: bool = typer.Option(False, "--json", help="Print the manifest as JSON."),
) -> None:
    """Check the manifest and show what it declares."""
    loaded = load_manifest(manifest)
    if as_json:
        out.print_json(loaded.model_dump_json())
        return
    table = Table(title="Models", title_justify="left")
    for column in ("name", "served as", "roles", "api", "port", "memory share"):
        table.add_column(column)
    for model in loaded.models:
        table.add_row(
            f"[name]{model.name}[/name]",
            model.served_name,
            ", ".join(sorted(model.roles)),
            model.api,
            str(model.port),
            f"{model.gpu_memory_utilization:.2f}",
        )
    out.print(table)
    out.print(
        f"[ok]Valid.[/ok] The models claim {loaded.claimed():.2f} of memory; "
        f"the budget is {loaded.memory_budget:.2f}."
    )


@app.command(name="render")
def render_command(manifest: Path | None = ManifestOption) -> None:
    """Write the compose file the models would run with, and print its path."""
    loaded = load_manifest(manifest)
    out.print(str(render.write(loaded, paths.home() / "models")))


def _check_secrets(models: list[ModelEntry]) -> None:
    gated = [model.name for model in models if model.gated]
    if gated:
        require({"HF_TOKEN"}, f"{gated}, which download gated weights,")


def _check_memory(
    loaded: Manifest,
    running: frozenset[str],
    starting: frozenset[str],
    host: memory.HostMemory | None,
    force: bool,
) -> None:
    if host is None:
        err.print(
            "[warn]Cannot read the host's memory here; the memory preflight is skipped.[/warn]"
        )
        return
    check = memory.preflight(loaded, host, running, starting)
    table = Table(title="Memory preflight", title_justify="left")
    for column in ("model", "share", "GiB", "state"):
        table.add_column(column)
    for claim in check.claims:
        state = "running, already held" if claim.running else "to start"
        table.add_row(claim.name, f"{claim.share:.2f}", f"{claim.gib:.1f}", state)
    table.add_row("", "", "", "")
    table.add_row("needed", "", f"{check.needed:.1f}", "")
    table.add_row("available", "", f"{check.available:.1f}", "")
    err.print(table)
    if not check.fits and not force:
        err.print(
            "[fail]Refused.[/fail] The models to start need more memory than is available. "
            "Stop something, lower a share, or pass --force to try anyway."
        )
        raise typer.Exit(2)


def _status_line(model: ModelEntry, health: str, elapsed: float, progress: CacheProgress) -> str:
    line = f"Waiting for [name]{model.name}[/name] to load {model.hf_id}: {health or 'starting'}"
    line += f", {int(elapsed // 60)}m{int(elapsed % 60):02d}s"
    described = progress.describe()
    return f"{line}, {described}" if described else line


def _fail(compose: Compose, model: ModelEntry, reason: str) -> NoReturn:
    tail = compose.logs(model.name, tail=40)
    err.print(f"[fail]{model.name} did not come up ({reason}).[/fail]")
    err.print(tail, markup=False, highlight=False)
    raise typer.Exit(1)


def wait_healthy(
    compose: Compose,
    model: ModelEntry,
    *,
    baseline: Restarts | None = None,
    grace: int = GRACE_SECONDS,
    progress: CacheProgress | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until `model` reports healthy, or fail with its log.

    It fails at once when the container exits, dies, disappears or restarts, and otherwise only
    at the deadline, `start_period_seconds` plus `grace`. `unhealthy` alone is not a failure: the
    health check counts failures from the end of the start period, and a first start that is
    still downloading weights reads `unhealthy` for as long as the download outlasts it, while
    the container is alive and making progress. The bytes in the model's cache directory
    (`progress`) are shown beside the state, so a download reads as one.

    `baseline` is the container and count read just before this start: a count covers a
    container's whole life, so one `up` left running from an earlier failed attempt carries
    restarts that are not this start's, and one `up` recreated starts again from zero.
    """
    started = clock()
    deadline = started + model.start_period_seconds + grace
    before = baseline or Restarts(container=None, count=0)
    meter = progress or CacheProgress(path=None, clock=clock)
    with err.status(_status_line(model, "starting", 0, meter)) as status:
        while True:
            state = compose.ps().get(model.name)
            if state is None:
                _fail(compose, model, "its container is gone")
            if state.health == "healthy":
                return
            restarted = compose.restarts(model.name).since(before)
            if state.state in STOPPED or restarted:
                _fail(
                    compose,
                    model,
                    f"{state.state}, {state.health or 'no health'}, {restarted} restarts",
                )
            meter.sample()
            now = clock()
            if now > deadline:
                _fail(
                    compose,
                    model,
                    f"not healthy {int(now - started)}s after its start, "
                    f"{state.health or 'no health'}; raise --grace to wait longer",
                )
            status.update(_status_line(model, state.health, now - started, meter))
            sleep(POLL_SECONDS)


@app.command()
def up(
    manifest: Path | None = ManifestOption,
    only: list[str] = typer.Option([], "--only", help="Start only these models (repeatable)."),
    force: bool = typer.Option(False, "--force", help="Start even if memory looks short."),
    grace: int = typer.Option(
        GRACE_SECONDS,
        "--grace",
        min=0,
        help="Seconds to wait past a model's start period before giving up on it.",
    ),
) -> None:
    """Start the models one at a time, each after the previous is healthy.

    A model still downloading or loading past its start period is waited on while its container
    runs; one that exits or restarts stops the sequence at once."""
    loaded = load_manifest(manifest)
    try:
        chosen = [loaded.named(name) for name in only] if only else list(loaded.models)
    except KeyError as error:
        err.print(f"[fail]{error.args[0]}[/fail]")
        raise typer.Exit(2) from None
    _check_secrets(chosen)
    compose = compose_for(loaded)
    try:
        if ensure_network(compose.runner, loaded.network):
            err.print(f"[muted]Created the {loaded.network} network.[/muted]")
        states = compose.ps()
        running = frozenset(n for n, s in states.items() if s.state == "running")
        starting = frozenset(model.name for model in chosen)
        _check_memory(loaded, running, starting, memory.read_host_memory(), force)
        for model in chosen:
            current = states.get(model.name)
            if current is not None and current.health == "healthy":
                err.print(f"[muted]{model.name} is already healthy.[/muted]")
                continue
            baseline = compose.restarts(model.name)
            compose.up(model.name)
            meter = CacheProgress(path=cache_dir(loaded, model), clock=time.monotonic)
            wait_healthy(compose, model, baseline=baseline, grace=grace, progress=meter)
            err.print(f"[ok]{model.name} is healthy[/ok] on 127.0.0.1:{model.port}.")
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None


DOWNLOADER = "hf"
"""The `huggingface_hub` CLI the vLLM images ship. `huggingface-cli` is its deprecated name."""


def _download(compose: Compose, model: ModelEntry, meter: CacheProgress) -> None:
    """Run the download in a thread while the status line follows the cache directory: the
    command prints nothing useful until it ends, and a 24 GB download takes half an hour."""
    failure: list[ComposeFailed] = []

    def target() -> None:
        try:
            compose.run_once(model.name, DOWNLOADER, "download", model.hf_id)
        except ComposeFailed as error:
            failure.append(error)

    worker = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    with err.status(f"Downloading [name]{model.name}[/name] ({model.hf_id})") as status:
        worker.start()
        while worker.is_alive():
            meter.sample()
            elapsed = time.monotonic() - started
            line = f"Downloading [name]{model.name}[/name] ({model.hf_id}), {int(elapsed)}s"
            described = meter.describe()
            status.update(f"{line}, {described}" if described else line)
            worker.join(POLL_SECONDS)
    if failure:
        raise failure[0]


@app.command()
def pull(
    manifest: Path | None = ManifestOption,
    only: list[str] = typer.Option([], "--only", help="Download only these models (repeatable)."),
) -> None:
    """Download the models' weights into the cache before `up`, so `up` only loads them.

    Each download runs `hf download` in the model's own image, against the same cache mount and
    with the same `HF_TOKEN` its server gets, so what lands in the cache is what the server
    reads. Running it again only checks what is already there."""
    loaded = load_manifest(manifest)
    try:
        chosen = [loaded.named(name) for name in only] if only else list(loaded.models)
    except KeyError as error:
        err.print(f"[fail]{error.args[0]}[/fail]")
        raise typer.Exit(2) from None
    _check_secrets(chosen)
    compose = compose_for(loaded)
    try:
        ensure_network(compose.runner, loaded.network)
        for model in chosen:
            directory = cache_dir(loaded, model)
            if directory is None:
                err.print(
                    f"[muted]{model.name} loads {model.hf_id}, which is not downloaded.[/muted]"
                )
                continue
            _download(compose, model, CacheProgress(path=directory, clock=time.monotonic))
            err.print(f"[ok]{model.name} downloaded[/ok] to {directory}.")
    except ComposeFailed as error:
        err.print(f"[fail]The download failed.[/fail] {error}")
        raise typer.Exit(1) from None


@app.command()
def down(manifest: Path | None = ManifestOption) -> None:
    """Stop and remove the model containers. Downloaded weights stay in the cache."""
    compose = compose_for(load_manifest(manifest))
    try:
        compose.down()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    err.print("[ok]Models stopped.[/ok]")


def _row(model: ModelEntry, state: ServiceState | None) -> dict[str, str]:
    return {
        "name": model.name,
        "served_name": model.served_name,
        "port": str(model.port),
        "state": state.state if state else "absent",
        "health": (state.health or "none") if state else "absent",
        "memory_share": f"{model.gpu_memory_utilization:.2f}",
    }


@app.command()
def status(
    manifest: Path | None = ManifestOption,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show each model's container state and health."""
    loaded = load_manifest(manifest)
    try:
        states = compose_for(loaded).ps()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    rows = [_row(model, states.get(model.name)) for model in loaded.models]
    if as_json:
        out.print_json(json.dumps(rows))
        return
    table = Table(title="Models", title_justify="left")
    for column in ("name", "served as", "port", "state", "health", "share"):
        table.add_column(column)
    for row in rows:
        style = {"healthy": "ok", "starting": "warn", "unhealthy": "fail"}.get(row["health"], "")
        table.add_row(
            f"[name]{row['name']}[/name]",
            row["served_name"],
            row["port"],
            row["state"],
            f"[{style}]{row['health']}[/{style}]" if style else row["health"],
            row["memory_share"],
        )
    out.print(table)


@app.command()
def logs(
    name: str,
    manifest: Path | None = ManifestOption,
    tail: int = typer.Option(200, "--tail", help="How many lines."),
) -> None:
    """Print the last lines of one model's logs."""
    loaded = load_manifest(manifest)
    try:
        loaded.named(name)
        text = compose_for(loaded).logs(name, tail=tail)
    except KeyError as error:
        err.print(f"[fail]{error.args[0]}[/fail]")
        raise typer.Exit(2) from None
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    out.print(text, markup=False, highlight=False)
