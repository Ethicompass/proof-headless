"""`proof stack`: what `up` refuses, what it pulls and waits for, and what `status` shows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.docker import Compose
from proof_headless.stack import commands, render
from proof_headless.stack import lock as locks
from proof_headless.stack.lock import Lock
from tests.conftest import LOCK
from tests.fakes import FakeCompose

runner = CliRunner()
USE_LOCK = ["--lock", str(LOCK)]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCompose:
    document = render.compose(locks.load(LOCK), paths.secrets_file())
    compose = FakeCompose(
        polls_to_healthy=1,
        services=tuple(document["services"]),
        one_shots=frozenset(render.one_shot(document)),
        images={name: service["image"] for name, service in document["services"].items()},
        ports={
            "yield-api": [
                {"URL": "127.0.0.1", "TargetPort": 8080, "PublishedPort": 8080, "Protocol": "tcp"}
            ]
        },
    )

    def compose_for(lock: Lock) -> Compose:
        file = render.write(lock, paths.home() / "stack", paths.secrets_file())
        return Compose(render.PROJECT, file, env_file=paths.secrets_file(), runner=compose)

    monkeypatch.setattr(commands, "compose_for", compose_for)
    monkeypatch.setattr(commands, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(commands, "host_platform_name", lambda: "linux/arm64")
    return compose


def _docker_calls(fake: FakeCompose, verb: str) -> list[list[str]]:
    return [call for call in fake.calls if call[1] == verb]


def test_up_creates_the_models_network_pulls_the_job_images_and_waits_until_healthy(
    fake: FakeCompose,
) -> None:
    fake.missing_networks.add("proof-models")
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 0, result.output
    assert ["docker", "network", "create", "proof-models"] in fake.calls
    lock = locks.load(LOCK)
    pulled = [call[-1] for call in _docker_calls(fake, "pull")]
    assert pulled == [lock.image("yield-driver").pinned, lock.image("lens-run").pinned]
    assert any(call[-3:] == ["up", "--detach", "--remove-orphans"] for call in fake.calls)
    assert "healthy" in result.output


def test_up_refuses_images_with_no_build_for_this_host(fake: FakeCompose, tmp_path: Path) -> None:
    raw = yaml.safe_load(LOCK.read_text())
    raw["images"]["yield-api"]["platforms"] = ["linux/amd64"]
    amd64 = tmp_path / "amd64.lock"
    amd64.write_text(yaml.safe_dump(raw))
    result = runner.invoke(app, ["stack", "up", "--lock", str(amd64)])
    assert result.exit_code == 2
    assert "yield-api" in result.output and "linux/arm64" in result.output
    assert fake.calls == []


def test_up_refuses_a_secrets_file_others_can_read(fake: FakeCompose) -> None:
    path = paths.secrets_file()
    path.write_text("TARGET_KEY=x\n")
    path.chmod(0o644)
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 2 and "chmod 600" in result.output
    assert fake.calls == []


def test_up_refuses_a_secret_that_would_reconfigure_yield(fake: FakeCompose) -> None:
    path = paths.secrets_file()
    path.write_text("YIELD_STORE_BACKEND=memory\nYIELD_S3_SECRET_KEY=s3cret\n")
    path.chmod(0o600)
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 2
    assert "YIELD_STORE_BACKEND" in result.output
    assert "YIELD_S3_SECRET_KEY" not in result.output.split("would reconfigure")[0]


def test_a_failed_seed_fails_up_with_its_log(fake: FakeCompose) -> None:
    fake.fails = frozenset({"yield-seed"})
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 1
    assert "yield-seed did not come up" in result.output
    assert any(call[-5:-2] == ["logs", "--no-color", "--tail"] for call in fake.calls)


def test_status_shows_each_service_s_digest_state_and_ports(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    result = runner.invoke(app, ["stack", "status", *USE_LOCK, "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    rows = {row["service"]: row for row in document["services"]}
    lock = locks.load(LOCK)
    assert rows["yield-api"]["digest"] == lock.image("yield-api").digest
    assert rows["yield-api"]["health"] == "healthy"
    assert rows["yield-api"]["ports"] == ["127.0.0.1:8080->8080/tcp"]
    assert rows["yield-seed"]["exit_code"] == 0
    assert all(row["matches_lock"] for row in rows.values())


def test_status_before_up_reports_every_service_absent(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["stack", "status", *USE_LOCK])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output and "brain-registry" in result.output


def test_status_flags_a_container_running_another_image_than_the_lock(fake: FakeCompose) -> None:
    fake.images["lens-api"] = "ghcr.io/ethicompass/lens-api:old"
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    result = runner.invoke(app, ["stack", "status", *USE_LOCK, "--json"])
    rows = {row["service"]: row for row in json.loads(result.stdout)["services"]}
    assert rows["lens-api"]["matches_lock"] is False


def test_down_stops_the_project(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["stack", "down", *USE_LOCK])
    assert result.exit_code == 0, result.output
    assert any("down" in call for call in fake.calls)


def test_logs_refuses_a_service_the_stack_does_not_have(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["stack", "logs", "nope", *USE_LOCK])
    assert result.exit_code == 2


def test_a_missing_lock_is_refused_with_how_to_make_one() -> None:
    result = runner.invoke(app, ["stack", "status"])
    assert result.exit_code == 2
    assert "proof stack pin" in result.output
