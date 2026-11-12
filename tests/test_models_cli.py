"""`proof models`: the order `up` keeps and what it refuses before anything starts."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.docker import Compose
from proof_headless.models import commands, memory, render
from proof_headless.models.manifest import Manifest
from tests.conftest import SPARK_MANIFEST
from tests.fakes import FakeCompose

runner = CliRunner()
MANIFEST = ["--manifest", str(SPARK_MANIFEST)]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCompose:
    compose = FakeCompose()

    def compose_for(manifest: Manifest) -> Compose:
        file = render.write(manifest, paths.home() / "models")
        return Compose(render.PROJECT, file, env_file=paths.secrets_file(), runner=compose)

    monkeypatch.setattr(commands, "compose_for", compose_for)
    monkeypatch.setattr(commands, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(memory, "read_host_memory", lambda: memory.HostMemory(128.0, 120.0))
    return compose


@pytest.fixture
def token() -> None:
    path = paths.secrets_file()
    path.write_text("HF_TOKEN=hf_x\n")
    path.chmod(0o600)


@pytest.mark.usefixtures("token")
def test_up_starts_one_model_at_a_time_each_after_the_previous_is_healthy(
    fake: FakeCompose,
) -> None:
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 0, result.output
    assert fake.started() == ["chat", "guardian", "rerank", "embed"]
    ups = [i for i, call in enumerate(fake.calls) if "up" in call]
    for previous, following in itertools.pairwise(ups):
        between = fake.calls[previous + 1 : following]
        assert any("ps" in call for call in between), "the next start waited on a health poll"


@pytest.mark.usefixtures("token")
def test_a_model_already_healthy_is_not_restarted(fake: FakeCompose) -> None:
    fake.state["chat"] = {"Service": "chat", "State": "running", "Health": "healthy"}
    result = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "chat"])
    assert result.exit_code == 0, result.output
    assert fake.started() == []


def test_a_gated_model_without_a_token_is_refused_before_anything_starts(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 2
    assert "HF_TOKEN" in result.output
    assert fake.started() == []


def test_a_secrets_file_others_can_read_is_refused(fake: FakeCompose) -> None:
    path = paths.secrets_file()
    path.write_text("HF_TOKEN=hf_x\n")
    path.chmod(0o644)
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 2
    assert "chmod 600" in result.output


@pytest.mark.usefixtures("token")
def test_short_memory_is_refused_with_the_arithmetic_unless_forced(
    fake: FakeCompose, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "read_host_memory", lambda: memory.HostMemory(128.0, 40.0))
    refused = runner.invoke(app, ["models", "up", *MANIFEST])
    assert refused.exit_code == 2
    assert "Memory preflight" in refused.output
    assert fake.started() == []
    forced = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "embed", "--force"])
    assert forced.exit_code == 0, forced.output


@pytest.mark.usefixtures("token")
def test_an_unhealthy_model_stops_the_sequence_and_shows_its_log(fake: FakeCompose) -> None:
    fake.fails = frozenset({"guardian"})
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 1
    assert fake.started() == ["chat", "guardian"]
    assert "CUDA out of memory" in result.output


def test_an_unknown_model_is_refused(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "nope"])
    assert result.exit_code == 2
    assert "no model 'nope'" in result.output


def test_status_as_json_lists_every_model_including_absent_ones(fake: FakeCompose) -> None:
    fake.state["chat"] = {"Service": "chat", "State": "running", "Health": "healthy"}
    result = runner.invoke(app, ["models", "status", *MANIFEST, "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["name"]: row for row in json.loads(result.output)}
    assert rows["chat"]["health"] == "healthy"
    assert rows["embed"]["state"] == "absent"


def test_validate_and_render(tmp_path: Path) -> None:
    assert runner.invoke(app, ["models", "validate", *MANIFEST]).exit_code == 0
    rendered = runner.invoke(app, ["models", "render", *MANIFEST])
    assert rendered.exit_code == 0
    assert (paths.home() / "models" / "compose.yaml").exists()


def test_an_invalid_manifest_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: []\n")
    result = runner.invoke(app, ["models", "validate", "--manifest", str(bad)])
    assert result.exit_code == 2
    assert "Invalid manifest" in result.output


@pytest.mark.usefixtures("token")
def test_a_model_in_a_restart_loop_fails_fast_with_its_log(fake: FakeCompose) -> None:
    fake.crash_loops = frozenset({"chat"})
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 1
    assert "restarts" in result.output and "CUDA out of memory" in result.output
    assert fake.started() == ["chat"]


@pytest.mark.usefixtures("token")
def test_starting_one_model_only_needs_memory_for_that_one(
    fake: FakeCompose, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "read_host_memory", lambda: memory.HostMemory(128.0, 10.0))
    result = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "embed"])
    assert result.exit_code == 0, result.output
    assert fake.started() == ["embed"]


def test_status_and_down_work_without_a_token(fake: FakeCompose) -> None:
    assert runner.invoke(app, ["models", "status", *MANIFEST]).exit_code == 0
    assert runner.invoke(app, ["models", "down", *MANIFEST]).exit_code == 0


def test_validate_as_json(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["models", "validate", *MANIFEST, "--json"])
    assert result.exit_code == 0
    assert {m["name"] for m in json.loads(result.stdout)["models"]} == {
        "chat",
        "guardian",
        "rerank",
        "embed",
    }


def test_a_manifest_over_budget_shows_the_shares_as_a_table(tmp_path: Path) -> None:
    data = yaml.safe_load(SPARK_MANIFEST.read_text())
    data["memory_budget"] = 0.5
    path = tmp_path / "over.yaml"
    path.write_text(yaml.safe_dump(data))
    result = runner.invoke(app, ["models", "validate", "--manifest", str(path)])
    assert result.exit_code == 2
    assert "Memory shares" in result.output and "0.76" in result.output


@pytest.mark.usefixtures("token")
def test_restarts_from_an_earlier_failed_start_do_not_fail_a_retry(fake: FakeCompose) -> None:
    fake.state["chat"] = {"Service": "chat", "State": "running", "Health": "starting"}
    fake.polls["chat"] = 0
    fake.restarts["chat"] = 7
    result = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "chat"])
    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("token")
def test_a_recreated_container_starts_its_restart_count_from_zero(fake: FakeCompose) -> None:
    """A fixed secret changes the configuration, so compose recreates the container whose old
    count was 7; the new container's zero restarts are not a failure."""
    fake.state["chat"] = {"Service": "chat", "State": "running", "Health": "starting"}
    fake.polls["chat"] = 0
    fake.restarts["chat"] = 7
    fake.recreate_on_up = True
    result = runner.invoke(app, ["models", "up", *MANIFEST, "--only", "chat"])
    assert result.exit_code == 0, result.output
