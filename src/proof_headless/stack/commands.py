"""`proof stack`: pin, render, start, stop and inspect Yield, Lens and the brain registry.

`up` refuses what would fail late. An image with no build for this host's architecture would pull
and then die with `exec format error` (the released images are amd64 only until the next
promotion, and the appliance is arm64). A secrets file others can read is refused like everywhere
else. The models' network is created when absent, because every job joins it and a launch onto a
missing network is refused by the API with a 503 mid-run (`docker.ensure_network` says why it
is external to both projects). The images the APIs launch per job are
pulled up front: neither dispatcher pulls, so a missing one would fail the first run, not `up`.
"""

from __future__ import annotations

import json
import platform as host_platform
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import typer
from rich.table import Table

from proof_headless import paths, secrets
from proof_headless.console import err, out
from proof_headless.docker import (
    Compose,
    ComposeFailed,
    Runner,
    ServiceState,
    docker,
    ensure_network,
    run,
)
from proof_headless.stack import lock as locks
from proof_headless.stack import render
from proof_headless.stack.lock import Channel, Lock, LockInvalid
from proof_headless.stack.pin import ResolveFailed, imagetools, pin

app = typer.Typer(
    help="Yield, Lens and the brain registry, from pinned images.", no_args_is_help=True
)

POLL_SECONDS = 5.0
READY_SECONDS = 900.0
"""How long `up` waits for every service once compose has started them. Compose already waited
for the stores and the seeds, so what is left is the APIs' own start."""
JOB_IMAGES = ("yield-driver", "lens-run")
"""Images no service runs, launched by an API per job; `yield-compute` is also a service."""

LockOption = typer.Option(
    None, "--lock", "-l", help="The image lock. Default: $PROOF_HOME/appliance.lock."
)


def host_platform_name() -> str:
    """This host as an image platform, `linux/amd64` or `linux/arm64`: the daemon runs Linux
    images even on a Mac, on the machine's own architecture."""
    machine = host_platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"linux/{arch}"


def _load(path: Path | None) -> Lock:
    try:
        return locks.load(path or paths.default_lock())
    except LockInvalid as error:
        err.print(f"[fail]Invalid lock.[/fail] {error}")
        raise typer.Exit(2) from None


def compose_for(lock: Lock, runner: Runner = run) -> Compose:
    """The compose project for `lock`, rendered under `$PROOF_HOME/stack`.

    The secrets file is the env file twice over: `--env-file` so compose interpolates the store
    credentials the operator may have set, and each API's `env_file` so a run's `secret_ref`
    resolves. Its values reach the containers and nothing else `proof` writes.
    """
    file = render.write(lock, paths.home() / "stack", paths.secrets_file())
    return Compose(project=render.PROJECT, file=file, env_file=paths.secrets_file(), runner=runner)


@app.command(name="pin")
def pin_command(
    channel: str = typer.Option(
        "release", "--channel", "-c", help="`release` (released tags) or `develop`."
    ),
    overrides: list[str] = typer.Option(
        [], "--set", help="Resolve NAME from REF instead of the channel's (NAME=REF, repeatable)."
    ),
    lock: Path | None = LockOption,
) -> None:
    """Resolve every image of a channel to its digest and write the lock."""
    if channel not in ("release", "develop"):
        err.print(f"[fail]No channel {channel!r}.[/fail] Use `release` or `develop`.")
        raise typer.Exit(2)
    pairs = dict(item.split("=", 1) for item in overrides if "=" in item)
    if len(pairs) != len(overrides):
        err.print("[fail]--set takes NAME=REF.[/fail]")
        raise typer.Exit(2)
    try:
        with err.status(f"Resolving the {channel} images"):
            pinned = pin(cast(Channel, channel), imagetools(), pairs)
    except KeyError as error:
        err.print(f"[fail]{error.args[0]}[/fail]")
        raise typer.Exit(2) from None
    except ResolveFailed as error:
        err.print(f"[fail]Could not resolve an image.[/fail] {error}")
        err.print("[muted]A private image needs `docker login ghcr.io` first.[/muted]")
        raise typer.Exit(1) from None
    target = lock or paths.default_lock()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(locks.dump(pinned))
    out.print(_lock_table(pinned))
    err.print(f"[ok]Pinned.[/ok] {target}")


