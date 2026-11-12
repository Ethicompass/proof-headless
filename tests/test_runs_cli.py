"""`proof run` against a fake Yield and a fake docker; the dashboard and plain mode."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import DataTable, RichLog, Static
from typer.testing import CliRunner

from proof_headless.cli import app
from proof_headless.docker import ServiceState
from proof_headless.runs import commands
from proof_headless.runs.board import Sampler, plain
from proof_headless.runs.client import RunStatus
from proof_headless.runs.dashboard import WatchApp
from proof_headless.runs.observe import JobContainers, Observation
from proof_headless.runs.state import Event, RunState, Store, specs_dir, state_dir
from tests.conftest import ROOT
from tests.fake_yield import HAPPY, FakeYield, job, status

EXAMPLE = ROOT / "examples" / "bundle-acme"
runner = CliRunner()


def _spec(run: str, **extra: object) -> None:
    extra.setdefault("webhook_url", "http://lens-api:8080/webhooks/yield")
    directory = specs_dir("acme")
    directory.mkdir(parents=True, exist_ok=True)
    spec = {
        "run_id": run,
        "metric_ids": ["ai_disclosure", "bias"],
        "metrics": {"ai_disclosure": {"judge": {"model": "j"}}, "bias": {"guardian": {}}},
        "judge": {"model": "j"},
        "embedder": {"model": "e"},
        "metric_mapping": "acme-dimensions",
        "kb_ref": {"repository": "acme/acme-eval"},
        "kb_subjects": ["acme-orders"],
        **extra,
    }
    (directory / f"{run}.json").write_text(json.dumps(spec))


def _later(minutes: float = 1.0) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _compute_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S.%fZ")


STACK = [
    {"Service": "yield-api", "State": "running", "Status": "Up 1 hour (healthy)"},
    {"Service": "lens-api", "State": "exited", "Status": "Exited (1) 2 minutes ago"},
    {"Service": "yield-seed", "State": "exited", "Status": "Exited (0) 1 hour ago"},
]


def _docker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if args[1] == "ps" and any("com.docker.compose.project=" in a for a in args):
        rows = [
            {
                **row,
                "Names": f"proof-stack-{row['Service']}-1",
                "Labels": f"com.docker.compose.service={row['Service']}",
            }
            for row in STACK
        ]
        listed = "\n".join(json.dumps(row) for row in rows)
        return subprocess.CompletedProcess(args, 0, stdout=listed, stderr="")
    if args[1] == "ps":
        row = {"Names": "yield-driver-r1", "State": "running", "Status": "Up 2 minutes"}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(row) + "\n", stderr="")
    stamp = _later().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    log = f"{stamp} openai.RateLimitError: Error code: 429\n"
    return subprocess.CompletedProcess(args, 0, stdout=log, stderr="")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeYield:
    yielded = FakeYield({"r1": HAPPY, "r2": HAPPY})
    monkeypatch.setattr(commands, "_yield", lambda api, compute: yielded.client())
    monkeypatch.setattr(commands, "JobContainers", lambda: JobContainers(runner=_docker))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return yielded


def test_queue_runs_every_spec_to_the_end(fake: FakeYield) -> None:
    _spec("r1")
    _spec("r2")
    result = runner.invoke(app, ["run", "queue", str(EXAMPLE), "--concurrency", "2"])
    assert result.exit_code == 0, result.output
    assert fake.posts == ["r1", "r2"]
    store = Store(state_dir("acme"))
    recorded = store.read()
    assert recorded is not None and recorded.succeeded
    kinds = [event.kind for event in store.events()]
    assert kinds[0] == "queue"
    assert kinds[-1] == "queue_done"
    assert "anomaly.rate_limited" in kinds
    assert "anomaly.stack_down" in kinds
    assert "no_webhook" not in kinds


def test_queue_refuses_to_start_over_an_unfinished_queue(fake: FakeYield) -> None:
    _spec("r1")
    fake.scripts["r1"] = [status("failed")]
    first = runner.invoke(app, ["run", "queue", str(EXAMPLE), "--retries", "0"])
    assert first.exit_code == 1
    finished_queue_is_replaced = runner.invoke(app, ["run", "queue", str(EXAMPLE)])
    assert finished_queue_is_replaced.exit_code == 1
    store = Store(state_dir("acme"))
    state = store.read()
    assert state is not None
    state.runs[0].state = RunState.ACTIVE
    store.write(state)
    refused = runner.invoke(app, ["run", "queue", str(EXAMPLE)])
    assert refused.exit_code == 2
    assert "--resume" in refused.output


def test_queue_without_specs_or_with_an_unknown_run_is_refused(fake: FakeYield) -> None:
    empty = runner.invoke(app, ["run", "queue", str(EXAMPLE)])
    assert empty.exit_code == 2
    _spec("r1")
    unknown = runner.invoke(app, ["run", "queue", str(EXAMPLE), "--only", "r9"])
    assert unknown.exit_code == 2
    assert "r9" in unknown.output
    resume = runner.invoke(app, ["run", "queue", str(EXAMPLE), "--resume"])
    assert resume.exit_code == 2
    assert "no queue is recorded" in resume.output


def test_status_json_reads_yield_and_the_queue(fake: FakeYield) -> None:
    _spec("r1")
    _spec("r2")
    fake.preload("r1", 1)
    result = runner.invoke(app, ["run", "status", str(EXAMPLE), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    rows = {row["run_id"]: row for row in document["runs"]}
    assert rows["r1"]["phase"] == "attacking"
    assert rows["r1"]["counts"] == "4/10"
    assert rows["r2"]["phase"] == "-"
    assert document["queue"] is None


FAILED = [status("failed", 8, liveness="dead")]


def test_remeasure_measures_a_failed_run_and_waits(fake: FakeYield) -> None:
    _spec("r1", webhook_url="http://receiver/hook")
    fake.scripts["r1"] = FAILED
    fake.preload("r1")
    fresh = _compute_stamp(_later())
    fake.jobs_after_post["measure-r1"] = [job("running", fresh), job("succeeded", fresh)]
    fake.manifests_after_post["r1"] = {"metrics": [{"metric_id": "bias", "status": "ran"}]}
    result = runner.invoke(
        app,
        ["run", "remeasure", str(EXAMPLE), "r1", "--metric", "bias", "--json"],
    )
    assert result.exit_code == 0, result.output
    (body,) = fake.metrics_posts
    assert body["metric_ids"] == ["bias"]
    assert body["metrics"] == {"bias": {"guardian": {}}}
    assert body["webhook_url"] == "http://receiver/hook"
    assert body["kb_subjects"] == ["acme-orders"]
    outcome = json.loads(result.stdout)
    assert outcome["state"] == "succeeded"
    assert outcome["metrics"] == {"bias": "ran"}
    kinds = [event.kind for event in Store(state_dir("acme")).events()]
    assert kinds == ["remeasure", "remeasured"]


def test_remeasure_fails_loudly_when_compute_answers_with_an_older_job(fake: FakeYield) -> None:
    """Another request reached compute between the check and this POST, so compute ignored this
    one: the job it shows predates the request, and its success is not this measurement's."""
    _spec("r1")
    fake.scripts["r1"] = FAILED
    fake.preload("r1")
    old = _compute_stamp(datetime.now(UTC) - timedelta(minutes=30))
    fake.jobs_after_post["measure-r1"] = [job("succeeded", old)]
    fake.manifests_after_post["r1"] = {"metrics": [{"metric_id": "bias", "status": "ran"}]}
    result = runner.invoke(app, ["run", "remeasure", str(EXAMPLE), "r1", "--json"])
    assert result.exit_code == 1
    outcome = json.loads(result.stdout)
    assert outcome["state"] == "old_record"
    assert outcome["metrics"] == {}
    assert "older job" in result.output


