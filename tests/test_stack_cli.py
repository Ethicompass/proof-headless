"""`proof stack`: what `up` refuses, what it pulls and waits for, and what `status` shows."""

from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from proof_headless import paths, secrets
from proof_headless.cli import app
from proof_headless.docker import Compose
from proof_headless.models.manifest import load
from proof_headless.stack import commands, credentials, render
from proof_headless.stack import lock as locks
from tests.conftest import EXAMPLE_BUNDLE, LOCK, SPARK_MANIFEST
from tests.fakes import FakeCompose

runner = CliRunner()
USE_LOCK = ["--lock", str(LOCK), "--manifest", str(SPARK_MANIFEST)]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCompose:
    document = render.compose(locks.load(LOCK), load(SPARK_MANIFEST))
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

    def project(file: Path) -> Compose:
        return Compose(render.PROJECT, file, env_file=paths.secrets_file(), runner=compose)

    monkeypatch.setattr(commands, "project", project)
    monkeypatch.setattr(commands, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(commands, "host_platform_name", lambda: "linux/arm64")
    return compose


def _text(output: str) -> str:
    """Output with Rich's line wrapping undone, which moves with the terminal's width."""
    return " ".join(output.split())


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
    assert "healthy" in _text(result.output)


def test_up_refuses_images_with_no_build_for_this_host(fake: FakeCompose, tmp_path: Path) -> None:
    raw = yaml.safe_load(LOCK.read_text())
    raw["images"]["yield-api"]["platforms"] = ["linux/amd64"]
    amd64 = tmp_path / "amd64.lock"
    amd64.write_text(yaml.safe_dump(raw))
    result = runner.invoke(
        app, ["stack", "up", "--lock", str(amd64), "--manifest", str(SPARK_MANIFEST)]
    )
    assert result.exit_code == 2
    assert "yield-api" in _text(result.output) and "linux/arm64" in _text(result.output)
    assert fake.calls == []


def test_up_refuses_a_secrets_file_others_can_read(fake: FakeCompose) -> None:
    path = paths.secrets_file()
    path.write_text("TARGET_KEY=x\n")
    path.chmod(0o644)
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 2 and "chmod 600" in _text(result.output)
    assert fake.calls == []


def test_up_refuses_a_bundle_secret_the_file_does_not_define(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["stack", "up", *USE_LOCK, "--bundle", str(EXAMPLE_BUNDLE)])
    assert result.exit_code == 2
    assert "ACME_TARGET_KEY" in _text(result.output)
    assert fake.calls == []


def test_up_maps_a_bundle_s_secrets_into_yield_and_nothing_else(fake: FakeCompose) -> None:
    path = paths.secrets_file()
    path.write_text("ACME_TARGET_KEY='k'\nHF_TOKEN='hf'\n")
    path.chmod(0o600)
    result = runner.invoke(app, ["stack", "up", *USE_LOCK, "--bundle", str(EXAMPLE_BUNDLE)])
    assert result.exit_code == 0, result.output
    document = render.read(commands.stack_file())
    assert "ACME_TARGET_KEY" in document["services"]["yield-api"]["environment"]
    assert "hf" not in commands.stack_file().read_text()


def test_up_refuses_a_manifest_with_no_chat_model(fake: FakeCompose, tmp_path: Path) -> None:
    raw = yaml.safe_load(SPARK_MANIFEST.read_text())
    raw["models"] = [m for m in raw["models"] if "chat" not in m["roles"]]
    manifest = tmp_path / "models.yaml"
    manifest.write_text(yaml.safe_dump(raw))
    result = runner.invoke(app, ["stack", "up", "--lock", str(LOCK), "--manifest", str(manifest)])
    assert result.exit_code == 2
    assert "chat role" in _text(result.output)


def test_a_failed_seed_fails_up_with_its_log(fake: FakeCompose) -> None:
    fake.fails = frozenset({"yield-seed"})
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 1
    assert "yield-seed did not come up" in _text(result.output)
    assert any(call[-5:-2] == ["logs", "--no-color", "--tail"] for call in fake.calls)


def test_status_shows_each_service_s_digest_state_and_ports(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    result = runner.invoke(app, ["stack", "status", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    rows = {row["service"]: row for row in document["services"]}
    lock = locks.load(LOCK)
    assert rows["yield-api"]["digest"] == lock.image("yield-api").digest
    assert rows["yield-api"]["health"] == "healthy"
    assert rows["yield-api"]["ports"] == ["127.0.0.1:8080->8080/tcp"]
    assert rows["yield-seed"]["exit_code"] == 0
    assert all(row["matches_lock"] for row in rows.values())


def test_status_after_render_reports_every_service_absent(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "render", *USE_LOCK, "--write"])
    result = runner.invoke(app, ["stack", "status", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["services"]
    assert {row["state"] for row in rows} == {"absent"}
    assert "brain-registry" in {row["service"] for row in rows}


def test_read_commands_never_re_render_the_started_stack(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    file = commands.stack_file()
    file.write_text(file.read_text() + "# started from this\n")
    for command in (["status"], ["logs", "lens-api"], ["down"]):
        assert runner.invoke(app, ["stack", *command]).exit_code == 0
    assert file.read_text().endswith("# started from this\n")


def test_read_commands_before_any_up_say_how_to_start(fake: FakeCompose) -> None:
    for command in (["status"], ["logs", "lens-api"], ["down"]):
        result = runner.invoke(app, ["stack", *command])
        assert result.exit_code == 2
        assert "proof stack up" in _text(result.output)


def test_status_flags_a_container_running_another_image_than_the_lock(fake: FakeCompose) -> None:
    fake.images["lens-api"] = "ghcr.io/ethicompass/lens-api:old"
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    result = runner.invoke(app, ["stack", "status", "--json"])
    rows = {row["service"]: row for row in json.loads(result.stdout)["services"]}
    assert rows["lens-api"]["matches_lock"] is False


def test_down_stops_the_project(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "up", *USE_LOCK])
    result = runner.invoke(app, ["stack", "down"])
    assert result.exit_code == 0, result.output
    assert any("down" in call for call in fake.calls)


def test_logs_refuses_a_service_the_stack_does_not_have(fake: FakeCompose) -> None:
    runner.invoke(app, ["stack", "render", *USE_LOCK, "--write"])
    result = runner.invoke(app, ["stack", "logs", "nope"])
    assert result.exit_code == 2


def test_a_missing_lock_is_refused_with_how_to_make_one() -> None:
    result = runner.invoke(app, ["stack", "up", "--manifest", str(SPARK_MANIFEST)])
    assert result.exit_code == 2
    assert "proof stack pin" in _text(result.output)


def test_up_generates_the_stack_s_credentials_once_into_the_secrets_file(
    fake: FakeCompose,
) -> None:
    first = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert first.exit_code == 0, first.output
    path = paths.secrets_file()
    held = secrets.values(path)
    assert set(credentials.NAMES) <= set(held)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = commands.stack_file().read_text()
    assert not any(held[name] in text for name in credentials.NAMES)
    second = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert second.exit_code == 0, second.output
    assert secrets.values(path) == held


def test_up_refuses_a_store_started_with_credentials_the_file_lacks(fake: FakeCompose) -> None:
    fake.volumes.add("proof-stack_lens_minio")
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 2
    assert "docker volume rm proof-stack_lens_minio" in _text(result.output)
    assert not commands.stack_file().exists()
    assert not paths.secrets_file().exists()


def _secrets(text: str) -> None:
    path = paths.secrets_file()
    path.write_text(text)
    path.chmod(0o600)


def _second_bundle(tmp_path: Path) -> Path:
    copy = tmp_path / "beta"
    shutil.copytree(EXAMPLE_BUNDLE, copy)
    manifest = copy / "bundle.toml"
    text = manifest.read_text().replace('"ACME_TARGET_KEY"', '"BETA_TARGET_KEY"')
    manifest.write_text(re.sub(r'(?m)^client = ".*"$', 'client = "beta"', text))
    return copy


def _yield_names() -> set[str]:
    environment = render.read(commands.stack_file())["services"]["yield-api"]["environment"]
    return {name for name in environment if name.endswith("_TARGET_KEY")}


def test_every_up_gives_yield_the_secrets_of_every_served_bundle(
    fake: FakeCompose, tmp_path: Path
) -> None:
    _secrets("ACME_TARGET_KEY='a'\nBETA_TARGET_KEY='b'\n")
    assert runner.invoke(app, ["stack", "up", *USE_LOCK, "-b", str(EXAMPLE_BUNDLE)]).exit_code == 0
    beta = _second_bundle(tmp_path)
    assert runner.invoke(app, ["stack", "up", *USE_LOCK, "-b", str(beta)]).exit_code == 0
    assert _yield_names() == {"ACME_TARGET_KEY", "BETA_TARGET_KEY"}
    assert runner.invoke(app, ["stack", "up", *USE_LOCK]).exit_code == 0
    assert _yield_names() == {"ACME_TARGET_KEY", "BETA_TARGET_KEY"}
    status = json.loads(runner.invoke(app, ["stack", "status", "--json"]).stdout)
    assert set(status["bundles"]) == {"acme", "beta"}


def test_forget_bundle_takes_its_secrets_out_on_the_next_up(
    fake: FakeCompose, tmp_path: Path
) -> None:
    _secrets("ACME_TARGET_KEY='a'\nBETA_TARGET_KEY='b'\n")
    beta = _second_bundle(tmp_path)
    up = ["stack", "up", *USE_LOCK, "-b", str(EXAMPLE_BUNDLE), "-b", str(beta)]
    assert runner.invoke(app, up).exit_code == 0
    assert runner.invoke(app, ["stack", "forget-bundle", "acme"]).exit_code == 0
    assert runner.invoke(app, ["stack", "up", *USE_LOCK]).exit_code == 0
    assert _yield_names() == {"BETA_TARGET_KEY"}
    assert runner.invoke(app, ["stack", "forget-bundle", "acme"]).exit_code == 2


def test_up_with_no_bundle_warns_that_every_run_is_refused(fake: FakeCompose) -> None:
    result = runner.invoke(app, ["stack", "up", *USE_LOCK])
    assert result.exit_code == 0, result.output
    assert "every run is refused" in _text(result.output)


def test_render_previews_without_touching_the_running_stack_s_file(fake: FakeCompose) -> None:
    _secrets("ACME_TARGET_KEY='a'\n")
    assert runner.invoke(app, ["stack", "up", *USE_LOCK, "-b", str(EXAMPLE_BUNDLE)]).exit_code == 0
    before = commands.stack_file().read_text()
    result = runner.invoke(app, ["stack", "render", *USE_LOCK])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["name"] == render.PROJECT
    assert commands.stack_file().read_text() == before