def _lock_table(lock: Lock) -> Table:
    table = Table(title=f"Lock ({lock.channel})", title_justify="left")
    for column in ("image", "reference", "digest", "platforms"):
        table.add_column(column)
    for name in locks.IMAGES:
        image = lock.image(name)
        table.add_row(
            f"[name]{name}[/name]",
            image.reference,
            image.digest[:19],
            ", ".join(image.platforms) or "unknown",
        )
    return table


@app.command(name="render")
def render_command(lock: Path | None = LockOption) -> None:
    """Write the compose file the stack would run with, and print its path."""
    loaded = _load(lock)
    out.print(str(render.write(loaded, paths.home() / "stack", paths.secrets_file())))


def _check_platforms(lock: Lock, force: bool) -> None:
    here = host_platform_name()
    foreign = [name for name in locks.IMAGES if not lock.image(name).runs_on(here)]
    if not foreign:
        return
    message = f"{foreign} have no {here} build in the lock."
    if force:
        err.print(f"[warn]{message} Starting anyway (--force).[/warn]")
        return
    err.print(
        f"[fail]Refused.[/fail] {message} Pin a channel that publishes {here} "
        "(`proof stack pin --channel develop`), or pass --force."
    )
    raise typer.Exit(2)


def _check_secrets() -> None:
    try:
        present = secrets.names(paths.secrets_file())
    except secrets.SecretsFileExposed as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None
    reconfiguring = sorted(name for name in present if render.reconfigures(name))
    if reconfiguring:
        err.print(
            f"[fail]Refused.[/fail] {reconfiguring} in {paths.secrets_file()} would reconfigure "
            "Yield or Lens, which load that file. Remove them."
        )
        raise typer.Exit(2)


def pull_job_images(runner: Runner, lock: Lock) -> None:
    """Pull the images the APIs launch, which compose does not know of."""
    for name in JOB_IMAGES:
        with err.status(f"Pulling [name]{name}[/name]"):
            docker(runner, "pull", "--quiet", lock.image(name).pinned)


