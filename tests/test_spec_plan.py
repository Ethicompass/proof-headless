"""`proof spec plan`: each spec counted by Yield's own generation, never under its run id."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from proof_headless.cli import app
from proof_headless.runs.state import specs_dir
from proof_headless.spec import commands
from proof_headless.spec.plan import Planner, stage1_request
from tests.test_spec import _receipt, _render, bundle_dir  # noqa: F401

runner = CliRunner()


def _spec(run_id: str = "r1", **extra: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "kb_ref": {"registry": "brain-registry:5000", "repository": "acme", "digest": "sha256:b"},
        "kb_subjects": ["support"],
        "catalogues": ["acme-support"],
        "catalogue_versions": {"acme-support": 2},
        "strategies": ["ask-a", "ask-b"],
        "replicas": 3,
        "budget": {"max_target_calls": 100},
        **extra,
    }


def _probes(**by_strategy: int) -> list[dict[str, Any]]:
    return [
        {"id": f"{name}-{n}", "strategy": name.replace("_", "-")}
        for name, count in by_strategy.items()
        for n in range(count)
    ]


@dataclass
class FakeCompute:
    """Compute's generation surface: `/stage1` answers 202 with a job, `/jobs/{id}` plays the job's
    states one per poll, and `/probes/{digest}` serves the set."""

    sets: dict[str, list[dict[str, Any]]]
    """Probe sets by the spec's run id."""
    states: list[str] = field(default_factory=lambda: ["running", "succeeded"])
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    """Runs whose stage 1 already closed, answered from the record with no job."""
    refused: dict[str, str] = field(default_factory=dict)
    posted: list[dict[str, Any]] = field(default_factory=list)
    polls: dict[str, int] = field(default_factory=dict)

    def _run_of(self, preview_id: str) -> str:
        return next(run for run in self.sets if preview_id.split("-", 2)[2] == run[:33])

    def _report(self, run: str) -> dict[str, Any]:
        return {
            "probes_digest": f"digest-{run}",
            "probe_count": len(self.sets[run]),
            **self.reports.get(run, {}),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/stage1":
            body = json.loads(request.content)
            self.posted.append(body)
            run = self._run_of(body["run_id"])
            if run in self.refused:
                return httpx.Response(422, json={"detail": self.refused[run]})
            if run in self.closed:
                return httpx.Response(
                    200, json={"job_id": "x", "state": "succeeded", "report": self._report(run)}
                )
            return httpx.Response(202, json={"job_id": f"generate-{body['run_id']}"})
        if path.startswith("/jobs/"):
            job = path.removeprefix("/jobs/")
            seen = self.polls.get(job, 0)
            self.polls[job] = seen + 1
            state = self.states[min(seen, len(self.states) - 1)]
            run = self._run_of(job.removeprefix("generate-"))
            answer: dict[str, Any] = {"job_id": job, "state": state, "detail": f"{state} now"}
            if state == "succeeded":
                answer["report"] = self._report(run)
            return httpx.Response(200, json=answer)
        if path.startswith("/probes/"):
            return httpx.Response(200, json=self.sets[path.removeprefix("/probes/digest-")])
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://compute", transport=httpx.MockTransport(self.handler))


def _planner(fake: FakeCompute) -> Planner:
    return Planner(fake.client(), sleep=lambda _s: None)


def test_a_plan_counts_the_set_yield_generated_by_strategy_times_replicas() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=4, ask_b=2)})

    plans, failed = _planner(fake).plan({"r1": _spec()})

    assert failed == {}
    (plan,) = plans
    assert plan.probes_by_strategy == {"ask-a": 4, "ask-b": 2}
    assert (plan.probes, plan.replicas, plan.traces) == (6, 3, 18)
    assert plan.warnings == ()


def test_the_preview_never_writes_under_the_run_s_own_id() -> None:
    """The real run's stage-1 record must stay unwritten, or it would pin whatever this generated
    before the spec was submitted."""
    fake = FakeCompute({"r1": _probes(ask_a=1)})

    _planner(fake).plan({"r1": _spec()})

    (posted,) = fake.posted
    assert posted["run_id"] != "r1"
    assert posted["run_id"].startswith("plan-")
    assert posted["run_id"].endswith("-r1")


def test_the_request_is_the_one_the_driver_sends() -> None:
    body = stage1_request(_spec(context={"language": "es"}))

    assert {k: v for k, v in body.items() if k != "run_id"} == {
        "kb_ref": {"registry": "brain-registry:5000", "repository": "acme", "digest": "sha256:b"},
        "kb_subjects": ["support"],
        "catalogues": ["acme-support"],
        "catalogue_versions": {"acme-support": 2},
        "plugins": [],
        "strategies": ["ask-a", "ask-b"],
        "context": {"language": "es"},
        "generator": None,
        "contract_name": "pi",
        "contract_version": None,
    }


def test_the_preview_id_changes_with_what_generation_reads_and_only_with_it() -> None:
    base = stage1_request(_spec())["run_id"]

    assert stage1_request(_spec(replicas=1, budget={}))["run_id"] == base
    assert stage1_request(_spec(kb_subjects=["billing"]))["run_id"] != base
    assert stage1_request(_spec(catalogue_versions={"acme-support": 3}))["run_id"] != base


def test_a_preview_id_stays_within_yield_s_run_id_limit() -> None:
    """Compute names the generation job after the id, and refuses a longer one with a bare 500."""
    longest = "a" * 49 + "z"
    near = "r-" + "x" * 31 + "-y"

    for run_id in (longest, near):
        preview = stage1_request(_spec(run_id))["run_id"]
        assert len(preview) <= 50
        assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?", preview)
    assert (
        stage1_request(_spec(longest))["run_id"]
        != stage1_request(_spec(longest[:-1] + "q"))["run_id"]
    ), "truncated ids stay apart through the digest"


