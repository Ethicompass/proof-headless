"""`proof models`: the order `up` keeps and what it refuses before anything starts."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
import typer
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
def test_a_model_not_healthy_by_its_deadline_stops_the_sequence_and_shows_its_log(
    fake: FakeCompose, tmp_path: Path
) -> None:
    data = yaml.safe_load(SPARK_MANIFEST.read_text())
    data["models"][1]["start_period_seconds"] = 0
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data))
    fake.fails = frozenset({"guardian"})
    result = runner.invoke(app, ["models", "up", "--manifest", str(path), "--grace", "0"])
    assert result.exit_code == 1
    assert fake.started() == ["chat", "guardian"]
    assert "not healthy" in result.output and "CUDA out of memory" in result.output


@pytest.mark.usefixtures("token")
def test_a_first_start_unhealthy_past_its_start_period_is_waited_on_while_it_runs(
    fake: FakeCompose,
) -> None:
    """The Spark's cold starts: the download outlasts the start period, the health check turns
    `unhealthy`, and the model comes up healthy minutes later on its own."""
    fake.unhealthy_polls = {"chat": 3, "guardian": 5}
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 0, result.output
    assert fake.started() == ["chat", "guardian", "rerank", "embed"]


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += 60.0


def _started(fake: FakeCompose, name: str) -> Compose:
    compose = Compose(render.PROJECT, paths.home() / "compose.yaml", runner=fake)
    compose.up(name)
    return compose


def test_an_unhealthy_model_that_never_recovers_fails_at_the_deadline(fake: FakeCompose) -> None:
    fake.fails = frozenset({"chat"})
    compose = _started(fake, "chat")
    model = Manifest.model_validate(yaml.safe_load(SPARK_MANIFEST.read_text())).named("chat")
    model = model.model_copy(update={"start_period_seconds": 600})
    clock = FakeClock()
    with pytest.raises(typer.Exit):
        commands.wait_healthy(compose, model, grace=600, sleep=clock.sleep, clock=clock)
    assert 1200 < clock.now <= 1260


def test_an_unhealthy_model_that_exits_fails_at_once(fake: FakeCompose) -> None:
    compose = _started(fake, "chat")
    fake.state["chat"].update({"State": "exited", "Health": "unhealthy", "ExitCode": 1})
    model = Manifest.model_validate(yaml.safe_load(SPARK_MANIFEST.read_text())).named("chat")
    clock = FakeClock()
    with pytest.raises(typer.Exit):
        commands.wait_healthy(compose, model, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == 0


def test_a_model_whose_container_is_gone_fails_at_once(fake: FakeCompose) -> None:
    compose = Compose(render.PROJECT, paths.home() / "compose.yaml", runner=fake)
    model = Manifest.model_validate(yaml.safe_load(SPARK_MANIFEST.read_text())).named("chat")
    clock = FakeClock()
    with pytest.raises(typer.Exit):
        commands.wait_healthy(compose, model, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == 0


@pytest.mark.usefixtures("token")
def test_pull_downloads_each_model_in_its_own_image_against_the_cache(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["models", "pull", *MANIFEST, "--only", "guardian"])
    assert result.exit_code == 0, result.output
    assert fake.ran() == [["hf", "guardian", "download", "meta-llama/Llama-Guard-4-12B"]]
    assert fake.started() == []


def test_pull_refuses_a_gated_model_without_a_token(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["models", "pull", *MANIFEST, "--only", "guardian"])
    assert result.exit_code == 2
    assert "HF_TOKEN" in result.output
    assert fake.ran() == []


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


@pytest.mark.usefixtures("token")
def test_up_creates_the_models_network_the_stack_shares_when_absent(fake: FakeCompose) -> None:
    fake.missing_networks.add("proof-models")
    result = runner.invoke(app, ["models", "up", *MANIFEST])
    assert result.exit_code == 0, result.output
    assert ["docker", "network", "create", "proof-models"] in fake.calls
