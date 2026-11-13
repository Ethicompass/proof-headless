"""`proof spec render`: one Yield run spec per run of a bundle's matrix; `proof spec plan`: what
each will produce.

This replaces the spec builder each engagement used to write for itself. The specs go to the
bundle's state directory under `$PROOF_HOME`, never into the bundle or this repository, because
they pin versions and digests of one load on one appliance.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from rich.markup import escape
from rich.table import Table

from proof_headless import paths
from proof_headless.bundle import manifest as bundles
from proof_headless.bundle.commands import receipt_path
from proof_headless.bundle.manifest import BundleInvalid
from proof_headless.console import err, out
from proof_headless.endpoints import YIELD_COMPUTE
from proof_headless.models import manifest as manifests
from proof_headless.models.manifest import ManifestInvalid
from proof_headless.runs.queue import read_specs
from proof_headless.runs.state import specs_dir
from proof_headless.spec import matrix as matrices
from proof_headless.spec.matrix import MatrixInvalid
from proof_headless.spec.plan import Planner
from proof_headless.spec.render import Receipt, Refused, Renderer, write

app = typer.Typer(help="Render Yield run specs from a bundle.", no_args_is_help=True)

ROLES_FILE = "roles.toml"


def _refuse(message: str) -> typer.Exit:
    err.print(f"[fail]Refused.[/fail] {escape(message)}")
    return typer.Exit(2)


def _bindings(values: list[str]) -> dict[str, str]:
    bound: dict[str, str] = {}
    for value in values:
        role, separator, model = value.partition("=")
        if not separator or not role or not model:
            raise _refuse(f"--bind takes ROLE=MODEL, got {value!r}")
        bound[role] = model
    return bound


@app.command()
def render(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    manifest: Path | None = typer.Option(
        None, "--manifest", "-m", help="The models manifest. Default: $PROOF_HOME/models.yaml."
    ),
    roles: Path | None = typer.Option(
        None, "--roles", help=f"The role bindings. Default: the bundle's {ROLES_FILE}."
    ),
    bind: list[str] = typer.Option(
        [], "--bind", help="ROLE=MODEL, binding a role to a manifest model; repeatable."
    ),
    out_dir: Path | None = typer.Option(
        None, "--out", help="Where to write. Default: $PROOF_HOME/bundles/<client>/specs."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the specs as JSON."),
) -> None:
    """Render one run spec per run of the bundle's matrix, pinned to its last load."""
    try:
        bundle = bundles.load(directory)
        models = manifests.load(manifest or paths.default_manifest())
        if bundle.matrix is None:
            raise Refused(f"bundle {bundle.client} names no matrix in bundle.toml")
        run_matrix = matrices.load_matrix(bundle.matrix)
        bindings = matrices.load_roles(roles or bundle.root / ROLES_FILE).bound(_bindings(bind))
        receipt = Receipt.read(receipt_path(bundle))
        rendered = Renderer(bundle, run_matrix, bindings, receipt, models).render()
    except (BundleInvalid, ManifestInvalid, MatrixInvalid, Refused) as error:
        raise _refuse(str(error)) from None
    target = out_dir or paths.home() / "bundles" / bundle.client / "specs"
    written = write(rendered, target)
    if as_json:
        out.print_json(
            json.dumps(
                [
                    {"run_id": item.run.id, "path": str(path), "spec": item.spec}
                    for item, path in zip(rendered, written, strict=True)
                ]
            )
        )
        return
    table = Table(title=f"Run specs for {bundle.client}", title_justify="left")
    for column in ("run", "catalogue", "strategies", "metrics", "judge model"):
        table.add_column(column)
    for item in rendered:
        table.add_row(
            f"[name]{item.run.id}[/name]",
            f"{item.run.catalogue} v{item.spec['catalogue_versions'][item.run.catalogue]}",
            ", ".join(item.run.strategies + item.run.plugins),
            ", ".join(item.run.metric_ids),
            item.judge_model,
        )
    out.print(table)
    err.print(f"[muted]Written to {target}[/muted]")


def _compute_client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=60.0)


@app.command()
def plan(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    only: list[str] = typer.Option([], "--only", help="Plan only these runs (repeatable)."),
    compute: str = typer.Option(
        YIELD_COMPUTE, "--compute", help="Yield compute on this appliance."
    ),
    timeout_minutes: float = typer.Option(
        30.0, "--timeout-minutes", min=1, help="How long to wait for all generations."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the plans as JSON."),
) -> None:
    """Count the probes and traces each rendered spec will produce, with Yield's own generation,
    before any run touches the target."""
    try:
        client_name = bundles.load(directory).client
    except BundleInvalid as error:
        raise _refuse(f"invalid bundle: {error}") from None
    try:
        found = read_specs(specs_dir(client_name), only)
    except KeyError as error:
        raise _refuse(str(error.args[0])) from None
    if not found:
        raise _refuse(f"no rendered specs in {specs_dir(client_name)}; run proof spec render first")
    specs = {run: json.loads(path.read_text()) for run, path in found.items()}
    planner = Planner(_compute_client(compute), timeout_seconds=timeout_minutes * 60)
    with err.status(f"Generating {len(specs)} probe sets in Yield compute"):
        plans, failed = planner.plan(specs)
    if as_json:
        out.print_json(
            json.dumps(
                {
                    "plans": [
                        {
                            "run_id": p.run_id,
                            "preview_id": p.preview_id,
                            "probes": p.probes,
                            "probes_by_strategy": p.probes_by_strategy,
                            "replicas": p.replicas,
                            "traces": p.traces,
                            "max_target_calls": p.max_target_calls,
                            "warnings": list(p.warnings),
                        }
                        for p in plans
                    ],
                    "failed": failed,
                    "probes": sum(p.probes for p in plans),
                    "traces": sum(p.traces for p in plans),
                }
            )
        )
    else:
        table = Table(title=f"Plan for {client_name}", title_justify="left", show_footer=True)
        table.add_column("run", footer="total")
        table.add_column("probes by strategy")
        table.add_column("probes", justify="right", footer=str(sum(p.probes for p in plans)))
        table.add_column("replicas", justify="right")
        table.add_column("traces", justify="right", footer=str(sum(p.traces for p in plans)))
        table.add_column("max calls", justify="right")
        for p in plans:
            table.add_row(
                f"[name]{p.run_id}[/name]",
                "\n".join(f"{name} {count}" for name, count in p.probes_by_strategy.items()),
                str(p.probes),
                str(p.replicas),
                str(p.traces),
                "" if p.max_target_calls is None else str(p.max_target_calls),
            )
        out.print(table)
        for p in plans:
            for warning in p.warnings:
                err.print(f"[warn]{escape(warning)}[/warn]")
        err.print(
            "[muted]Traces are probes times replicas; a run's Exploiter adds its own within the "
            "same budget.[/muted]"
        )
    for run_id, reason in failed.items():
        err.print(f"[fail]{escape(run_id)}[/fail] {escape(reason)}")
    if failed:
        raise typer.Exit(1)