def wait_ready(
    compose: Compose,
    document: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until every long-running service is healthy and every one-shot exited 0; fail fast
    on an unhealthy service, a stopped one, or a seed that failed, with its log."""
    services = render.long_running(document)
    jobs = render.one_shot(document)
    deadline = clock() + READY_SECONDS
    with err.status("Waiting for the stack to report healthy"):
        while True:
            states = compose.ps()
            failed = _failed(states, services, jobs)
            if failed is not None:
                state = states[failed]
                err.print(
                    f"[fail]{failed} did not come up ({state.state}, "
                    f"{state.health or 'no health'}, exit {state.exit_code}).[/fail]"
                )
                err.print(compose.logs(failed, tail=40), markup=False, highlight=False)
                raise typer.Exit(1)
            if _ready(states, services, jobs):
                return
            if clock() > deadline:
                waiting = [s for s in services if states.get(s, None) is None or not _up(states[s])]
                err.print(f"[fail]Not healthy in time: {waiting}.[/fail]")
                raise typer.Exit(1)
            sleep(POLL_SECONDS)


def _up(state: ServiceState) -> bool:
    return state.state == "running" and state.health == "healthy"


def _failed(states: dict[str, ServiceState], services: list[str], jobs: list[str]) -> str | None:
    for name in services:
        state = states.get(name)
        if state is not None and (
            state.health == "unhealthy" or state.state in {"exited", "dead", "restarting"}
        ):
            return name
    for name in jobs:
        state = states.get(name)
        if state is not None and state.exit_code not in (None, 0):
            return name
    return None


def _ready(states: dict[str, ServiceState], services: list[str], jobs: list[str]) -> bool:
    long_ok = all(name in states and _up(states[name]) for name in services)
    jobs_ok = all(name in states and states[name].exit_code == 0 for name in jobs)
    return long_ok and jobs_ok


def _explain(compose: Compose, document: dict[str, Any]) -> None:
    """The log of the service that failed, when compose stopped on a dependency: a seed that
    exited non-zero stops `up` with the seed's name and none of its output."""
    try:
        failed = _failed(compose.ps(), render.long_running(document), render.one_shot(document))
        if failed is not None:
            err.print(f"[fail]{failed}[/fail] last lines:")
            err.print(compose.logs(failed, tail=40), markup=False, highlight=False)
            return
    except ComposeFailed:
        pass
    err.print("[muted]`proof stack status` and `proof stack logs <service>` say more.[/muted]")


@app.command()
def up(
    lock: Path | None = LockOption,
    force: bool = typer.Option(False, "--force", help="Start images built for another platform."),
) -> None:
    """Pull the pinned images, start the stack, seed it, and wait until it is healthy."""
    loaded = _load(lock)
    _check_platforms(loaded, force)
    _check_secrets()
    compose = compose_for(loaded)
    document = render.compose(loaded, paths.secrets_file())
    runner = compose.runner
    try:
        if ensure_network(runner, render.MODELS_NET):
            err.print(f"[muted]Created the {render.MODELS_NET} network.[/muted]")
        with err.status("Pulling the stack's images"):
            compose.pull()
        pull_job_images(runner, loaded)
        with err.status("Starting the stores, then the seeds, then the APIs"):
            compose.up_all()
        wait_ready(compose, document)
    except ComposeFailed as error:
        err.print(f"[fail]docker failed.[/fail] {error}")
        _explain(compose, document)
        raise typer.Exit(1) from None
    out.print(_status_table(_rows(loaded, compose.ps(), document)))
    err.print("[ok]The stack is healthy.[/ok]")


@app.command()
def down(lock: Path | None = LockOption) -> None:
    """Stop and remove the stack's containers. The stores and the registry keep their volumes."""
    compose = compose_for(_load(lock))
    try:
        compose.down()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    err.print("[ok]Stack stopped.[/ok]")


def _rows(
    lock: Lock, states: dict[str, ServiceState], document: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, service in document["services"].items():
        expected = str(service["image"])
        state = states.get(name)
        rows.append(
            {
                "service": name,
                "image": expected.split("@", 1)[0],
                "digest": expected.split("@", 1)[1],
                "matches_lock": state is None or not state.image or state.image == expected,
                "state": state.state if state else "absent",
                "health": (state.health or "none") if state else "absent",
                "exit_code": state.exit_code if state else None,
                "ports": list(state.ports) if state else [],
                "one_shot": "restart" not in service,
            }
        )
    return rows


def _status_table(rows: list[dict[str, Any]]) -> Table:
    table = Table(title="Stack", title_justify="left")
    for column in ("service", "image", "digest", "state", "health", "ports"):
        table.add_column(column)
    for row in rows:
        health = row["health"]
        if row["one_shot"] and row["exit_code"] is not None:
            health = "done" if row["exit_code"] == 0 else f"exit {row['exit_code']}"
        style = {
            "healthy": "ok",
            "done": "ok",
            "starting": "warn",
            "unhealthy": "fail",
        }.get(health, "fail" if health.startswith("exit") else "")
        digest = row["digest"][:19]
        if not row["matches_lock"]:
            digest = f"[warn]{digest} (not the running image)[/warn]"
        table.add_row(
            f"[name]{row['service']}[/name]",
            row["image"],
            digest,
            row["state"],
            f"[{style}]{health}[/{style}]" if style else health,
            ", ".join(row["ports"]),
        )
    return table


@app.command()
def status(
    lock: Path | None = LockOption,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show each service's image digest, state, health and ports."""
    loaded = _load(lock)
    compose = compose_for(loaded)
    try:
        states = compose.ps()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    rows = _rows(loaded, states, render.compose(loaded, paths.secrets_file()))
    if as_json:
        out.print_json(json.dumps({"channel": loaded.channel, "services": rows}))
        return
    out.print(_status_table(rows))


@app.command()
def logs(
    service: str,
    lock: Path | None = LockOption,
    tail: int = typer.Option(200, "--tail", help="How many lines."),
) -> None:
    """Print the last lines of one service's logs."""
    loaded = _load(lock)
    names = list(render.compose(loaded, paths.secrets_file())["services"])
    if service not in names:
        err.print(f"[fail]No service {service!r}; the stack has {names}.[/fail]")
        raise typer.Exit(2)
    try:
        text = compose_for(loaded).logs(service, tail=tail)
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    out.print(text, markup=False, highlight=False)
