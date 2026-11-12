"""The queue's state machine against a fake Yield: concurrency, resume, failures and stalls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from proof_headless.runs.queue import Policy, Queue, new_state, read_specs
from proof_headless.runs.state import QueueState, RunState, Store
from tests.fake_yield import HAPPY, FakeYield, status

START = datetime(2026, 11, 12, 22, 0, tzinfo=UTC)


class Clock:
    def __init__(self, step: timedelta = timedelta(seconds=20)) -> None:
        self.at = START
        self.step = step

    def __call__(self) -> datetime:
        return self.at

    def sleep(self, _: float) -> None:
        self.at += self.step


def _specs(tmp_path: Path, *runs: str) -> dict[str, Path]:
    directory = tmp_path / "specs"
    directory.mkdir(exist_ok=True)
    for run in runs:
        (directory / f"{run}.json").write_text(json.dumps({"run_id": run, "metric_ids": ["bias"]}))
    return read_specs(directory)


def _queue(
    tmp_path: Path,
    fake: FakeYield,
    specs: dict[str, Path],
    *,
    policy: Policy | None = None,
    clock: Clock | None = None,
    state: QueueState | None = None,
) -> Queue:
    clock = clock or Clock()
    policy = policy or Policy()
    store = Store(tmp_path / "state")
    return Queue(
        client=fake.client(),
        store=store,
        state=state or new_state("acme", specs, policy.concurrency, clock()),
        specs=specs,
        policy=policy,
        now=clock,
    )


def _run(queue: Queue, clock: Clock, ticks: int = 200) -> QueueState:
    for _ in range(ticks):
        queue.tick()
        if queue.state.done:
            break
        clock.sleep(0)
    return queue.state


def test_no_more_runs_are_in_flight_than_the_concurrency_allows(tmp_path: Path) -> None:
    runs = ("r1", "r2", "r3", "r4")
    fake = FakeYield({run: HAPPY for run in runs})
    clock = Clock()
    specs = _specs(tmp_path, *runs)
    queue = _queue(tmp_path, fake, specs, policy=Policy(concurrency=2), clock=clock)
    state = _run(queue, clock)
    assert state.succeeded
    assert fake.posts == list(runs)
    assert fake.in_flight_max == 2


def test_a_run_yield_closed_or_is_measuring_is_adopted_and_never_posted(tmp_path: Path) -> None:
    fake = FakeYield({"done": HAPPY, "busy": HAPPY, "new": HAPPY})
    fake.preload("done", len(HAPPY) - 1)
    fake.preload("busy", 3)
    fake.manifests["done"] = {"metrics": [{"metric_id": "bias", "status": "ran"}]}
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "done", "busy", "new"), clock=clock)
    state = _run(queue, clock)
    assert state.succeeded
    assert fake.posts == ["new"]
    assert state.entry("done").metrics == {"bias": "ran"}
    kinds = [event.kind for event in queue.store.events()]
    assert kinds.count("adopted") == 2


def test_resume_continues_without_posting_a_finished_run_again(tmp_path: Path) -> None:
    fast = [status("attacking", 5), status("complete", 10)]
    slow = [status("attacking", n) for n in range(1, 8)] + [status("complete", 10)]
    fake = FakeYield({"r1": fast, "r2": slow, "r3": fast})
    specs = _specs(tmp_path, "r1", "r2", "r3")
    clock = Clock()
    first = _queue(tmp_path, fake, specs, policy=Policy(concurrency=2), clock=clock)
    for _ in range(3):
        first.tick()
        clock.sleep(0)
    crashed = first.store.read()
    assert crashed is not None
    assert crashed.entry("r1").state is RunState.COMPLETE
    assert crashed.entry("r2").state is RunState.ACTIVE

    policy = Policy(concurrency=2)
    resumed = _queue(tmp_path, fake, specs, policy=policy, clock=clock, state=crashed)
    state = _run(resumed, clock)
    assert state.succeeded
    assert sorted(fake.posts) == ["r1", "r2", "r3"]


def test_a_crash_between_the_post_and_the_save_reposts_identically(tmp_path: Path) -> None:
    """An attacking run is posted again: Yield answers 202 while its driver lives, and the run
    goes on as one run."""
    fake = FakeYield({"r1": HAPPY})
    fake.preload("r1", 1)
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    assert _run(queue, clock).succeeded
    assert fake.posts == ["r1"]
    assert fake.attempts["r1"] == 1


def test_an_accepted_run_whose_dispatch_failed_is_posted_again(tmp_path: Path) -> None:
    """A 503 after the spec was frozen leaves a run reading `accepted` with no driver; the next
    tick posts it again rather than waiting for the stall timer."""
    fake = FakeYield({"r1": HAPPY}, post_answers={"r1": [503]})
    clock = Clock()
    policy = Policy(stall_after=timedelta(hours=5))
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    queue.tick()
    assert "r1" in fake.driverless
    queue.tick()
    assert "r1" not in fake.driverless
    assert fake.posts == ["r1", "r1"]
    assert _run(queue, clock).succeeded


def test_a_relaunch_keeps_its_slot_until_yield_shows_the_new_attempt(tmp_path: Path) -> None:
    """Yield reads the old attempt's `failed` for a while after the re-POST; that is not a
    second failure, and the run keeps its slot."""
    dies_once = [status("attacking", 3), status("failed", 3, liveness="dead")]
    dies_once += [status("attacking", 6), status("complete", 10)]
    fake = FakeYield({"r1": dies_once, "r2": HAPPY}, relaunch_delay=4)
    clock = Clock()
    policy = Policy(concurrency=1, retries=1)
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1", "r2"), policy=policy, clock=clock)
    state = _run(queue, clock)
    assert state.succeeded
    assert state.entry("r1").retries == 1
    assert fake.posts == ["r1", "r1", "r2"]
    assert fake.in_flight_max == 1


def test_a_relaunch_that_never_begins_is_a_failure(tmp_path: Path) -> None:
    dies = [status("attacking", 3), status("failed", 3, liveness="dead")]
    fake = FakeYield({"r1": dies}, relaunch_delay=1000)
    clock = Clock(step=timedelta(minutes=1))
    policy = Policy(retries=1, relaunch_grace=timedelta(minutes=5))
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    state = _run(queue, clock)
    assert state.entry("r1").state is RunState.FAILED
    assert fake.posts == ["r1", "r1"]
    assert any(e.kind == "relaunch_missing" for e in queue.store.events())


def test_a_re_post_sends_the_bytes_yield_first_accepted(tmp_path: Path) -> None:
    script = [status("attacking", 3), status("failed", 3), status("complete", 10)]
    fake = FakeYield({"r1": script})
    specs = _specs(tmp_path, "r1")
    clock = Clock()
    queue = _queue(tmp_path, fake, specs, clock=clock)
    queue.tick()
    specs["r1"].write_text(json.dumps({"run_id": "r1", "metric_ids": ["re-rendered"]}))
    state = _run(queue, clock)
    assert state.succeeded
    assert fake.posts == ["r1", "r1"]
    assert any(e.kind == "spec_changed" for e in queue.store.events())


def test_a_409_on_a_re_post_follows_the_run_instead_of_closing_it(tmp_path: Path) -> None:
    stuck = [status("attacking", 4)] * 5 + [status("complete", 10)]
    fake = FakeYield({"r1": stuck})
    clock = Clock(step=timedelta(minutes=5))
    policy = Policy(stall_after=timedelta(minutes=15))
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    queue.tick()
    fake.frozen["r1"] = {"run_id": "r1", "metric_ids": ["another"]}
    state = _run(queue, clock)
    assert state.succeeded
    assert state.entry("r1").reposts_blocked
    assert fake.posts == ["r1", "r1"]
    assert any(e.kind == "conflict" for e in queue.store.events())


def test_a_spec_without_a_webhook_is_warned_about_once(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    _run(queue, clock)
    assert [e.kind for e in queue.store.events()].count("no_webhook") == 1


def test_a_failed_run_is_posted_again_then_given_up(tmp_path: Path) -> None:
    dies_twice = [status("attacking", 3), status("failed", 3, liveness="dead", detail="OOM")]
    dies_twice += [status("attacking", 5), status("failed", 5, liveness="dead", detail="OOM")]
    fake = FakeYield({"r1": dies_twice, "r2": HAPPY})
    clock = Clock()
    queue = _queue(
        tmp_path, fake, _specs(tmp_path, "r1", "r2"), policy=Policy(retries=1), clock=clock
    )
    state = _run(queue, clock)
    assert state.done
    assert not state.succeeded
    assert state.entry("r1").state is RunState.FAILED
    assert state.entry("r1").retries == 1
    assert state.entry("r1").error == "OOM"
    assert state.entry("r2").state is RunState.COMPLETE
    assert fake.posts.count("r1") == 2
    severities = {e.kind: e.severity for e in queue.store.events()}
    assert severities["retry"] == "warning"
    assert severities["failed"] == "error"


def test_a_failed_run_that_resumes_completes(tmp_path: Path) -> None:
    script = [status("attacking", 3), status("failed", 3), status("attacking", 6)]
    script.append(status("complete", 10))
    fake = FakeYield({"r1": script})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    assert _run(queue, clock).succeeded
    assert fake.posts == ["r1", "r1"]


def test_a_stalled_attack_is_posted_again_and_reported(tmp_path: Path) -> None:
    stuck = [status("attacking", 4)] * 5 + [status("complete", 10)]
    fake = FakeYield({"r1": stuck})
    clock = Clock(step=timedelta(minutes=5))
    policy = Policy(stall_after=timedelta(minutes=15), stall_resubmits=1)
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    state = _run(queue, clock)
    assert state.succeeded
    assert fake.posts == ["r1", "r1"]
    assert state.entry("r1").stall_resubmits == 1
    events = queue.store.events()
    assert any(e.kind == "stalled" and e.severity == "warning" for e in events)
    assert any("without progress" in e.message for e in events if e.kind == "submitted")


def test_a_measuring_run_is_never_posted_again_however_long(tmp_path: Path) -> None:
    measuring = [status("measuring", 10, note="bias")] * 10 + [status("complete", 10)]
    fake = FakeYield({"r1": measuring})
    clock = Clock(step=timedelta(minutes=10))
    queue = _queue(
        tmp_path,
        fake,
        _specs(tmp_path, "r1"),
        policy=Policy(stall_after=timedelta(minutes=15)),
        clock=clock,
    )
    assert _run(queue, clock).succeeded
    assert fake.posts == ["r1"]


@pytest.mark.parametrize(("code", "state"), [(400, RunState.REFUSED), (422, RunState.REFUSED)])
def test_a_refused_spec_closes_the_run_without_retrying(
    tmp_path: Path, code: int, state: RunState
) -> None:
    fake = FakeYield({"r1": HAPPY}, post_answers={"r1": [code]})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    result = _run(queue, clock)
    assert result.entry("r1").state is state
    assert f"scripted {code}" in (result.entry("r1").error or "")
    assert fake.posts == ["r1"]


def test_an_unavailable_yield_is_asked_again(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY}, post_answers={"r1": [503, 503, 202]})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    assert _run(queue, clock).succeeded
    assert fake.posts == ["r1", "r1", "r1"]


def test_unavailable_too_often_fails_the_run(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY}, post_answers={"r1": [503] * 10})
    clock = Clock()
    policy = Policy(submit_attempts=3)
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    assert _run(queue, clock).entry("r1").state is RunState.FAILED
    assert fake.posts == ["r1"] * 3


def test_failed_traces_and_failed_metrics_reach_the_event_log(tmp_path: Path) -> None:
    script = [status("attacking", 2), status("attacking", 5, failed=2), status("complete", 10, 2)]
    fake = FakeYield({"r1": script})
    fake.manifests["r1"] = {
        "metrics": [
            {"metric_id": "bias", "status": "ran"},
            {"metric_id": "context", "status": "failed"},
        ]
    }
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    state = _run(queue, clock)
    assert state.entry("r1").metrics == {"bias": "ran", "context": "failed"}
    events = queue.store.events()
    assert any(e.kind == "anomaly.failed_traces" for e in events)
    complete = next(e for e in events if e.kind == "complete")
    assert complete.severity == "warning"


def test_the_event_log_is_only_appended_to(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    queue.tick()
    first = queue.store.events_path.read_text()
    _run(queue, clock)
    assert queue.store.events_path.read_text().startswith(first)


def test_a_second_queue_for_the_same_client_is_refused(tmp_path: Path) -> None:
    from proof_headless.runs.state import QueueBusy

    store = Store(tmp_path / "state")
    with store.lock(), pytest.raises(QueueBusy), Store(tmp_path / "state").lock():
        pass


def test_an_outage_is_logged_when_it_starts_and_when_it_ends(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY})
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    queue.tick()
    fake.outage = 5
    assert _run(queue, clock).succeeded
    kinds = [event.kind for event in queue.store.events()]
    assert kinds.count("unreachable") == 1
    assert kinds.count("reachable") == 1
    assert fake.posts == ["r1"]


def test_a_run_that_never_advances_closes_as_stalled_and_the_queue_ends(tmp_path: Path) -> None:
    fake = FakeYield({"r1": [status("attacking", 4)]})
    clock = Clock(step=timedelta(minutes=5))
    policy = Policy(stall_after=timedelta(minutes=15), stall_resubmits=2)
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    state = _run(queue, clock)
    assert state.done
    assert state.entry("r1").state is RunState.STALLED
    assert fake.posts == ["r1"] * 3


def test_a_resumed_queue_does_not_report_old_log_lines_again(tmp_path: Path) -> None:
    from proof_headless.runs.observe import Container, LogLine, Observation

    fake = FakeYield({"r1": [status("attacking", n) for n in range(30)]})
    clock = Clock()
    lines: list[LogLine] = []
    driver = Container("yield-driver-r1", "running")

    def observe(ids: list[str], statuses: dict[str, object]) -> Observation:
        return Observation(
            at=clock(), containers={driver.name: driver}, logs={driver.name: list(lines)}
        )

    specs = _specs(tmp_path, "r1")
    first = _queue(tmp_path, fake, specs, clock=clock)
    first.observe = observe  # type: ignore[assignment]
    first.tick()
    clock.sleep(0)
    lines.append(LogLine(clock(), "RateLimitError"))
    first.tick()
    crashed = first.store.read()
    assert crashed is not None

    resumed = _queue(tmp_path, fake, specs, clock=clock, state=crashed)
    resumed.observe = observe  # type: ignore[assignment]
    clock.sleep(0)
    resumed.tick()
    clock.sleep(0)
    lines.append(LogLine(clock(), "RateLimitError"))
    resumed.tick()
    reported = [e for e in resumed.store.events() if e.kind == "anomaly.rate_limited"]
    assert len(reported) == 2


def test_a_409_on_a_first_post_refuses_the_run(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY})
    fake.preload("r1", 1)
    fake.frozen["r1"] = {"run_id": "r1", "metric_ids": ["another"]}
    clock = Clock()
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), clock=clock)
    state = _run(queue, clock)
    entry = state.entry("r1")
    assert entry.state is RunState.REFUSED
    assert "render with a new run id" in (entry.error or "")
    assert fake.posts == ["r1"]


def test_a_first_post_matching_what_yield_froze_is_accepted(tmp_path: Path) -> None:
    fake = FakeYield({"r1": HAPPY})
    fake.preload("r1", 1)
    specs = _specs(tmp_path, "r1")
    fake.frozen["r1"] = json.loads(specs["r1"].read_text())
    clock = Clock()
    queue = _queue(tmp_path, fake, specs, clock=clock)
    assert _run(queue, clock).succeeded
    assert fake.posts == ["r1"]


class StubLens:
    """Lens stages per run, one per read, the last repeated; `ensure` answers `launch_ok`."""

    def __init__(self, stages: dict[str, list[str | None]], launch_ok: bool = True) -> None:
        self.stages = stages
        self.launch_ok = launch_ok
        self.ensured: list[str] = []

    def stage(self, run_id: str) -> str | None:
        seen = self.stages[run_id]
        return seen.pop(0) if len(seen) > 1 else seen[0]

    def ensure(self, run_id: str) -> tuple[bool, str]:
        self.ensured.append(run_id)
        return self.launch_ok, "relaunched" if self.launch_ok else "refused (not received)"


def _followed(tmp_path: Path, lens: StubLens, clock: Clock) -> Queue:
    fake = FakeYield({"r1": [status("attacking", 4), status("complete", 10)]})
    policy = Policy(lens_wait=timedelta(minutes=10))
    queue = _queue(tmp_path, fake, _specs(tmp_path, "r1"), policy=policy, clock=clock)
    queue.lens = lens
    return queue


def test_a_complete_run_waits_for_its_lens_result(tmp_path: Path) -> None:
    lens = StubLens({"r1": ["accepted", "running", "done"]})
    clock = Clock(step=timedelta(minutes=1))
    queue = _followed(tmp_path, lens, clock)
    state = _run(queue, clock)
    assert state.entry("r1").lens == "done"
    assert lens.ensured == []


def test_a_lens_result_late_past_the_bound_is_launched_once(tmp_path: Path) -> None:
    lens = StubLens({"r1": ["accepted"]})
    clock = Clock(step=timedelta(minutes=3))
    queue = _followed(tmp_path, lens, clock)
    state = _run(queue, clock)
    assert state.done
    assert state.entry("r1").lens == "launched"
    assert lens.ensured == ["r1"]
    assert any(e.kind == "lens_launched" for e in queue.store.events())


def test_a_lens_launch_that_cannot_happen_is_an_error(tmp_path: Path) -> None:
    lens = StubLens({"r1": [None]}, launch_ok=False)
    clock = Clock(step=timedelta(minutes=3))
    queue = _followed(tmp_path, lens, clock)
    state = _run(queue, clock)
    assert state.entry("r1").lens == "failed"
    failed = next(e for e in queue.store.events() if e.kind == "lens_failed")
    assert failed.severity == "error"