def test_fresh_asks_for_a_new_generation_of_the_same_request() -> None:
    assert stage1_request(_spec(), "n1")["run_id"] != stage1_request(_spec())["run_id"]


def test_a_set_already_generated_is_read_without_waiting() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=2, ask_b=1)}, closed={"r1"})

    plans, _ = _planner(fake).plan({"r1": _spec()})

    assert plans[0].probes == 3
    assert fake.polls == {}


def test_a_failed_generation_is_reported_with_compute_s_reason() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=1), "r2": _probes(ask_a=2)})
    fake.states = ["running", "failed"]

    plans, failed = _planner(fake).plan({"r1": _spec("r1"), "r2": _spec("r2")})

    assert plans == []
    assert set(failed) == {"r1", "r2"}
    assert "failed now" in failed["r1"]


def test_a_refused_request_does_not_stop_the_other_runs() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=1), "r2": _probes(ask_a=2, ask_b=1)})
    fake.refused["r1"] = "ContextRequired: contextual_sibling needs a generator model"

    plans, failed = _planner(fake).plan({"r1": _spec("r1"), "r2": _spec("r2")})

    assert [p.run_id for p in plans] == ["r2"]
    assert "ContextRequired" in failed["r1"]


def test_a_generation_that_never_finishes_times_out() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=1)}, states=["running"])
    ticks = iter(range(0, 10_000, 100))
    planner = Planner(
        fake.client(), sleep=lambda _s: None, clock=lambda: float(next(ticks)), timeout_seconds=250
    )

    _, failed = planner.plan({"r1": _spec()})

    assert "did not finish" in failed["r1"]


def test_what_generation_could_not_do_is_said() -> None:
    fake = FakeCompute(
        {"r1": _probes(ask_a=40)},
        reports={
            "r1": {
                "strategies_set_aside": ["ask-b"],
                "degenerate_strategies": ["ask-a"],
                "unresolved_entities": {"contextual_sibling": ["x", "y"]},
                "unverified_probes": ["p1"],
            }
        },
    )

    (plan,), _ = _planner(fake).plan({"r1": _spec()})

    said = "\n".join(plan.warnings)
    assert "no probe for ['ask-b']" in said
    assert "ask-b set aside" in said
    assert "ask-a produced only documented premises" in said
    assert "could not deform 2 entities" in said
    assert "1 probes whose doc label" in said
    assert "120 traces and a budget of 100 target calls" in said


def test_a_set_aside_strategy_the_run_did_not_ask_for_is_not_reported() -> None:
    fake = FakeCompute(
        {"r1": _probes(ask_a=1, ask_b=1)},
        reports={"r1": {"strategies_set_aside": ["unrelated", "ask-b"]}},
    )

    (plan,), _ = _planner(fake).plan({"r1": _spec(budget={})})

    said = "\n".join(plan.warnings)
    assert "ask-b set aside" in said
    assert "unrelated" not in said


def test_a_run_narrowed_by_plugins_hears_set_asides_as_the_catalogue_s() -> None:
    fake = FakeCompute(
        {"r1": _probes(ask_a=1)}, reports={"r1": {"strategies_set_aside": ["other-plugin-x"]}}
    )

    (plan,), _ = _planner(fake).plan({"r1": _spec(strategies=[], plugins=["leak"], budget={})})

    assert any("other-plugin-x of the catalogue" in w for w in plan.warnings)


def test_a_budget_with_little_room_over_the_traces_is_warned() -> None:
    fake = FakeCompute({"r1": _probes(ask_a=10, ask_b=10)})

    (plan,), _ = _planner(fake).plan({"r1": _spec(budget={"max_target_calls": 90})})

    assert any("more than one call per trace" in w for w in plan.warnings)


def test_the_command_plans_the_rendered_specs_and_totals_them(
    bundle_dir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _receipt(bundle_dir)
    assert _render(bundle_dir).exit_code == 0
    specs = {p.stem: json.loads(p.read_text()) for p in specs_dir("acme").glob("*.json")}
    fake = FakeCompute({run: _probes(ask_a=2) for run in specs})
    monkeypatch.setattr(commands, "_compute_client", lambda _url: fake.client())

    result = runner.invoke(app, ["spec", "plan", str(bundle_dir), "--json"])

    assert result.exit_code == 0, result.output
    planned = json.loads(result.stdout)
    assert planned["failed"] == {}
    assert planned["probes"] == 2 * len(specs)
    assert planned["traces"] == sum(2 * int(s.get("replicas") or 1) for s in specs.values())


def test_the_command_exits_non_zero_when_a_run_cannot_be_planned(
    bundle_dir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _receipt(bundle_dir)
    assert _render(bundle_dir).exit_code == 0
    runs = [p.stem for p in specs_dir("acme").glob("*.json")]
    fake = FakeCompute({run: _probes(ask_a=1) for run in runs})
    fake.refused[runs[0]] = "unknown catalogue"
    monkeypatch.setattr(commands, "_compute_client", lambda _url: fake.client())

    result = runner.invoke(app, ["spec", "plan", str(bundle_dir)])

    assert result.exit_code == 1
    assert "unknown catalogue" in " ".join(result.output.split())


def test_planning_needs_rendered_specs(bundle_dir: Path) -> None:  # noqa: F811
    result = runner.invoke(app, ["spec", "plan", str(bundle_dir)])

    assert result.exit_code == 2
    assert "proof spec render first" in " ".join(result.output.split())
