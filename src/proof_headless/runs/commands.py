"""`proof run queue|status|watch|remeasure`: a bundle's runs through Yield, without a human polling.

Every command takes the bundle's directory, which names the client, and reads the specs
`proof spec render` wrote to `$PROOF_HOME/bundles/<client>/specs/`. The queue's state and its event
log live beside them (see `runs.state`). Yield's API and compute default to the appliance's own
loopback ports.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.markup import escape
from rich.table import Table

from proof_headless import paths
from proof_headless.bundle import manifest as bundles
from proof_headless.bundle.manifest import BundleInvalid
from proof_headless.console import err, out
from proof_headless.docker import Compose
from proof_headless.endpoints import LENS_API, YIELD_API, YIELD_COMPUTE
from proof_headless.lens import commands as lens_commands
from proof_headless.lens.api import LensApi
from proof_headless.lens.ensure import Manual
from proof_headless.models import manifest as manifests
from proof_headless.models import render
from proof_headless.models.manifest import ManifestInvalid
from proof_headless.runs import remeasure as remeasuring
from proof_headless.runs.anomalies import Detector
from proof_headless.runs.board import Board, Sampler, event_line, plain
from proof_headless.runs.client import Unreachable, Yield, metric_outcomes
from proof_headless.runs.lens_follow import LensFollower
from proof_headless.runs.observe import STACK_PROJECT, JobContainers, Observation, Observer
from proof_headless.runs.queue import Policy, Queue, new_state, read_specs
from proof_headless.runs.state import (
    TERMINAL,
    Event,
    QueueBusy,
    QueueState,
    RunState,
    Severity,
    Store,
    specs_dir,
    stamp,
    state_dir,
)

app = typer.Typer(help="Queue, watch and re-measure a bundle's runs.", no_args_is_help=True)

ApiOption = typer.Option(YIELD_API, "--api", help="Yield's API on this appliance.")
ComputeOption = typer.Option(YIELD_COMPUTE, "--compute", help="Yield compute on this appliance.")
ManifestOption = typer.Option(
    None,
    "--manifest",
    "-m",
    help="The models manifest whose health to watch. Default: $PROOF_HOME/models.yaml, if any.",
)


def _refuse(message: str) -> typer.Exit:
    err.print(f"[fail]Refused.[/fail] {escape(message)}")
    return typer.Exit(2)


def _client_of(directory: Path) -> str:
    try:
        return bundles.load(directory).client
    except BundleInvalid as error:
        raise _refuse(f"invalid bundle: {error}") from None


def _yield(api: str, compute: str) -> Yield:
    return Yield(
        httpx.Client(base_url=api, timeout=30.0), httpx.Client(base_url=compute, timeout=30.0)
    )


def _models(manifest: Path | None) -> tuple[Compose | None, tuple[str, ...]]:
    """The models' compose project and names, read without rendering anything: watching writes
    nothing, so a manifest never brought up has no compose file and no health to show."""
    path = manifest or paths.default_manifest()
    if not path.exists():
        if manifest is not None:
            raise _refuse(f"no models manifest at {path}")
        return None, ()
    try:
        loaded = manifests.load(path)
    except ManifestInvalid as error:
        raise _refuse(f"invalid manifest: {error}") from None
    compose = Compose(project=render.PROJECT, file=paths.home() / "models" / "compose.yaml")
    return compose, tuple(model.name for model in loaded.models)


def _observer(client: Yield, manifest: Path | None) -> Observer:
    compose, names = _models(manifest)
    return Observer(
        client=client,
        containers=JobContainers(),
        models=compose,
        model_names=names,
        stack_project=STACK_PROJECT,
    )


def _report(event: Event) -> None:
    err.print(event_line(event))


def _summary(state: QueueState) -> Table:
    table = Table(title=f"Runs for {state.client}", title_justify="left")
    for column in ("run", "queue", "phase", "closed", "failed", "metrics", "lens", "error"):
        table.add_column(column)
    for entry in state.runs:
        style = {RunState.COMPLETE: "ok"}.get(
            entry.state, "fail" if entry.state in TERMINAL else ""
        )
        table.add_row(
            f"[name]{escape(entry.run_id)}[/name]",
            f"[{style}]{entry.state}[/{style}]" if style else str(entry.state),
            entry.phase or "-",
            f"{entry.closed}/{entry.planned if entry.planned is not None else '?'}",
            str(entry.failed),
            ", ".join(f"{m} {s}" for m, s in entry.metrics.items()),
            entry.lens or "-",
            escape(entry.error or ""),
        )
    return table


@app.command(name="queue")
def queue_command(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    only: list[str] = typer.Option([], "--only", help="Queue only these runs (repeatable)."),
    concurrency: int | None = typer.Option(
        None, "--concurrency", "-c", min=1, help="Runs in flight at once. Default: 2."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Continue the recorded queue; finished runs are not posted again."
    ),
    retries: int = typer.Option(
        1, "--retries", min=0, help="Identical re-POSTs of a run Yield reads as failed."
    ),
    stall_minutes: float = typer.Option(
        15.0, "--stall-minutes", min=1, help="Minutes without progress that make a run stalled."
    ),
    poll_seconds: float = typer.Option(
        20.0, "--poll-seconds", min=1, help="Seconds between polls."
    ),
    api: str = ApiOption,
    compute: str = ComputeOption,
    manifest: Path | None = ManifestOption,
    lens: str = typer.Option(LENS_API, "--lens", help="lens-api on this appliance."),
    lens_wait_minutes: float = typer.Option(
        10.0,
        "--lens-wait-minutes",
        min=0,
        help="Minutes a complete run's Lens result may take before the queue launches Lens's job.",
    ),
    follow_lens: bool = typer.Option(
        True, "--lens-follow/--no-lens-follow", help="Follow each complete run to Lens."
    ),
) -> None:
    """Submit the bundle's rendered specs to Yield and wait for every run to close and, unless
    --no-lens-follow, to reach Lens."""
    client_name = _client_of(directory)
    store = Store(state_dir(client_name))
    recorded = store.read()
    try:
        if resume:
            if recorded is None:
                raise _refuse(f"no queue is recorded for {client_name}; start one without --resume")
            if only:
                raise _refuse("--only chooses the runs of a new queue; --resume keeps the recorded")
            state = recorded
            if concurrency is not None:
                state.concurrency = concurrency
            specs = read_specs(specs_dir(client_name))
        else:
            if recorded is not None and not recorded.done:
                raise _refuse(
                    f"the queue recorded for {client_name} has unfinished runs; continue it with "
                    f"--resume"
                )
            specs = read_specs(specs_dir(client_name), only)
            if not specs:
                raise _refuse(
                    f"no rendered specs in {specs_dir(client_name)}; run proof spec render first"
                )
            state = new_state(client_name, specs, concurrency or 2, datetime.now(UTC))
    except KeyError as error:
        raise _refuse(str(error.args[0])) from None
    policy = Policy(
        concurrency=state.concurrency,
        retries=retries,
        stall_after=timedelta(minutes=stall_minutes),
        lens_wait=timedelta(minutes=lens_wait_minutes),
    )
    client = _yield(api, compute)
    follower = None
    if follow_lens:
        kills = state_dir(client_name) / "lens" / "kills"
        follower = LensFollower(
            api=LensApi(lens_commands.connect(lens, 60.0)),
            client=client_name,
            manual=Manual(lens_commands.runner, "fallback", kills=kills),
        )
    observer = _observer(client, manifest)
    queue = Queue(
        client=client,
        store=store,
        state=state,
        specs=specs,
        policy=policy,
        observe=observer,
        report=_report,
        lens=follower,
    )
    try:
        with store.lock():
            queue.save()
            queue.run(time.sleep, poll_seconds)
    except QueueBusy as error:
        raise _refuse(str(error)) from None
    except KeyboardInterrupt:
        queue.save()
        err.print("[warn]Interrupted.[/warn] Continue with [b]proof run queue --resume[/b].")
        raise typer.Exit(130) from None
    out.print(_summary(state))
    err.print(f"[muted]State: {store.queue_path}; events: {store.events_path}[/muted]")
    if not state.succeeded:
        raise typer.Exit(1)


def _rows(board: Board) -> list[dict[str, Any]]:
    return [{**asdict(row), "counts": row.counts} for row in board.runs]


def _rendered(client_name: str) -> list[str]:
    directory = specs_dir(client_name)
    return list(read_specs(directory)) if directory.exists() else []


@app.command()
def status(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    api: str = ApiOption,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show each run's place in the queue and in Yield."""
    client_name = _client_of(directory)
    store = Store(state_dir(client_name))
    observer = Observer(client=_yield(api, YIELD_COMPUTE))
    errors: list[str] = []

    def look(run_ids: list[str]) -> Observation:
        statuses, error = observer.statuses(run_ids)
        if error:
            errors.append(error)
        return Observation(at=datetime.now(UTC), runs=statuses, yield_error=error)

    board = Sampler(
        store=store,
        client=client_name,
        run_ids=_rendered(client_name),
        observe=look,
    ).sample()
    recorded = store.read()
    if as_json:
        out.print_json(
            json.dumps(
                {
                    "client": client_name,
                    "queue": recorded.model_dump(mode="json") if recorded else None,
                    "runs": _rows(board),
                    "yield_error": errors[0] if errors else None,
                }
            )
        )
        return
    table = Table(title=f"Runs for {client_name}", title_justify="left")
    for column in ("run", "queue", "phase", "closed", "failed", "liveness", "note"):
        table.add_column(column)
    for row in board.runs:
        table.add_row(
            f"[name]{escape(row.run_id)}[/name]",
            row.queue,
            row.phase,
            row.counts,
            str(row.failed),
            row.liveness,
            escape(row.note),
        )
    out.print(table)
    if errors:
        err.print(
            f"[warn]Yield did not answer; phases are the queue's last record.[/warn] {errors[0]}"
        )