@pytest.mark.parametrize(
    ("script", "manifest", "held", "args", "message"),
    [
        (HAPPY[1:2], None, None, [], "attacking"),
        (HAPPY[-1:], None, None, [], "closes a run once"),
        (FAILED, {"phase": "failed"}, None, [], "closed with a manifest"),
        (FAILED, None, "succeeded", [], "still holds the record of measure-r1"),
        (FAILED, None, "running", [], "still holds the record of measure-r1"),
        (FAILED, None, None, ["--metric", "toxicity"], "toxicity"),
        (None, None, None, [], "no such run"),
    ],
)
def test_remeasure_refuses_what_yield_would_not_measure(
    fake: FakeYield,
    script: list[dict[str, object]] | None,
    manifest: dict[str, object] | None,
    held: str | None,
    args: list[str],
    message: str,
) -> None:
    _spec("r1")
    if script is not None:
        fake.scripts["r1"] = script
        fake.preload("r1")
    if manifest is not None:
        fake.manifests["r1"] = manifest
    if held is not None:
        fake.jobs["measure-r1"] = [job(held, "20261112T010000.000000Z")]
    result = runner.invoke(app, ["run", "remeasure", str(EXAMPLE), "r1", *args])
    assert result.exit_code == 2
    assert message in " ".join(result.output.split())
    assert fake.metrics_posts == []


