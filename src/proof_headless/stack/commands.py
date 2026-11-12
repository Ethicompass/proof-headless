"""`proof stack`: pin, render, start, stop and inspect Yield, Lens and the brain registry.

`up` is the one command that renders. It refuses what would fail late: an image with no build for
this host (it would pull and then die with `exec format error`), a models manifest with no chat
model for Lens, a secrets file others can read, and a secret a bundle declares that the file does
not define. It generates the stack's own credentials the secrets file lacks before rendering
(`credentials.ensure`), as `render --write` does. It creates the models' network when absent
(`docker.ensure_network` says why it is external to both projects), pulls the images the APIs launch
per job (neither dispatcher pulls), starts the project and waits until every service is healthy and
every seed exited 0.

`status`, `logs` and `down` read the compose file the stack was started from and never re-render
it, and `render` only previews unless told `--write`, so a `pin` or a new manifest after `up`
cannot change what they describe or stop. The bundles the stack serves are recorded, so every
`up` gives Yield the secrets of all of them, not only of the bundles on that command line.
"""

from __future__ import annotations

import json
import platform as host_platform
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import typer
import yaml
from rich.table import Table

from proof_headless import paths, secrets
from proof_headless.bundle import manifest as bundles
from proof_headless.bundle.manifest import BundleInvalid
from proof_headless.console import err, out
from proof_headless.docker import (
    Compose,
    ComposeFailed,
    Runner,
    ServiceState,
    appliance,
    docker,
    ensure_network,
)
from proof_headless.models.commands import load_manifest
from proof_headless.models.manifest import Manifest
from proof_headless.secrets_commands import require
from proof_headless.stack import credentials, render
from proof_headless.stack import lock as locks
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
ManifestOption = typer.Option(
    None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
)
BundleOption = typer.Option(
    [],
    "--bundle",
    "-b",
    help="A bundle whose declared secrets Yield may resolve (repeatable).",
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


def stack_file() -> Path:
    """Where `up` and `render` write the stack's compose file."""
    return paths.home() / "stack" / "compose.yaml"


def project(file: Path) -> Compose:
    """The stack's compose project on `file`; a test replaces it to answer without a daemon."""
    return appliance(render.PROJECT, file)


def started() -> tuple[Compose, dict[str, Any]]:
    """The project and document of the stack as last rendered, without rendering it again."""
    file = stack_file()
    if not file.exists():
        err.print("[fail]No stack here yet.[/fail] Start it with `proof stack up`.")
        raise typer.Exit(2)
    return project(file), render.read(file)


def bundles_file() -> Path:
    """The bundles the stack serves, by client, with the secret names each declared."""
    return paths.home() / "stack" / "bundles.json"


def served() -> dict[str, dict[str, Any]]:
    """The recorded bundles: `{client: {"directory": ..., "secrets": [...]}}`.

    Recorded because Yield's secrets are rendered into its services, and compose recreates
    yield-api and yield-compute from the file on every `up`: rendered from that call's
    `--bundle` flags alone, a later `up` would silently drop every earlier client's secrets.
    """
    path = bundles_file()
    if not path.exists():
        return {}
    recorded: dict[str, dict[str, Any]] = json.loads(path.read_text())
    return recorded


def _record(entries: dict[str, dict[str, Any]]) -> None:
    path = bundles_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")


def _with_bundles(directories: list[Path]) -> dict[str, dict[str, Any]]:
    """The recorded bundles plus those in `directories`, each read now; nothing is written."""
    entries = served()
    for directory in directories:
        try:
            bundle = bundles.load(directory)
        except BundleInvalid as error:
            err.print(f"[fail]Invalid bundle.[/fail] {error}")
            raise typer.Exit(2) from None
        entries[bundle.client] = {
            "directory": str(directory.resolve()),
            "secrets": sorted(bundle.secrets),
        }
    return entries


def secret_names(entries: dict[str, dict[str, Any]]) -> list[str]:
    """The union of the secrets every served bundle declares: what Yield gets."""
    return sorted({name for entry in entries.values() for name in entry["secrets"]})


def _document(lock: Lock, manifest: Manifest, names: list[str]) -> dict[str, Any]:
    try:
        return render.compose(lock, manifest, names)
    except render.StackInvalid as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None


def _render(lock: Lock, manifest: Manifest, names: list[str]) -> Path:
    _document(lock, manifest, names)
    return render.write(lock, manifest, stack_file().parent, names)


def foreign_images(lock: Lock, platform: str) -> list[str]:
    """The lock's images with no build for `platform`."""
    return [name for name in locks.IMAGES if not lock.image(name).runs_on(platform)]


@app.command(name="pin")
def pin_command(
    channel: str = typer.Option(
        "develop",
        "--channel",
        "-c",
        help="`develop` (amd64 and arm64) or `release` (amd64 only until the next promotion).",
    ),
    overrides: list[str] = typer.Option(
        [], "--set", help="Resolve NAME from REF instead of the channel's (NAME=REF, repeatable)."
    ),
    platform: str | None = typer.Option(
        None, "--platform", help="The platform the stack will run on. Default: this host's."
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
    target_platform = platform or host_platform_name()
    foreign = foreign_images(pinned, target_platform)
    if foreign:
        other = "develop" if channel == "release" else "release"
        err.print(_lock_table(pinned))
        err.print(
            f"[fail]Refused.[/fail] {foreign} have no {target_platform} build on the {channel} "
            f"channel, so the stack could not run them. Nothing was written. Try "
            f"`--channel {other}`, or pin for another host with `--platform`."
        )
        raise typer.Exit(2)
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
def render_command(
    lock: Path | None = LockOption,
    manifest: Path | None = ManifestOption,
    bundle: list[Path] = BundleOption,
    write: bool = typer.Option(
        False, "--write", help="Write it where `up` does, replacing the running stack's file."
    ),
) -> None:
    """Print the compose file `up` would start, for the served bundles plus any --bundle.

    A preview by default: the file under `$PROOF_HOME/stack` is the one the running stack was
    started from, and `status`, `logs` and `down` read it, so only `--write` replaces it, after
    generating the credentials it refers to that the secrets file lacks, as `up` does.
    """
    loaded = _load(lock)
    models = load_manifest(manifest)
    names = secret_names(_with_bundles(bundle))
    if not write:
        document = _document(loaded, models, names)
        text = yaml.safe_dump(document, sort_keys=False)
        out.print(text, markup=False, highlight=False, soft_wrap=True, end="")
        return
    _ensure_credentials()
    out.print(str(_render(loaded, models, names)))


def _check_platforms(lock: Lock, force: bool) -> None:
    here = host_platform_name()
    foreign = foreign_images(lock, here)
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


def _ensure_credentials() -> None:
    """Generate the stack's credentials the secrets file lacks, before writing the file that
    refers to them: compose refuses every command on that file, `down` included, while one of
    them is missing."""
    try:
        generated = credentials.ensure(paths.secrets_file())
    except secrets.SecretsFileExposed as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None
    if generated:
        err.print(f"[muted]Generated {generated} into {paths.secrets_file()}.[/muted]")


@app.command()
def up(
    lock: Path | None = LockOption,
    manifest: Path | None = ManifestOption,
    bundle: list[Path] = BundleOption,
    force: bool = typer.Option(False, "--force", help="Start images built for another platform."),
) -> None:
    """Render the stack, pull its images, start and seed it, and wait until it is healthy."""
    loaded = _load(lock)
    models = load_manifest(manifest)
    _check_platforms(loaded, force)
    entries = _with_bundles(bundle)
    names = secret_names(entries)
    require(names, f"The bundles the stack serves ({sorted(entries)})")
    if names:
        err.print(f"[muted]Yield gets {names}, for {sorted(entries)}.[/muted]")
    else:
        err.print(
            "[warn]No bundle served: Yield gets no secret, and every run is refused at its "
            "gate, which requires the connector's secret. Pass --bundle.[/warn]"
        )
    _ensure_credentials()
    compose = project(_render(loaded, models, names))
    _record(entries)
    document = render.read(compose.file)
    runner = compose.runner
    try:
        if ensure_network(runner, models.network):
            err.print(f"[muted]Created the {models.network} network.[/muted]")
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
    out.print(_status_table(_rows(compose.ps(), document)))
    err.print("[ok]The stack is healthy.[/ok]")


@app.command()
def down() -> None:
    """Stop and remove the stack's containers. The stores and the registry keep their volumes."""
    compose, _ = started()
    try:
        compose.down()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    err.print("[ok]Stack stopped.[/ok]")


def _rows(states: dict[str, ServiceState], document: dict[str, Any]) -> list[dict[str, Any]]:
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
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show each service's image digest, state, health and ports."""
    compose, document = started()
    try:
        states = compose.ps()
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    rows = _rows(states, document)
    entries = served()
    if as_json:
        out.print_json(
            json.dumps({"file": str(compose.file), "bundles": entries, "services": rows})
        )
        return
    out.print(_status_table(rows))
    table = Table(title="Bundles served", title_justify="left")
    for column in ("client", "secrets Yield gets", "directory"):
        table.add_column(column)
    for client, entry in sorted(entries.items()):
        table.add_row(f"[name]{client}[/name]", ", ".join(entry["secrets"]), entry["directory"])
    out.print(table)


@app.command(name="forget-bundle")
def forget_bundle(client: str) -> None:
    """Stop serving a client's bundle: its secrets leave Yield on the next `up`."""
    entries = served()
    if client not in entries:
        err.print(
            f"[fail]No bundle {client!r} is served; the stack serves {sorted(entries)}.[/fail]"
        )
        raise typer.Exit(2)
    del entries[client]
    _record(entries)
    err.print(f"[ok]Forgot {client}.[/ok] Run `proof stack up` to take its secrets out of Yield.")


@app.command()
def logs(
    service: str,
    tail: int = typer.Option(200, "--tail", help="How many lines."),
) -> None:
    """Print the last lines of one service's logs."""
    compose, document = started()
    names = list(document["services"])
    if service not in names:
        err.print(f"[fail]No service {service!r}; the stack has {names}.[/fail]")
        raise typer.Exit(2)
    try:
        text = compose.logs(service, tail=tail)
    except ComposeFailed as error:
        err.print(f"[fail]docker compose failed.[/fail] {error}")
        raise typer.Exit(1) from None
    out.print(text, markup=False, highlight=False)
