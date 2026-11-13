"""`proof evidence export|verify`: one checksummed package per engagement, and its check."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.markup import escape
from rich.table import Table

from proof_headless import docker, paths, secrets
from proof_headless.console import err, out
from proof_headless.evidence import package
from proof_headless.evidence.collect import Collector, Endpoints, NothingToExport
from proof_headless.evidence.export import Refused, export
from proof_headless.evidence.s3 import Credentials, Store

app = typer.Typer(help="Export and verify an engagement's evidence package.", no_args_is_help=True)

ACCESS_KEY = "YIELD_S3_ACCESS_KEY"
SECRET_KEY = "YIELD_S3_SECRET_KEY"
"""The names Yield's compose reads its store credentials from, and so the names the operator's
secrets file holds them under."""


def connect(timeout: float) -> httpx.Client:
    """The HTTP client every source is read through; a test replaces it with one over fakes."""
    return httpx.Client(timeout=timeout)


runner: docker.Runner = docker.run
"""The docker CLI the image digests are read with; a test replaces it."""


@app.command(name="export")
def export_command(
    client: str = typer.Argument(..., help="The client whose engagement to export."),
    destination: Path | None = typer.Option(
        None, "--out", help="The package directory. Default: $PROOF_HOME/exports/<client>-<time>."
    ),
    manifest: Path | None = typer.Option(
        None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
    ),
    yield_api: str = typer.Option(Endpoints.yield_api, "--yield", help="Yield's API."),
    lens_api: str = typer.Option(Endpoints.lens_api, "--lens", help="lens-api."),
    store: str = typer.Option("http://127.0.0.1:9000", "--store", help="Yield's S3 store."),
    bucket: str = typer.Option("yield", "--bucket", help="Yield's bucket."),
    no_store: bool = typer.Option(False, "--no-store", help="Do not export the store's objects."),
    as_json: bool = typer.Option(False, "--json", help="Print the index as JSON."),
) -> None:
    """Gather CLIENT's runs, results, reports, manifests and receipt into a checksummed package.

    Refused, and nothing written, when any value of the secrets file appears in any file."""
    try:
        held = secrets.values(paths.secrets_file())
    except secrets.SecretsFileExposed as error:
        err.print(f"[fail]Refused.[/fail] {error}")
        raise typer.Exit(2) from None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = destination or paths.home() / "exports" / f"{client}-{stamp}"
    with connect(60.0) as http:
        reader: Store | None = None
        gap: str | None = "--no-store" if no_store else None
        if not no_store:
            if ACCESS_KEY in held and SECRET_KEY in held:
                reader = Store(http, store, bucket, Credentials(held[ACCESS_KEY], held[SECRET_KEY]))
            else:
                gap = f"{ACCESS_KEY} and {SECRET_KEY} are not both in the secrets file"
        collector = Collector(
            client,
            target,
            http,
            runner,
            Endpoints(yield_api=yield_api, lens_api=lens_api),
            store=reader,
            store_gap=gap,
            manifest=manifest,
        )
        try:
            with err.status(f"Exporting {client}"):
                index = export(collector, target, held)
        except NothingToExport as error:
            err.print(f"[fail]Refused.[/fail] {escape(str(error))}")
            raise typer.Exit(2) from None
        except Refused as refused:
            err.print(f"[fail]Refused.[/fail] {escape(str(refused))}; nothing was written.")
            for leak in refused.leaks:
                err.print(f"  [name]{escape(leak.path)}[/name] holds the value of {leak.secret}")
            for path in refused.unreadable:
                err.print(f"  [name]{escape(path)}[/name] does not decompress")
            raise typer.Exit(1) from None
    for name in index["unscanned_secrets"]:
        err.print(
            f"[warn]not scanned[/warn] {name}: its value is shorter than "
            f"{package.MIN_SECRET_LENGTH} characters, too short to match without false refusals"
        )
    if as_json:
        out.print_json(json.dumps({**index, "path": str(target)}))
    else:
        for gap_entry in index["gaps"]:
            err.print(f"[warn]gap[/warn] {escape(gap_entry['what'])}: {escape(gap_entry['why'])}")
        out.print(
            f"[ok]Exported.[/ok] {len(index['runs'])} runs, {len(index['gaps'])} gaps: {target}"
        )


@app.command(name="verify")
def verify_command(
    directory: Path = typer.Argument(..., help="An evidence package."),
    as_json: bool = typer.Option(False, "--json", help="Print the problems as JSON."),
) -> None:
    """Re-check every file of a package against its SHA256SUMS. Exits 1 on any difference."""
    if not directory.is_dir():
        err.print(f"[fail]Refused.[/fail] {escape(str(directory))} is not a directory")
        raise typer.Exit(2)
    problems = package.verify(directory)
    if as_json:
        out.print_json(json.dumps([{"path": p.path, "problem": p.problem} for p in problems]))
    elif problems:
        table = Table(title=f"{directory}: does not verify", title_justify="left")
        table.add_column("path")
        table.add_column("problem")
        for problem in problems:
            table.add_row(escape(problem.path), f"[fail]{problem.problem}[/fail]")
        out.print(table)
    else:
        count = sum(1 for _ in package.files(directory)) - 1
        out.print(f"[ok]Verified.[/ok] {count} files match {package.SUMS}.")
    if problems:
        raise typer.Exit(1)
