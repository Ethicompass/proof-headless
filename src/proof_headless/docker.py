"""`docker compose`, as the appliance drives it: through one seam a test can replace.

Every call goes through a `Runner`, a function from arguments to a completed process, so the
order of operations (one model at a time, wait for health, then the next) is tested without a
daemon. The real runner is `subprocess.run` with output captured, never a shell.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, capturing its output."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


class ComposeFailed(RuntimeError):
    """`docker compose` exited non-zero."""

    def __init__(self, args: Sequence[str], result: subprocess.CompletedProcess[str]) -> None:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        super().__init__(f"`{' '.join(args)}` exited {result.returncode}: {detail}")


@dataclass(frozen=True)
class ServiceState:
    """One service as `docker compose ps` reports it."""

    service: str
    state: str
    """`running`, `exited`, `restarting`, ..."""
    health: str
    """`healthy`, `starting`, `unhealthy`, or empty when the service has no health check."""


@dataclass(frozen=True)
class Compose:
    """One compose project on this host."""

    project: str
    file: Path
    env_file: Path | None = None
    runner: Runner = run

    def _base(self) -> list[str]:
        args = ["docker", "compose", "-p", self.project, "-f", str(self.file)]
        if self.env_file is not None and self.env_file.exists():
            args += ["--env-file", str(self.env_file)]
        return args

    def _call(self, *args: str) -> str:
        full = [*self._base(), *args]
        result = self.runner(full)
        if result.returncode != 0:
            raise ComposeFailed(full, result)
        return result.stdout

    def up(self, service: str) -> None:
        """Create and start one service, detached, leaving the others as they are."""
        self._call("up", "--detach", "--no-deps", service)

    def down(self) -> None:
        """Stop and remove every service of the project, including services a newer manifest no
        longer declares, keeping caches and volumes."""
        self._call("down", "--remove-orphans")

    def ps(self) -> dict[str, ServiceState]:
        """Every service that has a container, by name."""
        output = self._call("ps", "--all", "--format", "json")
        states: dict[str, ServiceState] = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            rows = json.loads(line)
            for row in rows if isinstance(rows, list) else [rows]:
                states[row["Service"]] = ServiceState(
                    service=row["Service"],
                    state=str(row.get("State", "")),
                    health=str(row.get("Health", "")),
                )
        return states

    def logs(self, service: str, *, tail: int = 200) -> str:
        """The last `tail` lines of one service's logs."""
        return self._call("logs", "--no-color", "--tail", str(tail), service)
