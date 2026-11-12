"""`proof run` against a fake Yield and a fake docker; the dashboard and plain mode."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
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
from tests.fake_yield import HAPPY, FakeYield, status

EXAMPLE = ROOT / "examples" / "bundle-acme"
runner = CliRunner()


def _spec(run: str, **extra: object) -> None:
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


def _docker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if args[1] == "ps":
        row = {"Names": "yield-driver-r1", "State": "running", "Status": "Up 2 minutes"}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(row) + "\n", stderr="")
    log = "2026-11-12T02:59:00Z openai.RateLimitError: Error code: 429\n"
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


def test_remeasure_posts_the_specs_metrics_and_waits(fake: FakeYield) -> None:
    _spec("r1", webhook_url="http://receiver/hook")
    fake.preload("r1", len(HAPPY) - 1)
    fake.jobs["measure-r1"] = ["running", "succeeded"]
    fake.manifests["r1"] = {"metrics": [{"metric_id": "bias", "status": "ran"}]}
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
    assert json.loads(result.stdout)["metrics"] == {"bias": "ran"}
    kinds = [event.kind for event in Store(state_dir("acme")).events()]
    assert kinds == ["remeasure", "remeasured"]


@pytest.mark.parametrize(
    ("index", "args", "message"),
    [
        (1, [], "attacking"),
        (len(HAPPY) - 1, ["--metric", "toxicity"], "toxicity"),
        (None, [], "has no run"),
    ],
)
def test_remeasure_refuses_what_cannot_be_measured(
    fake: FakeYield, index: int | None, args: list[str], message: str
) -> None:
    _spec("r1")
    if index is not None:
        fake.preload("r1", index)
    result = runner.invoke(app, ["run", "remeasure", str(EXAMPLE), "r1", *args])
    assert result.exit_code == 2
    assert message in result.output
    assert fake.metrics_posts == []


def test_watch_plain_prints_runs_models_and_anomalies(fake: FakeYield, tmp_path: Path) -> None:
    _spec("r1")
    fake.preload("r1", 1)
    result = runner.invoke(
        app, ["run", "watch", str(EXAMPLE), "--plain", "--count", "1", "--interval", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "r1 attacking 4/10 failed 0 queue - liveness running" in result.stdout
    assert "rate_limited" in result.stdout
    assert fake.posts == []
    assert not Store(state_dir("acme")).events_path.exists()


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
