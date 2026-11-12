"""A lens-api and a docker daemon that keep their state in memory, for testing `proof lens`."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from proof_headless.lens.launcher import container_name

SECRET_VALUE = "s3cr3t-value-never-shown"


@dataclass
class FakeLens:
    """Runs by `project/run`, each a status document. A launch (relaunch route or a container the
    fake docker started) moves a run to `running`; `finish` after `polls_to_done` reads makes it
    `done` with a result, or `failed` when the run is in `fails`."""

    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    relaunch_answer: str = "launched"
    relaunch_status: int | None = None
    relaunch_detail: str | None = None
    """An HTTP status the relaunch route answers with instead of launching."""
    polls_to_done: int = 1
    fails: frozenset[str] = frozenset()
    reads: dict[str, int] = field(default_factory=dict)
    relaunches: list[str] = field(default_factory=list)
    artifacts: dict[str, bytes] = field(default_factory=dict)

    def put(self, key: str, stage: str, **extra: Any) -> None:
        project, run = key.split("/")
        self.runs[key] = {
            "project_id": project,
            "run_id": run,
            "yield_run_id": run,
            "stage": stage,
            "has_delivery": stage not in {"awaiting_delivery", "registered"},
            "attempts": 0,
            "result_ref": None,
            "health": None,
            **extra,
        }

    def launched(self, key: str) -> None:
        run = self.runs[key]
        run["stage"] = "running"
        run["attempts"] += 1
        run["health"] = {"liveness": "running", "job_state": "running"}
        self.reads[key] = 0

    def _advance(self, key: str) -> None:
        run = self.runs[key]
        if run["stage"] != "running" or key not in self.reads:
            return
        self.reads[key] += 1
        if self.reads[key] < self.polls_to_done:
            return
        if key in self.fails:
            run["stage"] = "failed"
            run["health"] = {"liveness": "dead", "job_state": "exited", "exit_code": 3}
        else:
            run["stage"] = "done"
            run["result_ref"] = "sha256:" + "e" * 64
            run["health"] = None
            self.artifacts[run["result_ref"]] = json.dumps({"run_id": run["run_id"]}).encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET" and parts[0] == "artifacts":
            body = self.artifacts.get(parts[1])
            return httpx.Response(200, content=body) if body else httpx.Response(404)
        key = f"{parts[1]}/{parts[3]}"
        if key not in self.runs:
            return httpx.Response(404, json={"detail": f"no run {key}"})
        if request.method == "POST" and parts[-1] == "launch":
            self.relaunches.append(key)
            if self.relaunch_status is not None:
                detail = self.relaunch_detail or f"run {key} is not admitted"
                return httpx.Response(self.relaunch_status, json={"detail": detail})
            answer = {"project_id": parts[1], "run_id": parts[3], "launch": self.relaunch_answer}
            if self.relaunch_answer == "launch_failed":
                return httpx.Response(503, json={**answer, "reason": "daemon unreachable"})
            self.launched(key)
            return httpx.Response(202, json=answer)
        self._advance(key)
        return httpx.Response(200, json=self.runs[key])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://lens")


@dataclass
class FakeDocker:
    """`docker ps`, `inspect`, `rm` and `run` against one lens-api container and the run
    containers this fake started. `run` reads the env file while it exists, as the daemon would."""

    lens: FakeLens
    api_env: list[str] = field(
        default_factory=lambda: [
            "PATH=/usr/bin",
            "LENS_STORE_BACKEND=s3",
            "LENS_RUN_IMAGE=registry.local/lens-run@sha256:" + "a" * 64,
            "LENS_DOCKER_NETWORK=lens",
            "LENS_PUBLIC_BASE_URL=http://127.0.0.1:8090",
            "LENS_MODEL_READER_CREDENTIAL_REF=READER_KEY",
            f"READER_KEY={SECRET_VALUE}",
            "LENS_MODEL_READER_BASE_URL=http://host.docker.internal:8000/v1",
        ]
    )
    containers: dict[str, str] = field(default_factory=dict)
    created: str = "2026-11-12T10:00:00.000000000Z"
    """Run containers by name, to their status."""
    calls: list[list[str]] = field(default_factory=list)
    env_files: list[Path] = field(default_factory=list)
    env_seen: list[str] = field(default_factory=list)
    env_modes: list[int] = field(default_factory=list)
    run_fails: bool = False

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)

        def done(
            stdout: str = "", code: int = 0, stderr: str = ""
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)

        if args[1] == "ps":
            return done("lens-lens-api-1\n")
        if args[1] == "inspect" and args[2] == "--format":
            name = args[-1]
            if name not in self.containers:
                return done(code=1, stderr=f"Error: No such object: {name}")
            status = self.containers[name]
            state = {"Status": status, "ExitCode": 137, "OOMKilled": False}
            if status != "created":
                state["FinishedAt"] = "2026-11-12T10:00:00.123456789Z"
            return done(f"{json.dumps(state)}\t{self.created}\n")
        if args[1] == "logs":
            return done(
                "loading\n",
                stderr=f'File "/app/lens_run/main.py": token {SECRET_VALUE} rejected\n',
            )
        if args[1] == "inspect":
            document = {
                "Config": {"Env": self.api_env},
                "NetworkSettings": {"Networks": {"lens": {}}},
            }
            return done(json.dumps([document]))
        if args[1] == "rm":
            self.containers.pop(args[-1], None)
            return done(args[-1])
        if args[1] == "run":
            path = Path(args[args.index("--env-file") + 1])
            self.env_files.append(path)
            self.env_modes.append(path.stat().st_mode & 0o777)
            self.env_seen = path.read_text().splitlines()
            if self.run_fails:
                return done(code=125, stderr=f"bad value {SECRET_VALUE} in env")
            name = args[args.index("--name") + 1]
            self.containers[name] = "running"
            project, run = args[-2], args[-1]
            assert name == container_name(project, run)
            self.lens.launched(f"{project}/{run}")
            return done("c0ffee\n")
        raise AssertionError(f"unexpected docker call {args}")
