"""Making sure one Yield run ends with its Lens result: read, launch if needed, wait.

The decision is read off Lens's status, which Lens derives from its store and the daemon:

- no run under the derived ids: Lens never received the delivery, and nothing here can make Yield
  send it again;
- `done`: the result is there, and nothing is launched (a second `proof lens run` is a no-op);
- `awaiting_delivery`, `upstream_failed`, `registered`, `not_admitted`: there is nothing a launch
  could read;
- `accepted` or `running` with a live process: a job is on it, so this only waits;
- otherwise (`accepted` with no live process, `failed`, a `running` whose process is dead): launch.

The launch goes through lens-api's relaunch route first, which records a dead attempt's exit and
logs before it replaces the container (Lens ADR 0027). Only when that route cannot launch, or the
operator asks for it, is the job started by hand with lens-api's environment (`launcher`). This is
the entry point `proof run queue` calls after a run whose result does not arrive in time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from proof_headless.docker import Runner
from proof_headless.lens import launcher
from proof_headless.lens.api import DONE, FAILED, NOTHING_TO_LAUNCH, LensApi, Status
from proof_headless.lens.runs import YieldRun


@dataclass
class Outcome:
    """What became of one Yield run. `ok` is whether it ends with a result (or, without waiting,
    with a job on its way to one)."""

    run: YieldRun
    ok: bool
    action: str
    """`done`, `waited`, `launched`, `relaunched`, `already_running`, or `refused`."""
    status: Status | None = None
    detail: str = ""
    steps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Manual:
    """How to launch by hand: when, and what to override in what lens-api's container says."""

    runner: Runner
    mode: str = "fallback"
    """`fallback` after the relaunch route failed, `only` without asking that route, `never`."""
    api_container: str | None = None
    image: str | None = None
    network: str | None = None


@dataclass(frozen=True)
class Waiting:
    """How long to wait for the result. `timeout` 0 does not wait at all."""

    timeout: float = 3600.0
    interval: float = 10.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def _launch(lens: LensApi, run: YieldRun, manual: Manual, outcome: Outcome) -> bool:
    """Launch the run's job; whether a job is now on it."""
    project, run_id = run.project_id, run.lens_run_id
    if manual.mode != "only":
        said, reason = lens.relaunch(project, run_id)
        outcome.steps.append(f"lens-api relaunch: {said}" + (f" ({reason})" if reason else ""))
        if said in {"launched", "already_running"}:
            outcome.action = "relaunched" if said == "launched" else "already_running"
            return True
        if said != "launch_failed" or manual.mode == "never":
            outcome.action, outcome.detail = "refused", f"lens-api answered {said}: {reason or ''}"
            return False
    try:
        container = manual.api_container or launcher.api_container(manual.runner)
        environment = launcher.read_api(manual.runner, container)
        launched = launcher.launch(
            manual.runner,
            environment,
            project,
            run_id,
            image=manual.image,
            network=manual.network,
        )
    except launcher.LaunchRefused as error:
        outcome.action, outcome.detail = "refused", str(error)
        return False
    if launched.replaced is not None:
        outcome.steps.append(f"removed the dead {launched.container} (exit {launched.replaced})")
    outcome.steps.append(f"manual launch: {launched.launch} {launched.container}")
    outcome.action = launched.launch
    return True


def ensure(lens: LensApi, run: YieldRun, manual: Manual, waiting: Waiting) -> Outcome:
    """Bring `run` to a Lens result, launching once at most, and wait for it within `waiting`."""
    outcome = Outcome(run, ok=False, action="refused")
    status = lens.status(run.project_id, run.lens_run_id)
    outcome.status = status
    if status is None:
        outcome.detail = (
            f"Lens has no run {run.project_id}/{run.lens_run_id}: it never received Yield's "
            f"delivery for this run"
        )
        return outcome
    if status.stage == DONE:
        outcome.ok, outcome.action = True, "done"
        return outcome
    if status.stage in NOTHING_TO_LAUNCH:
        outcome.detail = f"the run is {status.stage}; there is nothing a launch could read"
        return outcome
    if status.alive:
        outcome.action = "waited"
    elif not _launch(lens, run, manual, outcome):
        return outcome
    if waiting.timeout <= 0:
        outcome.ok = True
        return outcome
    return _wait(lens, run, waiting, outcome)


def _wait(lens: LensApi, run: YieldRun, waiting: Waiting, outcome: Outcome) -> Outcome:
    """Poll until the result lands, the run fails, or the bound passes.

    A `failed` read right after a launch can still be the attempt before it, until the new job
    marks its own attempt, so a failure only counts once the attempt count has moved past the one
    seen before the launch, or the process Lens asks about is no longer alive.
    """
    before = outcome.status.attempts if outcome.status else 0
    deadline = waiting.clock() + waiting.timeout
    while True:
        status = lens.status(run.project_id, run.lens_run_id)
        outcome.status = status
        if status is not None and status.stage == DONE:
            outcome.ok = True
            return outcome
        if (
            status is not None
            and status.stage == FAILED
            and (status.attempts > before or outcome.action == "waited")
        ):
            code = f"exit {status.exit_code}" if status.exit_code is not None else "no exit code"
            outcome.detail = (
                f"lens-run failed ({code}{': ' + status.reason if status.reason else ''})"
            )
            return outcome
        if waiting.clock() >= deadline:
            stage = status.stage if status else "unknown"
            outcome.detail = f"no result within {waiting.timeout:.0f}s; the run is {stage}"
            return outcome
        waiting.sleep(waiting.interval)
