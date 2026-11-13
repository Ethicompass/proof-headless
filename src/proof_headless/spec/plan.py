"""`proof spec plan`: how many probes and traces each rendered spec will produce, before any run.

The count comes from Yield's own stage 1, not from arithmetic over the brain. Stage 1 is compute's
generation job: it reads the brain, applies the catalogue, deforms each entity into a premise and
writes the probe set, and it never touches the target. Counting entities per subject and kind
by hand, as the last engagement did, misses what generation does to them: strategies set aside for
the run's shape, entities a construction could not deform, and duplicates the library merges.

Each spec is planned under its own preview run id, `plan-<12 hex>-<run>`, so the real run's
stage-1 record stays unwritten and the run generates its own set when it starts. The count is the
same then: one probe per strategy and entity, and an entity a construction cannot deform still
becomes a probe. The words may not be, when a generator model writes premises. Planning a spec
again with the same request answers from the preview's record at once. A spec that names no
`contract_version` is generated against the newest contract, which the request does not show, so
`fresh` asks for a new generation regardless.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

PREVIEW_PREFIX = "plan-"

RUN_ID_MAX = 50
"""Yield's run id limit, which a preview id has to meet too: compute names the generation job after
it and refuses a longer one with a bare 500."""


class PlanFailed(RuntimeError):
    """Compute could not plan a spec: it refused the request, the job failed, or it timed out."""


def stage1_request(spec: Mapping[str, Any], nonce: str = "") -> dict[str, Any]:
    """The `/stage1` body Yield's driver would send for `spec`, under a preview run id.

    The id's digest covers the run id, the body and `nonce`, so two runs never share a preview and
    a new nonce forces a new generation. The run id is truncated to keep the whole within Yield's
    limit; the digest keeps truncated ids apart."""
    body: dict[str, Any] = {
        "kb_ref": spec.get("kb_ref"),
        "kb_subjects": list(spec.get("kb_subjects") or []),
        "catalogues": list(spec["catalogues"]),
        "catalogue_versions": dict(spec.get("catalogue_versions") or {}),
        "plugins": list(spec.get("plugins") or []),
        "strategies": list(spec.get("strategies") or []),
        "context": spec.get("context"),
        "generator": spec.get("generator"),
        "contract_name": spec.get("contract_name") or "pi",
        "contract_version": spec.get("contract_version"),
    }
    identity = {"run_id": spec["run_id"], "body": body, "nonce": nonce}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    head = f"{PREVIEW_PREFIX}{digest}-"
    tail = str(spec["run_id"])[: RUN_ID_MAX - len(head)].rstrip("-")
    return {"run_id": head + tail, **body}


@dataclass(frozen=True)
class Plan:
    """What one spec will produce."""

    run_id: str
    preview_id: str
    probes_by_strategy: dict[str, int]
    replicas: int
    max_target_calls: int | None
    warnings: tuple[str, ...] = field(default=())

    @property
    def probes(self) -> int:
        return sum(self.probes_by_strategy.values())

    @property
    def traces(self) -> int:
        """Probes times replicas: the Profiler's traces. The Exploiter, when the run has one, adds
        its own on top, within the same budget."""
        return self.probes * self.replicas


def _warnings(
    run_id: str,
    strategies: tuple[str, ...],
    by_strategy: Mapping[str, int],
    report: Mapping[str, Any],
    traces: int,
    max_calls: int | None,
) -> tuple[str, ...]:
    said: list[str] = []
    empty = [name for name in strategies if not by_strategy.get(name)]
    if empty:
        said.append(f"{run_id}: no probe for {empty}")
    for name in report.get("strategies_set_aside") or []:
        if name in strategies:
            said.append(f"{run_id}: {name} set aside for this run's shape")
        elif not strategies:
            said.append(
                f"{run_id}: {name} of the catalogue set aside for this run's shape "
                f"(compute lists the whole catalogue; it may not belong to this run's plugins)"
            )
    for name in report.get("degenerate_strategies") or []:
        said.append(f"{run_id}: {name} produced only documented premises")
    unresolved = report.get("unresolved_entities") or {}
    for construction, entities in unresolved.items():
        said.append(f"{run_id}: {construction} could not deform {len(entities)} entities")
    unverified = report.get("unverified_probes") or []
    if unverified:
        said.append(f"{run_id}: {len(unverified)} probes whose doc label was not verified")
    if max_calls is not None and traces > max_calls:
        said.append(
            f"{run_id}: {traces} traces and a budget of {max_calls} target calls; "
            f"the run stops before it attacks every probe"
        )
    elif max_calls is not None and traces * 2 > max_calls:
        said.append(
            f"{run_id}: {traces} traces against a budget of {max_calls} target calls; a "
            f"retry or a conducted conversation spends more than one call per trace"
        )
    return tuple(said)


class Planner:
    """Plans specs against one Yield compute."""

    def __init__(
        self,
        compute: httpx.Client,
        *,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        nonce: str = "",
    ) -> None:
        self.compute = compute
        self.nonce = nonce
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self.clock = clock

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.compute.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise PlanFailed(f"{self.compute.base_url}{path}: {error}") from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise PlanFailed(f"{path} answered {response.status_code}: {str(detail)[:500]}")
        return response.json()

    def launch(self, spec: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """Ask compute to generate `spec`'s set: the preview id, the job id, and the report when
        the preview's set was already generated."""
        request = stage1_request(spec, self.nonce)
        accepted = self._call("POST", "/stage1", json=request)
        return request["run_id"], str(accepted["job_id"]), dict(accepted.get("report") or {})

    def wait(self, job_id: str, started: float) -> dict[str, Any]:
        """The job's report once it succeeded."""
        while True:
            state = self._call("GET", f"/jobs/{job_id}")
            if state.get("state") == "succeeded" and state.get("report"):
                return dict(state["report"])
            if state.get("state") == "failed":
                raise PlanFailed(f"{job_id} failed: {state.get('detail') or 'no detail'}")
            if self.clock() - started > self.timeout_seconds:
                raise PlanFailed(f"{job_id} did not finish in {self.timeout_seconds:.0f} s")
            self.sleep(self.poll_seconds)

    def finish(self, spec: Mapping[str, Any], preview_id: str, report: Mapping[str, Any]) -> Plan:
        """Read the probe set the report names and count it by strategy."""
        probes = self._call("GET", f"/probes/{report['probes_digest']}")
        by_strategy = dict(sorted(Counter(str(p.get("strategy")) for p in probes).items()))
        replicas = int(spec.get("replicas") or 1)
        budget = spec.get("budget") or {}
        max_calls = budget.get("max_target_calls")
        max_calls = int(max_calls) if max_calls is not None else None
        strategies = tuple(spec.get("strategies") or ())
        traces = sum(by_strategy.values()) * replicas
        return Plan(
            run_id=str(spec["run_id"]),
            preview_id=preview_id,
            probes_by_strategy=by_strategy,
            replicas=replicas,
            max_target_calls=max_calls,
            warnings=_warnings(
                str(spec["run_id"]), strategies, by_strategy, report, traces, max_calls
            ),
        )

    def plan(self, specs: Mapping[str, Mapping[str, Any]]) -> tuple[list[Plan], dict[str, str]]:
        """Plan every spec: all generations are launched first, then awaited, so compute runs them
        side by side. Returns the plans and, per run, why one could not be planned."""
        started = self.clock()
        launched: dict[str, tuple[str, str, dict[str, Any]]] = {}
        failed: dict[str, str] = {}
        for run_id, spec in specs.items():
            try:
                launched[run_id] = self.launch(spec)
            except PlanFailed as error:
                failed[run_id] = str(error)
        plans: list[Plan] = []
        for run_id, (preview_id, job_id, report) in launched.items():
            try:
                closed = report or self.wait(job_id, started)
                plans.append(self.finish(specs[run_id], preview_id, closed))
            except PlanFailed as error:
                failed[run_id] = str(error)
        return plans, failed
