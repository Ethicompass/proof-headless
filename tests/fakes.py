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
    """Services that crash at start under `restart: unless-stopped`: `restarting`, never exited."""
    state: dict[str, dict[str, str]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        command = next(a for a in args[2:] if a in {"up", "ps", "logs", "down"})
        stdout = ""
        if command == "up":
            service = args[-1]
            self.state[service] = {"Service": service, "State": "running", "Health": "starting"}
            self.polls[service] = 0
        elif command == "ps":
            for service, row in self.state.items():
                if service in self.crash_loops:
                    row["State"] = "restarting"
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

    def started(self) -> list[str]:
        return [call[-1] for call in self.calls if "up" in call]
