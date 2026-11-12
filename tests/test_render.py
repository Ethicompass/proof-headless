"""What the models would run with, read without a daemon."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from proof_headless.models import render
from proof_headless.models.manifest import load
from tests.conftest import SPARK_MANIFEST


def test_each_model_is_a_service_on_the_models_network_reachable_on_loopback_only() -> None:
    manifest = load(SPARK_MANIFEST)
    document = render.compose(manifest)
    assert document["name"] == render.PROJECT
    assert document["networks"] == {"proof-models": {"name": "proof-models", "external": True}}
    for model in manifest.models:
        service = document["services"][model.name]
        assert service["ports"] == [f"127.0.0.1:{model.port}:8000"]
        assert service["networks"] == ["proof-models"]
        assert service["image"] == model.image


def test_the_command_puts_proof_s_flags_first_and_the_manifest_s_verbatim_after() -> None:
    chat = load(SPARK_MANIFEST).named("chat")
    command = render.command(chat)
    assert command[:9] == [
        chat.hf_id,
        "--served-model-name",
        chat.served_name,
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--gpu-memory-utilization",
        "0.4",
    ]
    assert command[9:] == list(chat.args)


def test_only_a_gated_model_gets_the_token_and_its_absence_does_not_break_compose() -> None:
    document = render.compose(load(SPARK_MANIFEST))
    assert document["services"]["guardian"]["environment"]["HF_TOKEN"] == "${HF_TOKEN:-}"
    assert "HF_TOKEN" not in document["services"]["chat"]["environment"]


def test_the_health_check_waits_out_the_declared_load_time_and_the_gpu_is_reserved() -> None:
    manifest = load(SPARK_MANIFEST)
    service = render.compose(manifest)["services"]["chat"]
    assert (
        service["healthcheck"]["start_period"] == f"{manifest.named('chat').start_period_seconds}s"
    )
    devices = service["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]


GOLDEN = Path(__file__).parent / "golden" / "dgx-spark-compose.yaml"


def test_the_spark_manifest_renders_the_reviewed_compose_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole document, against a reviewed copy: a change to what the models run with shows up
    as a diff of this file. `UPDATE_GOLDEN=1` rewrites it."""
    monkeypatch.setenv("HOME", "/home/operator")
    rendered = yaml.safe_dump(render.compose(load(SPARK_MANIFEST)), sort_keys=False)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(rendered)
    assert rendered == GOLDEN.read_text()
