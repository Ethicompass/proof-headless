"""A Yield API and compute that play scripted runs, behind `httpx.MockTransport`.

Each run has a script: the statuses `GET /runs/{id}` answers, one per poll, the last repeated.
A run is unknown (404) until it is posted. `POST /runs` answers 202 unless `post_answers` scripts
other codes for that run, and keeps Yield's retry contract: a different body under a frozen id is
409. It records how many runs were in flight at once, which is what a concurrency limit bounds.
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


@dataclass
class FakeYield:
    scripts: dict[str, list[dict[str, Any]]]
    post_answers: dict[str, list[int]] = field(default_factory=dict)
    known: dict[str, int] = field(default_factory=dict)
    """Runs Yield has, by the index of the status they are at."""
    frozen: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: list[str] = field(default_factory=list)
    metrics_posts: list[dict[str, Any]] = field(default_factory=list)
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, list[str]] = field(default_factory=dict)
    in_flight_max: int = 0
    outage: int = 0
    """How many status reads answer 503 before Yield answers again."""

    def preload(self, run_id: str, index: int = 0) -> None:
        """A run Yield already has, as after a queue that crashed after posting it."""
        self.known[run_id] = index

    def _current(self, run_id: str) -> dict[str, Any]:
        script = self.scripts[run_id]
        return script[min(self.known[run_id], len(script) - 1)]

    def _in_flight(self) -> int:
        return sum(1 for run in self.known if self._current(run)["phase"] not in TERMINAL)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            spec = json.loads(request.content)
            run_id = spec["run_id"]
            self.posts.append(run_id)
            answers = self.post_answers.get(run_id)
            if answers:
                code = answers.pop(0)
                if code != 202:
                    return httpx.Response(code, json={"detail": f"scripted {code}"})
            if run_id in self.frozen and self.frozen[run_id] != spec:
                return httpx.Response(409, json={"detail": "frozen with a different spec"})
            self.frozen[run_id] = spec
            if run_id not in self.known:
                self.known[run_id] = 0
            elif self._current(run_id)["phase"] == "failed":
                self.known[run_id] += 1
            self.in_flight_max = max(self.in_flight_max, self._in_flight())
            return httpx.Response(202, json={"run_id": run_id, "result_location": "x"})
        if request.method == "GET" and path.startswith("/runs/") and path.endswith("/result"):
            run_id = path.split("/")[2]
            if run_id not in self.manifests:
                return httpx.Response(404, json={"detail": "no result"})
            return httpx.Response(200, json=self.manifests[run_id])
        if request.method == "GET" and path.startswith("/runs/") and self.outage:
            self.outage -= 1
            return httpx.Response(503, json={"detail": "down"})
        if request.method == "GET" and path.startswith("/runs/"):
            run_id = path.split("/")[2]
            if run_id not in self.known:
                return httpx.Response(404, json={"detail": f"no run {run_id}"})
            answer = {"run_id": run_id, **self._current(run_id)}
            script = self.scripts[run_id]
            if self.known[run_id] < len(script) - 1 and answer["phase"] != "failed":
                self.known[run_id] += 1
            self.in_flight_max = max(self.in_flight_max, self._in_flight())
            return httpx.Response(200, json=answer)
        if request.method == "POST" and path == "/metrics":
            body = json.loads(request.content)
            self.metrics_posts.append(body)
            job = f"measure-{body['run_id']}"
            return httpx.Response(202, json={"job_id": job, "state": "pending"})
        if request.method == "GET" and path.startswith("/jobs/"):
            job = path.split("/")[2]
            states = self.jobs.get(job)
            if not states:
                return httpx.Response(404, json={"detail": "no job"})
            state = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json={"job_id": job, "state": state, "detail": "d"})
        return httpx.Response(500, json={"detail": f"unexpected {request.method} {path}"})

    def client(self) -> Yield:
        transport = httpx.MockTransport(self.handle)
        return Yield(
            httpx.Client(base_url="http://api", transport=transport),
            httpx.Client(base_url="http://compute", transport=transport),
        )
