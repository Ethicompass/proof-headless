"""Re-measuring a run whose attack is over: compute's `POST /metrics` over the stored dataset.

Re-measuring costs no attack, and it is what the last engagement needed most after the queue: a run
whose measure died on a rate limit, a metric whose judge was swapped, a run that read as failed
holding a complete dataset. The request is built from the run's own rendered spec, so it measures
with the instruments the run was specified with, narrowed to the metrics asked for.

Only a run Yield reads as complete or failed is re-measured. Before that there is no dataset to
measure (the attack is still running) or a measure is already spending the judge, and compute
would answer that one with the running job anyway.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from proof_headless.runs.client import JOB_TERMINAL, JobState, Phase, Yield

REMEASURABLE = frozenset({Phase.COMPLETE, Phase.FAILED})
FIELDS = ("judge", "embedder", "metric_mapping", "metric_mapping_version", "kb_ref", "webhook_url")
"""The spec's fields `MetricsRequest` takes as they are."""


class Refused(ValueError):
    """The run cannot be re-measured as asked."""


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


def wait(
    client: Yield,
    job_id: str,
    *,
    poll_seconds: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[JobState], None] = lambda job: None,
    deadline_polls: int | None = None,
) -> JobState | None:
    """Poll compute's job until it succeeds or fails; `None` if `deadline_polls` ran out first."""
    polls = 0
    while deadline_polls is None or polls < deadline_polls:
        job = client.job(job_id)
        if job is not None:
            progress(job)
            if job.state in JOB_TERMINAL:
                return job
        polls += 1
        sleep(poll_seconds)
    return None
