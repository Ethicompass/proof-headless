"""`proof lens status|run`: where each of a client's Yield runs stands in Lens, and the launch that
gets it a result."""

from __future__ import annotations

import json
from typing import Any

import httpx
import typer
from rich.markup import escape
from rich.table import Table

from proof_headless import docker
from proof_headless.console import err, out
from proof_headless.lens import runs as yield_runs
from proof_headless.lens.api import DEFAULT_URL, LensApi, LensUnavailable, Status
from proof_headless.lens.ensure import Manual, Outcome, Waiting, ensure
from proof_headless.lens.runs import RunsUnknown, YieldRun

app = typer.Typer(help="Make sure every Yield run has its Lens result.", no_args_is_help=True)

LENS_OPTION = typer.Option(DEFAULT_URL, "--lens", help="lens-api on this appliance.")


def connect(url: str, timeout: float) -> httpx.Client:
    """The HTTP client for lens-api; a test replaces it with one over a fake."""
    return httpx.Client(base_url=url, timeout=timeout)


runner: docker.Runner = docker.run
"""The docker CLI the manual launch drives; a test replaces it with a fake daemon."""


def _refuse(message: str) -> typer.Exit:
    err.print(f"[fail]Refused.[/fail] {escape(message)}")
    return typer.Exit(2)


def _runs(client: str, chosen: list[str]) -> list[YieldRun]:
    try:
        if chosen:
            return [yield_runs.find(client, run_id) for run_id in chosen]
        return yield_runs.client_runs(client)
    except RunsUnknown as error:
        raise _refuse(str(error)) from None


def _row(run: YieldRun, status: Status | None) -> dict[str, Any]:
    return {
        "yield_run": run.run_id,
        "project_id": run.project_id,
        "run_id": run.lens_run_id,
        "received": status is not None,
        "admitted": bool(status and status.admitted),
        "stage": status.stage if status else None,
        "liveness": status.liveness if status else None,
        "attempts": status.attempts if status else 0,
        "result": status.result_ref if status else None,
    }


@app.command()
def status(
    client: str = typer.Argument(..., help="The client whose runs to read."),
    lens: str = LENS_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """For each Yield run of CLIENT: whether Lens received and admitted its delivery, the derived
    Lens ids, the run's stage and whether its result is there. Exits 1 when any run has none."""
    chosen = _runs(client, [])
    rows: list[dict[str, Any]] = []
    try:
        with connect(lens, 30.0) as http:
            api = LensApi(http)
            rows = [_row(run, api.status(run.project_id, run.lens_run_id)) for run in chosen]
    except LensUnavailable as error:
        raise _refuse(str(error)) from None
    if as_json:
        out.print_json(json.dumps(rows))
    else:
        table = Table(title=f"Lens results for {client}", title_justify="left")
        for column in ("yield run", "lens project", "received", "stage", "result"):
            table.add_column(column)
        for row in rows:
            received = "[ok]yes[/ok]" if row["received"] else "[fail]no[/fail]"
            stage = row["stage"] or "-"
            if row["liveness"]:
                stage += f" ({row['liveness']})"
            result = f"[ok]{row['result'][:19]}[/ok]" if row["result"] else "[warn]none[/warn]"
            table.add_row(
                f"[name]{row['yield_run']}[/name]", row["project_id"], received, stage, result
            )
        out.print(table)
    if any(not row["result"] for row in rows):
        raise typer.Exit(1)


def _report(outcome: Outcome) -> None:
    name = f"[name]{outcome.run.run_id}[/name]"
    for step in outcome.steps:
        err.print(f"[muted]{outcome.run.run_id}: {escape(step)}[/muted]")
    if outcome.ok:
        ref = outcome.status.result_ref if outcome.status else None
        said = f"result {ref}" if ref else f"{outcome.action}, not waited for"
        out.print(f"[ok]ok[/ok] {name} {outcome.action}: {escape(said)}")
    else:
        out.print(f"[fail]no result[/fail] {name} {outcome.action}: {escape(outcome.detail)}")


@app.command()
def run(
    client: str = typer.Argument(..., help="The client whose runs to bring to a result."),
    run_ids: list[str] = typer.Argument(None, help="Yield run ids. Default: every run of CLIENT."),
    lens: str = LENS_OPTION,
    manual: bool = typer.Option(
        False, "--manual", help="Launch by hand right away, without asking lens-api to relaunch."
    ),
    fallback: bool = typer.Option(
        True,
        "--fallback/--no-fallback",
        help="Launch by hand when lens-api's relaunch fails.",
    ),
    api_container: str | None = typer.Option(
        None, "--api-container", help="The lens-api container. Default: the one compose runs."
    ),
    image: str | None = typer.Option(
        None, "--image", help="The lens-run image. Default: lens-api's LENS_RUN_IMAGE."
    ),
    network: str | None = typer.Option(
        None,
        "--network",
        help="The job's network. Default: LENS_DOCKER_NETWORK, as lens-api's dispatcher uses.",
    ),
    timeout: float = typer.Option(
        3600.0, "--timeout", help="Seconds to wait for each result; 0 launches without waiting."
    ),
    interval: float = typer.Option(10.0, "--interval", help="Seconds between status reads."),
    as_json: bool = typer.Option(False, "--json", help="Print the outcomes as JSON."),
) -> None:
    """Launch lens-run for each run Lens admitted that has no result, and wait for the results.

    Idempotent: a run with its result, or with a live job, is not launched again. Exits 1 when any
    run ends without a result."""
    if manual and not fallback:
        raise _refuse("--manual and --no-fallback leave no way to launch")
    chosen = _runs(client, run_ids or [])
    mode = "only" if manual else ("fallback" if fallback else "never")
    kills = yield_runs.state_dir(client) / "lens" / "kills"
    how = Manual(runner, mode, api_container, image, network, kills)
    waiting = Waiting(timeout=timeout, interval=interval)
    outcomes: list[Outcome] = []
    try:
        with connect(lens, 60.0) as http:
            api = LensApi(http)
            for item in chosen:
                with err.status(f"Lens result for {item.run_id}"):
                    outcome = ensure(api, item, how, waiting)
                outcomes.append(outcome)
                if not as_json:
                    _report(outcome)
    except LensUnavailable as error:
        raise _refuse(str(error)) from None
    if as_json:
        out.print_json(
            json.dumps(
                [
                    {
                        **_row(o.run, o.status),
                        "ok": o.ok,
                        "action": o.action,
                        "detail": o.detail,
                        "steps": o.steps,
                    }
                    for o in outcomes
                ]
            )
        )
    if not all(o.ok for o in outcomes):
        raise typer.Exit(1)
