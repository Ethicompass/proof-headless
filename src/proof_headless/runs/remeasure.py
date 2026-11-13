"""Re-measuring a run whose attack is over: compute's `POST /metrics` over the stored dataset.

What Yield honours decides what this allows, and it allows less than "re-measure anything":

- A **complete** run cannot be re-measured. The measure keeps an existing manifest (a run closes
  once), and storing a metric's output refuses a different result under the same key, so a new
  measurement of a complete run cannot change what it reports. A different measurement is a new
  run id.
- A **failed** run with no manifest can: a run that died before its measure closed it (a measure
  killed by a rate limit, a driver read dead after the handoff) holds its dataset, and a measure
  writes the manifest that completes it. This is the case the last engagement needed.
- Compute keeps one record per `measure-<run>` job and ignores a second request while the first is
  held (six hours while it runs, an hour after it settles), then launches a job that reads the
  *first* request. So a request is refused while such a record exists, and a job state is believed
  only once it is newer than the POST: an older one is the previous job's, and reporting it would
  report someone else's measurement.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from proof_headless.runs.client import JOB_TERMINAL, JobState, Phase, RunStatus, Yield

FIELDS = ("judge", "embedder", "metric_mapping", "metric_mapping_version", "kb_ref", "webhook_url")
"""The spec's fields `MetricsRequest` takes as they are."""
STALE_AFTER = timedelta(seconds=300)
"""Yield's `YIELD_STALE_AFTER_SECONDS` default. Compute has no reaper: a job whose container died
keeps reading `running`, and the age of its last beat is the only sign."""


class Refused(ValueError):
    """The run cannot be re-measured as asked; the message says why."""


def job_id(run_id: str) -> str:
    return f"measure-{run_id}"


def job_moment(job: JobState) -> datetime | None:
    """A job's `at`, which compute stamps as `20261112T030000.123456Z`."""
    if not job.at:
        return None
    try:
        return datetime.strptime(job.at, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def check(status: RunStatus | None, manifest: dict[str, Any] | None, held: JobState | None) -> None:
    """Refuse a re-measurement Yield would not perform as asked.

    Raises:
        Refused: The run is unknown, not failed, already has a manifest, or compute still holds a
            measure record that would answer in place of this request.
    """
    if status is None:
        raise Refused("Yield has no such run")
    run = status.run_id
    if status.phase is Phase.COMPLETE:
        raise Refused(
            f"{run} is complete. Yield closes a run once: its manifest stands and a different "
            f"result under the same key is refused, so measuring it again cannot change what it "
            f"reports. Measure differently under a new run id."
        )
    if status.phase is not Phase.FAILED:
        raise Refused(f"{run} is {status.phase}; only a failed run can be re-measured")
    if manifest is not None:
        raise Refused(
            f"{run} closed with a manifest, which a measure keeps; measuring it again cannot "
            f"change it"
        )
    if held is not None:
        when = f" (last heard {held.at})" if held.at else ""
        raise Refused(
            f"compute still holds the record of {held.job_id}, {held.state}{when}. A request "
            f"now would be ignored and the old request measured again. The record is released an "
            f"hour after the job settles, or six hours after a dead job last beat; try then."
        )


def body(
    spec: dict[str, Any], metrics: Sequence[str] = (), *, webhook_url: str | None = None
) -> dict[str, Any]:
    """The `MetricsRequest` for `spec`, narrowed to `metrics` when any are named.

    Raises:
        Refused: A named metric is not one the spec measures, or the spec names none.
    """
    known = list(spec.get("metric_ids") or [])
    if not known:
        raise Refused(f"spec {spec.get('run_id')} names no metric_ids to measure")
    unknown = [m for m in metrics if m not in known]
    if unknown:
        raise Refused(f"{unknown} are not metrics of {spec.get('run_id')}; it measures {known}")
    chosen = list(metrics) or known
    configured = spec.get("metrics") or {}
    request: dict[str, Any] = {
        "run_id": spec["run_id"],
        "metric_ids": chosen,
        "metrics": {m: configured[m] for m in chosen if m in configured},
        "kb_subjects": list(spec.get("kb_subjects") or []),
    }
    for name in FIELDS:
        if spec.get(name) is not None:
            request[name] = spec[name]
    if webhook_url is not None:
        request["webhook_url"] = webhook_url
    return request


class Ending(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    """`running`, but silent for longer than Yield's stale bound."""
    OLD_RECORD = "old_record"
    """The job compute answers with is older than this request: the request was dropped."""
    MISSING = "missing"
    """Compute never showed a job for the request."""
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class Waited:
    ending: Ending
    job: JobState | None


def _now() -> datetime:
    return datetime.now(UTC)


def wait(
    client: Yield,
    job: str,
    *,
    posted_at: datetime,
    timeout: timedelta,
    poll_seconds: float = 15.0,
    stale_after: timedelta = STALE_AFTER,
    missing_polls: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _now,
    progress: Callable[[JobState], None] = lambda job: None,
) -> Waited:
    """Poll compute's job until it ends, dies, turns out to be an older job's, or time runs out."""
    deadline = now() + timeout
    unseen = 0
    while True:
        found = client.job(job)
        moment = None if found is None else job_moment(found)
        if found is None:
            unseen += 1
            if unseen >= missing_polls:
                return Waited(Ending.MISSING, None)
        else:
            progress(found)
            if moment is not None and moment < posted_at:
                return Waited(Ending.OLD_RECORD, found)
            if found.state in JOB_TERMINAL:
                ending = Ending.SUCCEEDED if found.state == "succeeded" else Ending.FAILED
                return Waited(ending, found)
            if found.state == "running" and moment is not None and now() - moment > stale_after:
                return Waited(Ending.DEAD, found)
        if now() >= deadline:
            return Waited(Ending.TIMED_OUT, found)
        sleep(poll_seconds)
