"""`proof models`: validate, render, start, stop and inspect the served models.

`up` is the one with an order to keep. vLLM claims its memory share when a server starts, and two
servers starting together race for the same free memory, so models start one at a time and the
next waits until the previous reports healthy. Before the first starts, the manifest's arithmetic
is checked against the host's available memory, and a gated model without `HF_TOKEN` is refused:
both would otherwise fail minutes in, with a container log as the only explanation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import typer
from rich.table import Table

from proof_headless import paths, secrets
from proof_headless.console import err, out
from proof_headless.docker import Compose, ComposeFailed, ServiceState
from proof_headless.models import manifest as manifests
from proof_headless.models import memory, render
from proof_headless.models.manifest import Manifest, ManifestInvalid, ModelEntry

app = typer.Typer(help="Serve the models in a manifest with vLLM.", no_args_is_help=True)

POLL_SECONDS = 10.0
GRACE_SECONDS = 600
"""How long past a model's `start_period_seconds` `up` waits for it before giving up."""

ManifestOption = typer.Option(
    None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
)


def _load(path: Path | None) -> Manifest:
    try:
        return manifests.load(path or paths.default_manifest())
    except ManifestInvalid as error:
        err.print(f"[fail]Invalid manifest.[/fail] {error}")
        raise typer.Exit(2) from None


def compose_for(manifest: Manifest) -> Compose:
    """The compose project for `manifest`, rendered under `$PROOF_HOME/models`."""
    file = render.write(manifest, paths.home() / "models")
    return Compose(project=render.PROJECT, file=file, env_file=paths.secrets_file())


@app.command()
def validate(manifest: Path | None = ManifestOption) -> None:
    """Check the manifest and show what it declares."""
    loaded = _load(manifest)
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
    loaded = _load(manifest)
    out.print(str(render.write(loaded, paths.home() / "models")))


def _check_secrets(loaded: Manifest, models: list[ModelEntry]) -> None:
    gated = [model.name for model in models if model.gated]
    if not gated:
        return
    try:
        present = secrets.names(paths.secrets_file())
    except secrets.SecretsFileExposed as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None
    if "HF_TOKEN" not in present:
        err.print(
            f"[fail]Refused.[/fail] {gated} download gated weights and HF_TOKEN is not in "
            f"{paths.secrets_file()}. Add it (the token must have accepted each model's license)."
        )
        raise typer.Exit(2)


def _check_memory(
    loaded: Manifest, running: frozenset[str], host: memory.HostMemory | None, force: bool
) -> None:
    if host is None:
        err.print(
            "[warn]Cannot read the host's memory here; the memory preflight is skipped.[/warn]"
        )
        return
    check = memory.preflight(loaded, host, running)
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


def wait_healthy(
    compose: Compose,
    model: ModelEntry,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until `model` reports healthy; fail fast if its container stops or turns unhealthy."""
    deadline = clock() + model.start_period_seconds + GRACE_SECONDS
    with err.status(f"Waiting for [name]{model.name}[/name] to load {model.hf_id}"):
        while True:
            state = compose.ps().get(model.name)
            if state is not None and state.health == "healthy":
                return
            if state is not None and (state.state == "exited" or state.health == "unhealthy"):
                tail = compose.logs(model.name, tail=40)
                err.print(f"[fail]{model.name} did not come up ({state.state}, {state.health}).")
                err.print(tail, markup=False, highlight=False)
                raise typer.Exit(1)
            if clock() > deadline:
                err.print(f"[fail]{model.name} was not healthy in time.[/fail]")
                raise typer.Exit(1)
            sleep(POLL_SECONDS)


@app.command()
def up(
    manifest: Path | None = ManifestOption,
    only: list[str] = typer.Option([], "--only", help="Start only these models (repeatable)."),
    force: bool = typer.Option(False, "--force", help="Start even if memory looks short."),
) -> None:
    """Start the models one at a time, each after the previous is healthy."""
    loaded = _load(manifest)
    try:
        chosen = [loaded.named(name) for name in only] if only else list(loaded.models)
    except KeyError as error:
        err.print(f"[fail]{error.args[0]}[/fail]")
        raise typer.Exit(2) from None
    _check_secrets(loaded, chosen)
    compose = compose_for(loaded)
    try:
        states = compose.ps()
        running = frozenset(n for n, s in states.items() if s.state == "running")
        _check_memory(loaded, running, memory.read_host_memory(), force)
        for model in chosen:
            current = states.get(model.name)
            if current is not None and current.health == "healthy":
                err.print(f"[muted]{model.name} is already healthy.[/muted]")
                continue
            compose.up(model.name)
            wait_healthy(compose, model)
            err.print(f"[ok]{model.name} is healthy[/ok] on 127.0.0.1:{model.port}.")
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None


@app.command()
def down(manifest: Path | None = ManifestOption) -> None:
    """Stop and remove the model containers. Downloaded weights stay in the cache."""
    compose = compose_for(_load(manifest))
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
    loaded = _load(manifest)
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
    loaded = _load(manifest)
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
