"""The three Lens routes `proof` reads and calls, over one `httpx.Client` a test can replace.

`GET /projects/{p}/runs/{r}` is the run's status, derived by Lens from its store and, while a
process may be behind the run, from the docker daemon (Lens ADR 0027). `POST .../launch` is Lens's
own remedy for an admitted run whose launch did not finish: it launches with exactly the
environment lens-api gives every job. `GET /artifacts/{digest}` is the result itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8090"
"""lens-api's loopback port on the appliance."""

DONE = "done"
FAILED = "failed"
RUNNING = "running"
ACCEPTED = "accepted"
NOTHING_TO_LAUNCH = frozenset(
    {"registered", "not_admitted", "awaiting_delivery", "upstream_failed"}
)
"""Stages in which no launch can produce a result: the delivery is missing, failed upstream, or
the run lost its Yield run to another."""


class LensUnavailable(RuntimeError):
    """Lens could not be reached, or answered something that is not its API."""


@dataclass(frozen=True)
class Status:
    """What `GET /projects/{p}/runs/{r}` says, reduced to what `proof lens` decides on."""

    stage: str
    has_delivery: bool
    attempts: int
    result_ref: str | None
    liveness: str | None
    """The control plane's verdict on the run's process: `running`, `dead`, `unknown`, or `None`
    when Lens did not ask (the run is done, or nothing was launched to ask about)."""
    exit_code: int | None
    reason: str

    @classmethod
    def of(cls, document: dict[str, Any]) -> Status:
        health = document.get("health") or {}
        exit_code = health.get("exit_code")
        return cls(
            stage=str(document.get("stage", "")),
            has_delivery=bool(document.get("has_delivery")),
            attempts=int(document.get("attempts") or 0),
            result_ref=document.get("result_ref") or None,
            liveness=health.get("liveness"),
            exit_code=int(exit_code) if exit_code is not None else None,
            reason=str(health.get("reason") or ""),
        )

    @property
    def admitted(self) -> bool:
        """Lens holds the delivery's run and nothing stands between it and a launch."""
        return self.stage not in {"registered", "not_admitted"}

    @property
    def alive(self) -> bool:
        """A process is behind the run right now, so launching another would only be refused."""
        return self.stage in {ACCEPTED, RUNNING} and self.liveness == RUNNING


class LensApi:
    """lens-api, reached through `client`, whose base URL is the API's."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _send(self, method: str, path: str) -> httpx.Response:
        try:
            return self.client.request(method, path)
        except httpx.HTTPError as error:
            raise LensUnavailable(
                f"Lens could not be reached at {self.client.base_url}: {error}"
            ) from None

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            answer = response.json()
        except json.JSONDecodeError:
            raise LensUnavailable(f"{what}: Lens answered no JSON: {response.text[:200]}") from None
        if not isinstance(answer, dict):
            raise LensUnavailable(f"{what}: Lens answered {type(answer).__name__}, not an object")
        return answer

    def status(self, project_id: str, run_id: str) -> Status | None:
        """The run's status, or `None` when Lens has no run under these ids: for a derived run,
        that its delivery never arrived."""
        what = f"run {project_id}/{run_id}"
        response = self._send("GET", f"/projects/{project_id}/runs/{run_id}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise LensUnavailable(
                f"{what}: Lens answered {response.status_code}: {response.text[:300]}"
            )
        return Status.of(self._json(response, what))

    def relaunch(self, project_id: str, run_id: str) -> tuple[str, str | None]:
        """Ask lens-api to launch the run again: `(launch, reason)`.

        `launch` is what lens-api answered (`launched`, `already_running`, `not_delivered`,
        `upstream_failed`, or `launch_failed` with a 503), `launch_failed` when the route is
        missing (405, a Lens older than ADR 0027), or `refused` for any other answer: a 404 (no
        run under those ids), a 409 (the run is not admitted) or anything unexpected is Lens
        declining the launch, and nobody launches past it.
        """
        what = f"relaunch of {project_id}/{run_id}"
        response = self._send("POST", f"/projects/{project_id}/runs/{run_id}/launch")
        if response.status_code in (202, 503):
            answer = self._json(response, what)
            return str(answer.get("launch", "launch_failed")), answer.get("reason")
        if response.status_code == 405:
            return "launch_failed", "this lens-api has no relaunch route"
        try:
            detail = response.json().get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        return "refused", f"Lens answered {response.status_code}: {detail or response.text[:300]}"

    def artifact(self, digest: str) -> bytes:
        """The artifact stored under `digest`, as Lens serves it."""
        response = self._send("GET", f"/artifacts/{digest}")
        if response.status_code >= 400:
            raise LensUnavailable(
                f"artifact {digest}: Lens answered {response.status_code}: {response.text[:200]}"
            )
        return response.content
