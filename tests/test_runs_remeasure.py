"""Waiting on compute's measure job: success, an older job's record, death, silence, time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from proof_headless.runs.remeasure import Ending, wait
from tests.fake_yield import FakeYield, job

POSTED = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = POSTED

    def __call__(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def stamp(minutes_after: float) -> str:
    return (POSTED + timedelta(minutes=minutes_after)).strftime("%Y%m%dT%H%M%S.%fZ")


def _wait(fake: FakeYield, timeout: timedelta = timedelta(hours=1)) -> Ending:
    clock = Clock()
    return wait(
        fake.client(),
        "measure-r1",
        posted_at=POSTED,
        timeout=timeout,
        poll_seconds=60,
        sleep=clock.sleep,
        now=clock,
    ).ending


def test_a_job_newer_than_the_request_is_believed() -> None:
    fake = FakeYield({})
    fake.jobs["measure-r1"] = [job("running", stamp(1)), job("succeeded", stamp(2))]
    assert _wait(fake) is Ending.SUCCEEDED


def test_a_job_older_than_the_request_is_an_older_job_s() -> None:
    fake = FakeYield({})
    fake.jobs["measure-r1"] = [job("succeeded", stamp(-30))]
    assert _wait(fake) is Ending.OLD_RECORD


def test_a_running_job_that_stopped_beating_is_dead() -> None:
    fake = FakeYield({})
    fake.jobs["measure-r1"] = [job("running", stamp(0.5))]
    assert _wait(fake) is Ending.DEAD


def test_a_job_compute_never_shows_is_missing() -> None:
    assert _wait(FakeYield({})) is Ending.MISSING


def test_a_job_that_keeps_beating_past_the_deadline_times_out() -> None:
    fake = FakeYield({})
    fake.jobs["measure-r1"] = [job("running", stamp(n)) for n in range(1, 40)]
    assert _wait(fake, timeout=timedelta(minutes=10)) is Ending.TIMED_OUT
