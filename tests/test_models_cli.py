"""`proof models`: the order `up` keeps and what it refuses before anything starts."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
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
