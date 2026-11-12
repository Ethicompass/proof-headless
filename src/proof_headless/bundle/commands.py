"""`proof bundle validate|load`: check a client's bundle, then publish it into the appliance."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from rich.table import Table

from proof_headless import docker, paths
from proof_headless.bundle import checks, loader
from proof_headless.bundle import manifest as bundles
from proof_headless.bundle.manifest import Bundle, BundleInvalid
from proof_headless.console import err, out
from proof_headless.secrets_commands import present

app = typer.Typer(help="Validate and load a client bundle.", no_args_is_help=True)


def _bundle(directory: Path) -> Bundle:
    try:
        return bundles.load(directory)
    except BundleInvalid as error:
        err.print(f"[fail]Invalid bundle.[/fail] {error}")
        raise typer.Exit(2) from None


def _problems(bundle: Bundle) -> list[checks.Problem]:
    return checks.check(bundle, present())


def receipt_path(bundle: Bundle) -> Path:
    """Where the last load of this client's bundle is recorded."""
    return paths.home() / "bundles" / bundle.client / "receipt.json"


@app.command()
def validate(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    as_json: bool = typer.Option(False, "--json", help="Print the problems as JSON."),
) -> None:
    """Check the bundle without publishing anything."""
    bundle = _bundle(directory)
    problems = _problems(bundle)
    if as_json:
        out.print_json(json.dumps([{"where": p.where, "what": p.what} for p in problems]))
    elif problems:
        table = Table(title=f"Bundle {bundle.client}: problems", title_justify="left")
        table.add_column("where")
        table.add_column("what")
        for problem in problems:
            table.add_row(problem.where, problem.what)
        out.print(table)
    else:
        counts = {
            "catalogues": len(bundle.catalogues),
            "profiles": len(bundle.profiles),
            "priors": len(bundle.priors),
            "mappings": len(bundle.mappings),
            "brains": len(bundle.brains),
            "declarations": len(bundle.declarations),
        }
        listed = ", ".join(f"{n} {kind}" for kind, n in counts.items())
        out.print(f"[ok]Valid.[/ok] Bundle [name]{bundle.client}[/name]: {listed}.")
    if problems:
        raise typer.Exit(1)


@app.command(name="load")
def load_command(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    compute: str = typer.Option(
        "http://127.0.0.1:8081", "--compute", help="Yield compute on this appliance."
    ),
    registry: str = typer.Option(
        "localhost:5051", "--registry", help="The brain registry on this appliance."
    ),
    insecure: bool | None = typer.Option(
        None,
        "--insecure/--no-insecure",
        help="Push brains over plain HTTP. Default: yes for a localhost or 127.0.0.1 registry, "
        "otherwise the brain's own vitruvio.toml decides.",
    ),
    skip_brains: bool = typer.Option(
        False, "--skip-brains", help="Do not push brains; keep the last receipt's digests."
    ),
) -> None:
    """Validate, then publish every piece of the bundle and record a receipt."""
    bundle = _bundle(directory)
    problems = _problems(bundle)
    if problems:
        for problem in problems:
            err.print(f"[fail]{problem.where}:[/fail] {problem.what}")
        err.print("Fix these with [b]proof bundle validate[/b] before loading.")
        raise typer.Exit(1)
    local = registry.startswith(("localhost", "127.0.0.1"))
    plain = insecure if insecure is not None else (True if local else None)
    path = receipt_path(bundle)
    with httpx.Client(base_url=compute, timeout=120.0) as client, err.status("Loading") as status:
        receipt = loader.load(
            bundle,
            client,
            registry=registry,
            declarations_dir=paths.home() / "declarations",
            runner=docker.run,
            insecure=plain,
            previous=loader.read_receipt(path),
            skip_brains=skip_brains,
            progress=lambda what: status.update(f"Loading {what}"),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.document(), indent=2) + "\n")
    table = Table(title=f"Loaded {bundle.client}", title_justify="left")
    for column in ("kind", "name", "version", "digest", "new"):
        table.add_column(column)
    for item in receipt.loaded:
        new = "kept" if item.carried else {True: "yes", False: "unchanged", None: ""}[item.created]
        table.add_row(item.kind, item.name, str(item.version or ""), (item.digest or "")[:19], new)
    out.print(table)
    err.print(f"[muted]Receipt: {path}[/muted]")
    if not receipt.complete:
        err.print(f"[fail]Load stopped.[/fail] {receipt.error}")
        err.print("The receipt is marked incomplete; fix the piece and load again.")
        raise typer.Exit(1)
