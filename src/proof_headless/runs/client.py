"""Yield's API and compute, as a queue of runs speaks to them.

The shapes are read loosely (`extra="ignore"`) and declared here rather than imported from Yield
(ADR 0001): the appliance runs whatever Yield its lock pins, and a field Yield adds must not break a
poll. What the queue relies on is Yield's retry contract (`POST /runs`): the same request under the
same run id answers 202 every time and relaunches a driver that died, a different request under a
frozen id answers 409, and a driver already attacking is a 202 too. A re-POST of a *finished* run
would launch a driver again, so the queue asks `GET /runs/{id}` before every submission and never
posts a run Yield already closed or is still running.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict


class Phase(StrEnum):
    """A run's phase as Yield derives it from the artifacts in its store."""

    ACCEPTED = "accepted"
    GENERATING = "generating"
    ATTACKING = "attacking"
    MEASURING = "measuring"
    COMPLETE = "complete"
    FAILED = "failed"


IN_FLIGHT = frozenset({Phase.ACCEPTED, Phase.GENERATING, Phase.ATTACKING, Phase.MEASURING})
BEFORE_MEASURE = frozenset({Phase.ACCEPTED, Phase.GENERATING, Phase.ATTACKING})


class Health(BaseModel):
    """What the job registry and the control plane last said about the process behind a run."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    liveness: str = "unknown"
    """`running`, `stalled` (a suspicion), `dead` (a verdict that reads the run as failed) or
    `unknown`."""
    role: str = ""
    note: str = ""
    last_heard: str | None = None
    silence_seconds: float | None = None
    detail: str = ""


class RunStatus(BaseModel):
    """`GET /runs/{id}`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    run_id: str
    phase: Phase
    planned: int | None = None
    closed: int = 0
    failed: int = 0
    pending: int | None = None
    attempt: str | None = None
    health: Health | None = None


class JobState(BaseModel):
    """Compute's `GET /jobs/{id}`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    job_id: str
    state: str
    """`pending`, `running`, `succeeded`, `failed` or `unknown`."""
    detail: str = ""
    at: str = ""


JOB_TERMINAL = frozenset({"succeeded", "failed"})


class Answer(StrEnum):
    """What became of a submission."""

    ACCEPTED = "accepted"
    CONFLICT = "conflict"
    """409: the run id is frozen with a different spec. Only a new run id changes it."""
    REFUSED = "refused"
    """400 or 422: Yield's gate refused the spec."""
    UNAVAILABLE = "unavailable"
    """503 or no answer: worth asking again."""


@dataclass(frozen=True)
class Submitted:
    answer: Answer
    status_code: int | None
    detail: str


class Unreachable(RuntimeError):
    """Yield did not answer, or answered with a server error."""


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:500]
    return str(body)[:500]


class Yield:
    """One Yield deployment: its API and its compute."""

    def __init__(self, api: httpx.Client, compute: httpx.Client) -> None:
        self.api = api
        self.compute = compute

    def _get(self, client: httpx.Client, path: str) -> httpx.Response | None:
        try:
            response = client.get(path)
        except httpx.HTTPError as error:
            raise Unreachable(f"{client.base_url}{path}: {error}") from error
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise Unreachable(f"{path} answered {response.status_code}: {_detail(response)}")
        return response

    def submit(self, spec: dict[str, Any]) -> Submitted:
        """`POST /runs`."""
        try:
            response = self.api.post("/runs", json=spec)
        except httpx.HTTPError as error:
            return Submitted(Answer.UNAVAILABLE, None, str(error))
        code = response.status_code
        if code in (200, 202):
            return Submitted(Answer.ACCEPTED, code, "")
        if code == 409:
            return Submitted(Answer.CONFLICT, code, _detail(response))
        if code in (400, 404, 422):
            return Submitted(Answer.REFUSED, code, _detail(response))
        return Submitted(Answer.UNAVAILABLE, code, _detail(response))

    def status(self, run_id: str) -> RunStatus | None:
        """`GET /runs/{id}`, or `None` when Yield has no such run."""
        response = self._get(self.api, f"/runs/{run_id}")
        return None if response is None else RunStatus.model_validate(response.json())

    def result(self, run_id: str) -> dict[str, Any] | None:
        """`GET /runs/{id}/result`: the manifest, or `None` before there is one."""
        response = self._get(self.api, f"/runs/{run_id}/result")
        return None if response is None else dict(response.json())

    def measure(self, body: dict[str, Any]) -> str:
        """Compute's `POST /metrics`: launch a measurement and answer with its job id."""
        try:
            response = self.compute.post("/metrics", json=body)
        except httpx.HTTPError as error:
            raise Unreachable(f"{self.compute.base_url}/metrics: {error}") from error
        if response.status_code >= 400:
            raise Unreachable(f"/metrics answered {response.status_code}: {_detail(response)}")
        return str(response.json()["job_id"])

    def job(self, job_id: str) -> JobState | None:
        """Compute's `GET /jobs/{id}`, or `None` while compute knows no such job."""
        response = self._get(self.compute, f"/jobs/{job_id}")
        return None if response is None else JobState.model_validate(response.json())


def metric_outcomes(manifest: dict[str, Any]) -> dict[str, str]:
    """Each metric's status in a run's manifest: `ran`, `failed` or `skipped`."""
    outcomes = manifest.get("metrics") or []
    return {
        str(item["metric_id"]): str(item.get("status", ""))
        for item in outcomes
        if isinstance(item, dict) and "metric_id" in item
    }