def watch_plain(
    sampler: Sampler,
    interval: float,
    *,
    count: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Print each sample's changes as Rich lines, `count` times or until interrupted."""
    previous: Board | None = None
    taken = 0
    while True:
        board = sampler.sample()
        for line in plain(board, previous):
            out.print(line, highlight=False)
        previous = board
        taken += 1
        if count and taken >= count:
            return
        sleep(interval)


def snapshot(board: Board) -> dict[str, Any]:
    """One board as JSON: what `watch --json` prints."""
    return {
        "at": board.at.isoformat(),
        "client": board.client,
        "models": board.models,
        "stack": board.stack,
        "runs": _rows(board),
        "anomalies": [
            {
                "kind": str(a.kind),
                "severity": str(a.severity),
                "subject": a.subject,
                "run_id": a.run_id,
                "message": a.message,
            }
            for a in board.anomalies
        ],
        "events": [event.model_dump(mode="json", exclude_none=True) for event in board.events],
    }


def _watcher(store: Store, *, one_shot: bool) -> Detector:
    """The detector a watch starts with.

    A watch that keeps sampling reads log lines only from its launch: the history is in the
    queue's event log, which it shows. A single sample has no later sample to see news in, so it
    reads from the queue's last look and marks, or the whole tail when no queue has looked.
    """
    if not one_shot:
        return Detector(since=datetime.now(UTC))
    recorded = store.read()
    detector = Detector()
    if recorded is not None and recorded.observed_at is not None:
        detector.since = datetime.fromisoformat(recorded.observed_at)
        detector.read_until.update(
            {name: datetime.fromisoformat(mark) for name, mark in recorded.log_marks.items()}
        )
    return detector


@app.command()
def watch(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    plain_mode: bool = typer.Option(
        False,
        "--plain",
        help="Print Rich lines instead of the dashboard. Implied without a terminal.",
    ),
    interval: float = typer.Option(30.0, "--interval", min=1, help="Seconds between samples."),
    count: int = typer.Option(0, "--count", min=0, help="Stop after this many samples; 0 never."),
    api: str = ApiOption,
    compute: str = ComputeOption,
    manifest: Path | None = ManifestOption,
    as_json: bool = typer.Option(
        False, "--json", help="Print one sample as JSON: the runs, the models and the anomalies."
    ),
) -> None:
    """Watch the runs, the models, the stack and the anomalies. Reads only; never changes
    anything."""
    client_name = _client_of(directory)
    store = Store(state_dir(client_name))
    observer = _observer(_yield(api, compute), manifest)
    sampler = Sampler(
        store=store,
        client=client_name,
        run_ids=_rendered(client_name),
        observe=observer,
        detector=_watcher(store, one_shot=as_json),
    )
    if as_json:
        out.print_json(json.dumps(snapshot(sampler.sample())))
        return
    if plain_mode or not (sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            watch_plain(sampler, interval, count=count)
        except KeyboardInterrupt:
            return
        return
    from proof_headless.runs.dashboard import WatchApp

    WatchApp(sampler, interval).run()


ENDINGS = {
    remeasuring.Ending.FAILED: "the measure job failed",
    remeasuring.Ending.DEAD: "the measure job stopped beating; its container is likely dead",
    remeasuring.Ending.OLD_RECORD: "compute answered with an older job's record; this request "
    "was dropped and nothing was measured for it",
    remeasuring.Ending.MISSING: "compute never showed a job for the request",
    remeasuring.Ending.TIMED_OUT: "the measure job did not end in time",
}


@app.command(name="remeasure")
def remeasure_command(
    directory: Path = typer.Argument(..., help="The bundle's directory."),
    run: str = typer.Argument(..., help="The failed run to measure."),
    metric: list[str] = typer.Option(
        [], "--metric", help="Measure only this metric (repeatable). Default: all of the run's."
    ),
    webhook_url: str | None = typer.Option(
        None, "--webhook-url", help="Where compute delivers the results. Default: the spec's."
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for the measure job to end."),
    timeout_minutes: float = typer.Option(
        240.0, "--timeout-minutes", min=1, help="How long --wait waits for the job."
    ),
    poll_seconds: float = typer.Option(
        15.0, "--poll-seconds", min=1, help="Seconds between polls."
    ),
    api: str = ApiOption,
    compute: str = ComputeOption,
    as_json: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
) -> None:
    """Measure a failed run's stored dataset again, without attacking again.

    Only a run Yield reads as failed, with no manifest, and no measure record compute still
    holds: a complete run's results cannot change (see `runs.remeasure`).
    """
    client_name = _client_of(directory)
    path = specs_dir(client_name) / f"{run}.json"
    if not path.exists():
        raise _refuse(f"no rendered spec for {run} at {path}")
    spec = json.loads(path.read_text())
    client = _yield(api, compute)
    job_id = remeasuring.job_id(run)
    try:
        current = client.status(run)
        manifest = client.result(run) if current is not None else None
        held = client.job(job_id) if current is not None else None
        remeasuring.check(current, manifest, held)
        request = remeasuring.body(spec, metric, webhook_url=webhook_url)
    except remeasuring.Refused as error:
        raise _refuse(str(error)) from None
    except Unreachable as error:
        err.print(f"[fail]Yield did not answer.[/fail] {escape(str(error))}")
        raise typer.Exit(1) from None
    if "webhook_url" not in request:
        err.print(
            "[warn]No webhook:[/warn] the results stay in Yield's bucket, named by the manifest."
        )
    store = Store(state_dir(client_name))
    posted_at = datetime.now(UTC)
    try:
        answered = client.measure(request)
    except Unreachable as error:
        err.print(f"[fail]Compute refused the measurement.[/fail] {escape(str(error))}")
        raise typer.Exit(1) from None
    store.append(
        Event(
            at=stamp(posted_at),
            kind="remeasure",
            run_id=run,
            message=f"measuring {', '.join(request['metric_ids'])} as job {answered}",
        )
    )
    err.print(f"Measuring [name]{escape(run)}[/name] as job {escape(answered)}.")
    outcome: dict[str, Any] = {
        "run_id": run,
        "job_id": answered,
        "metric_ids": request["metric_ids"],
        "state": "launched",
        "detail": "",
        "metrics": {},
    }
    failed = False
    if wait:
        try:
            with err.status("Measuring") as spinner:
                waited = remeasuring.wait(
                    client,
                    answered,
                    posted_at=posted_at,
                    timeout=timedelta(minutes=timeout_minutes),
                    poll_seconds=poll_seconds,
                    sleep=time.sleep,
                    progress=lambda j: spinner.update(
                        f"Measuring: {escape(j.state)} {escape(j.detail)}"
                    ),
                )
            manifest = client.result(run) or {}
        except Unreachable as error:
            err.print(f"[fail]Lost Yield while waiting.[/fail] {escape(str(error))}")
            raise typer.Exit(1) from None
        failed = waited.ending is not remeasuring.Ending.SUCCEEDED
        metrics = {m: s for m, s in metric_outcomes(manifest).items() if m in request["metric_ids"]}
        outcome.update(
            {
                "state": str(waited.ending),
                "detail": ENDINGS.get(waited.ending, waited.job.detail if waited.job else ""),
                "metrics": metrics if not failed else {},
            }
        )
        store.append(
            Event(
                at=stamp(datetime.now(UTC)),
                kind="remeasured",
                severity=Severity.ERROR if failed else Severity.INFO,
                run_id=run,
                message=f"job {answered} {waited.ending}"
                + (f": {outcome['detail']}" if failed else "")
                + (
                    " (" + ", ".join(f"{m} {s}" for m, s in metrics.items()) + ")"
                    if metrics
                    else ""
                ),
            )
        )
    if as_json:
        out.print_json(json.dumps(outcome))
    elif wait:
        table = Table(title=f"Measured {run}: {outcome['state']}", title_justify="left")
        table.add_column("metric")
        table.add_column("status")
        for name in request["metric_ids"]:
            table.add_row(name, outcome["metrics"].get(name, "-"))
        out.print(table)
    if failed:
        err.print(f"[fail]{escape(str(outcome['detail']))}[/fail]")
        raise typer.Exit(1)
