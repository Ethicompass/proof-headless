"""A `docker compose` that keeps its state in memory, for testing what `proof` asks it to do."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class FakeCompose:
    """Answers `up`, `ps`, `logs` and `down` the way the real CLI does, from in-memory state.

    A started service reports `starting` for `polls_to_healthy` calls of `ps`, then `healthy`,
    unless it is listed in `fails`, in which case it turns `unhealthy`.
    """

    polls_to_healthy: int = 2
    fails: frozenset[str] = frozenset()
    crash_loops: frozenset[str] = frozenset()
    """Services that crash while loading under `restart: unless-stopped`: docker brings them back
    to `running` before most polls, so only their restart count shows it."""
    restarts: dict[str, int] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)
    recreate_on_up: bool = False
    """Whether `up` replaces the container, as compose does when its configuration changed."""
    services: tuple[str, ...] = ()
    """What `up` with no service starts: every service of the project, as `up_all` asks."""
    one_shots: frozenset[str] = frozenset()
    """Services that run once: after `up` they report `exited` with code 0, or 1 when in `fails`."""
    images: dict[str, str] = field(default_factory=dict)
    ports: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    missing_networks: set[str] = field(default_factory=set)
    """Networks `docker network inspect` does not find, until `docker network create` makes one."""
    volumes: set[str] = field(default_factory=set)
    """Volumes `docker volume inspect` finds."""
    state: dict[str, dict[str, object]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            service = args[-1].split("-", 1)[1].rsplit("-", 1)[0]
            count = str(self.restarts.get(service, 0))
            return subprocess.CompletedProcess(args, 0, stdout=count, stderr="")
        if args[:3] == ["docker", "network", "inspect"] and args[3] in self.missing_networks:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
        if args[:3] == ["docker", "volume", "inspect"]:
            code = 0 if args[3] in self.volumes else 1
            return subprocess.CompletedProcess(args, code, stdout="", stderr="")
        if args[:3] == ["docker", "network", "create"]:
            self.missing_networks.discard(args[3])
        if args[1] in {"network", "pull"}:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        command = next(a for a in args[2:] if a in {"up", "ps", "logs", "down", "pull"})
        stdout = ""
        if command == "up":
            started = [args[-1]] if not args[-1].startswith("-") else list(self.services)
            for service in started:
                self._start(service)
        elif command == "ps" and "--quiet" in args:
            service = args[-1]
            generation = self.generations.get(service, 0)
            stdout = f"id-{service}-{generation}\n" if service in self.state else ""
        elif command == "ps":
            for service, row in self.state.items():
                if service in self.one_shots:
                    continue
                if service in self.crash_loops:
                    self.restarts[service] = self.restarts.get(service, 0) + 1
                    continue
                if row["Health"] == "starting":
                    self.polls[service] += 1
                    if self.polls[service] >= self.polls_to_healthy:
                        row["Health"] = "unhealthy" if service in self.fails else "healthy"
            stdout = "\n".join(json.dumps(row) for row in self.state.values())
        elif command == "logs":
            stdout = "CUDA out of memory\n"
        elif command == "down":
            self.state.clear()
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def _start(self, service: str) -> None:
        row: dict[str, object] = {"Service": service, "State": "running", "Health": "starting"}
        if service in self.one_shots:
            code = 1 if service in self.fails else 0
            row = {"Service": service, "State": "exited", "Health": "", "ExitCode": code}
        if service in self.images:
            row["Image"] = self.images[service]
        if service in self.ports:
            row["Publishers"] = self.ports[service]
        self.state[service] = row
        if self.recreate_on_up:
            self.generations[service] = self.generations.get(service, 0) + 1
            self.restarts[service] = 0
        self.polls[service] = 0

    def started(self) -> list[str]:
        return [call[-1] for call in self.calls if "up" in call]