def test_remeasure_says_when_yield_does_not_answer(
    fake: FakeYield, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spec("r1")
    fake.outage = 1
    result = runner.invoke(app, ["run", "remeasure", str(EXAMPLE), "r1"])
    assert result.exit_code == 1
    assert "did not answer" in result.output


def test_watch_plain_prints_runs_models_and_anomalies(fake: FakeYield, tmp_path: Path) -> None:
    _spec("r1")
    fake.preload("r1", 1)
    result = runner.invoke(
        app, ["run", "watch", str(EXAMPLE), "--plain", "--count", "1", "--interval", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "r1 attacking 4/10 failed 0 queue - liveness running" in result.stdout
    assert "rate_limited" in result.stdout
    assert "lens-api" in result.stdout
    assert "stack_down" in result.stdout
    assert fake.posts == []
    assert not Store(state_dir("acme")).events_path.exists()


def test_watch_falls_back_to_plain_without_a_terminal(fake: FakeYield) -> None:
    _spec("r1")
    fake.preload("r1", 1)
    result = runner.invoke(app, ["run", "watch", str(EXAMPLE), "--count", "1"])
    assert result.exit_code == 0, result.output
    assert "r1 attacking" in result.stdout


def test_watch_json_prints_one_snapshot(fake: FakeYield) -> None:
    _spec("r1")
    fake.preload("r1", 1)
    result = runner.invoke(app, ["run", "watch", str(EXAMPLE), "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.stdout)
    assert snapshot["runs"][0]["phase"] == "attacking"
    assert snapshot["stack"]["lens-api"].startswith("exited")
    kinds = {a["kind"] for a in snapshot["anomalies"]}
    assert {"rate_limited", "stack_down"} <= kinds


def _sampler(tmp_path: Path) -> Sampler:
    store = Store(tmp_path / "state")
    store.append(
        Event(at="2026-11-12T02:00:00+00:00", kind="submitted", run_id="r1", message="posted")
    )
    statuses = {
        "r1": RunStatus.model_validate({"run_id": "r1", **status("attacking", 4, failed=2)}),
        "r2": None,
    }

    def observe(ids: list[str]) -> Observation:
        return Observation(
            at=datetime(2026, 11, 12, 3, 0, tzinfo=UTC),
            runs=statuses,
            models={"chat": ServiceState("chat", "running", "unhealthy")},
        )

    return Sampler(store=store, client="acme", run_ids=["r1", "r2"], observe=observe)


def test_plain_prints_only_what_changed(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path)
    first = plain(sampler.sample(), None)
    assert any("models: chat" in line for line in first)
    assert any("submitted: posted" in line for line in first)
    assert any("failed_traces" in line for line in first)
    assert any("model_down" in line for line in first)
    board = sampler.sample()
    assert plain(board, board) == []


async def test_the_dashboard_shows_runs_models_and_the_feed(tmp_path: Path) -> None:
    watch = WatchApp(_sampler(tmp_path), interval=60)
    async with watch.run_test() as pilot:
        await watch.workers.wait_for_complete()
        await pilot.pause()
        table = watch.query_one("#runs", DataTable)
        assert table.row_count == 2
        assert table.get_row("r1")[2] == "attacking"
        assert "unhealthy" in str(watch.query_one("#models", Static).content)
        feed = watch.query_one("#feed", RichLog)
        text = "\n".join(line.text for line in feed.lines)
        assert "posted" in text
        assert "model_down" in text
        await pilot.press("q")
    assert watch.return_code == 0
