"""After a run completes, its Lens result: the queue's hand-off to `proof lens`.

Yield's measure delivers a completed run to Lens's webhook, and Lens launches its own job. When
that result has not appeared within the queue's bound (the delivery was lost, or Lens's launch did
not finish), the queue calls `proof_headless.lens.ensure` once, which launches through lens-api's
relaunch route, or by hand when that route cannot, and does not wait: whether that job ends is
`proof lens run`'s to follow, and the queue's job ends with the launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from proof_headless.lens import runs as yield_runs
from proof_headless.lens.api import LensApi
from proof_headless.lens.ensure import Manual, Waiting, ensure
from proof_headless.lens.runs import RunsUnknown


class FollowsLens(Protocol):
    """What the queue needs from Lens. `stage` raises `LensUnavailable` when Lens does not
    answer."""

    def stage(self, run_id: str) -> str | None:
        """The Lens run's stage, `None` when Lens never received the run's delivery."""
        ...

    def ensure(self, run_id: str) -> tuple[bool, str]:
        """Launch what the run needs for a result; whether a job is on it, and what was done."""
        ...


@dataclass
class LensFollower:
    """`FollowsLens` over lens-api and `proof lens`'s own launch."""

    api: LensApi
    client: str
    manual: Manual

    def stage(self, run_id: str) -> str | None:
        run = yield_runs.find(self.client, run_id)
        status = self.api.status(run.project_id, run.lens_run_id)
        return None if status is None else status.stage

    def ensure(self, run_id: str) -> tuple[bool, str]:
        try:
            run = yield_runs.find(self.client, run_id)
        except RunsUnknown as error:
            return False, str(error)
        outcome = ensure(self.api, run, self.manual, Waiting(timeout=0))
        said = "; ".join([*outcome.steps, outcome.detail] if outcome.detail else outcome.steps)
        return outcome.ok, f"{outcome.action}" + (f" ({said})" if said else "")
