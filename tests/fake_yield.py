"""A Yield API and compute that play scripted runs, behind `httpx.MockTransport`.

Each run has a script: the statuses `GET /runs/{id}` answers, one per poll, the last repeated.
A run is unknown (404) until it is posted. It keeps the parts of Yield's behaviour the queue has to
get right:

- the retry contract: an identical re-POST answers 202 (and changes nothing while a driver runs),
  a different body under a frozen id answers 409;
- a re-POST of a failed run relaunches it, but Yield keeps reading `failed` under the old attempt
  for `relaunch_delay` polls, until the new driver announces its attempt;
- a scripted 503 is a dispatch that failed after the spec was frozen: the run then reads
  `accepted` with no driver, and goes nowhere until it is posted again;
- compute keeps a `measure-<run>` record and ignores a second request while it holds one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from proof_headless.runs.client import Yield

TERMINAL = {"complete", "failed"}


def status(
    phase: str,
    closed: int = 0,
    failed: int = 0,
    planned: int | None = 10,
    liveness: str = "running",
    note: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "phase": phase,
        "planned": planned,
        "closed": closed,
        "failed": failed,
        "health": {"liveness": liveness, "role": "driver", "note": note, "detail": detail},
    }


HAPPY = [
    status("accepted"),
    status("attacking", 4),
    status("attacking", 10),
    status("measuring", 10, note="measuring bias"),
    status("complete", 10, liveness="unknown"),
]


def job(state: str, at: str) -> dict[str, Any]:
    return {"state": state, "at": at, "detail": f"{state} at {at}"}


@dataclass
class FakeYield:
    scripts: dict[str, list[dict[str, Any]]]
    post_answers: dict[str, list[int]] = field(default_factory=dict)
    relaunch_delay: int = 0
    """Polls a relaunched run keeps reading as the old attempt's `failed`."""
    known: dict[str, int] = field(default_factory=dict)
    """Runs Yield has, by the index of the status they are at."""
    attempts: dict[str, int] = field(default_factory=dict)
    relaunching: dict[str, int] = field(default_factory=dict)
    driverless: set[str] = field(default_factory=set)
    frozen: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: list[str] = field(default_factory=list)
    metrics_posts: list[dict[str, Any]] = field(default_factory=list)
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Compute's job records: each poll pops one, the last repeated."""
    jobs_after_post: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Records a `POST /metrics` installs when compute holds none."""
    manifests_after_post: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Manifests a measure writes, installed by a `POST /metrics` that was honoured."""
    in_flight_max: int = 0
    outage: int = 0
    """How many status reads answer 503 before Yield answers again."""

    def preload(self, run_id: str, index: int = 0) -> None:
        """A run Yield already has, as after a queue that crashed after posting it."""
        self.known[run_id] = index
        self.attempts[run_id] = 1

    def _current(self, run_id: str) -> dict[str, Any]:
        if run_id in self.driverless:
            return status("accepted", liveness="unknown")
        script = self.scripts[run_id]
        return script[min(self.known[run_id], len(script) - 1)]

    def _in_flight(self) -> int:
        """Runs whose driver is alive: in flight, or failed with a relaunch on its way."""
        return sum(
            1
            for run in self.known
            if run not in self.driverless
            and (self._current(run)["phase"] not in TERMINAL or run in self.relaunching)
        )

    def _post(self, request: httpx.Request) -> httpx.Response:
        spec = json.loads(request.content)
        run_id = spec["run_id"]
        self.posts.append(run_id)
        if run_id in self.frozen and self.frozen[run_id] != spec:
            return httpx.Response(409, json={"detail": "frozen with a different spec"})
        answers = self.post_answers.get(run_id)
        code = answers.pop(0) if answers else 202
        if code in (400, 422):
            return httpx.Response(code, json={"detail": f"scripted {code}"})
        self.frozen[run_id] = spec
        if run_id not in self.known:
            self.known[run_id] = 0
            self.attempts[run_id] = 1
        if code != 202:
            self.driverless.add(run_id)
            return httpx.Response(code, json={"detail": f"scripted {code}"})
        if run_id in self.driverless:
            self.driverless.discard(run_id)
        elif self._current(run_id)["phase"] == "failed" and run_id not in self.relaunching:
            self.relaunching[run_id] = self.relaunch_delay
        self.in_flight_max = max(self.in_flight_max, self._in_flight())
        return httpx.Response(202, json={"run_id": run_id, "result_location": "x"})

    def _get(self, run_id: str) -> httpx.Response:
        if run_id not in self.known:
            return httpx.Response(404, json={"detail": f"no run {run_id}"})
        if run_id in self.relaunching:
            if self.relaunching[run_id] > 0:
                self.relaunching[run_id] -= 1
            else:
                del self.relaunching[run_id]
                self.known[run_id] += 1
                self.attempts[run_id] += 1
        answer = {"run_id": run_id, **self._current(run_id), "attempt": f"a{self.attempts[run_id]}"}
        script = self.scripts[run_id]
        stays = answer["phase"] == "failed" or run_id in self.driverless
        if self.known[run_id] < len(script) - 1 and not stays:
            self.known[run_id] += 1
        self.in_flight_max = max(self.in_flight_max, self._in_flight())
        return httpx.Response(200, json=answer)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return self._post(request)
        if request.method == "GET" and path.startswith("/runs/") and path.endswith("/result"):
            run_id = path.split("/")[2]
            if run_id not in self.manifests:
                return httpx.Response(404, json={"detail": "no result"})
            return httpx.Response(200, json=self.manifests[run_id])
        if request.method == "GET" and path.startswith("/runs/") and self.outage:
            self.outage -= 1
            return httpx.Response(503, json={"detail": "down"})
        if request.method == "GET" and path.startswith("/runs/"):
            return self._get(path.split("/")[2])
        if request.method == "POST" and path == "/metrics":
            body = json.loads(request.content)
            self.metrics_posts.append(body)
            name = f"measure-{body['run_id']}"
            if name not in self.jobs and name in self.jobs_after_post:
                self.jobs[name] = self.jobs_after_post.pop(name)
                if body["run_id"] in self.manifests_after_post:
                    self.manifests[body["run_id"]] = self.manifests_after_post.pop(body["run_id"])
            return httpx.Response(202, json={"job_id": name, "state": "pending"})
        if request.method == "GET" and path.startswith("/jobs/"):
            name = path.split("/")[2]
            states = self.jobs.get(name)
            if not states:
                return httpx.Response(404, json={"detail": "no job"})
            record = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json={"job_id": name, **record})
        return httpx.Response(500, json={"detail": f"unexpected {request.method} {path}"})

    def client(self) -> Yield:
        transport = httpx.MockTransport(self.handle)
        return Yield(
            httpx.Client(base_url="http://api", transport=transport),
            httpx.Client(base_url="http://compute", transport=transport),
        )
