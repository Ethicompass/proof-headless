"""`proof doctor` passes a server that answers the way runs need, and names what is wrong."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from proof_headless import doctor as doctor_module
from proof_headless import paths
from proof_headless.cli import app
from proof_headless.models.manifest import ModelEntry, load
from tests.conftest import SPARK_MANIFEST
from tests.fake_vllm import FakeVllm

runner = CliRunner()
MANIFEST = ["--manifest", str(SPARK_MANIFEST)]


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeVllm]:
    fakes = {model.name: FakeVllm() for model in load(SPARK_MANIFEST).models}

    def connect(model: ModelEntry, host: str, timeout: float) -> httpx.Client:
        transport = httpx.MockTransport(fakes[model.name])
        return httpx.Client(base_url=f"http://{host}:{model.port}", transport=transport)

    monkeypatch.setattr(doctor_module, "client_for", connect)
    original = doctor_module.examine

    def examine(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["connect"] = connect
        return original(*args, **kwargs)

    monkeypatch.setattr(doctor_module, "examine", examine)
    return fakes


def _results(output: str) -> dict[tuple[str, str], dict[str, object]]:
    document = json.loads(output)
    return {(r["model"], r["probe"]): r for r in document["results"]}


def test_every_role_gets_its_probes_and_a_healthy_set_passes(servers: dict[str, FakeVllm]) -> None:
    result = runner.invoke(app, ["doctor", *MANIFEST, "--json"])
    assert result.exit_code == 0, result.output
    probes = set(_results(result.stdout))
    assert probes == {
        ("chat", "chat"),
        ("chat", "logprobs"),
        ("chat", "structured output"),
        ("chat", "reasoning switch"),
        ("guardian", "guardian verdict"),
        ("rerank", "rerank"),
        ("embed", "embeddings"),
    }
    reports = list((paths.home() / "evidence").glob("doctor-*.json"))
    assert len(reports) == 1 and json.loads(reports[0].read_text())["passed"] is True


def test_the_judge_s_requests_ask_for_twenty_alternatives_with_reasoning_off(
    servers: dict[str, FakeVllm],
) -> None:
    runner.invoke(app, ["doctor", *MANIFEST, "--only", "chat", "--json"])
    asked = next(body for body in servers["chat"].seen if body.get("logprobs"))
    assert asked["top_logprobs"] == 20
    assert asked["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(
    ("model", "flaw", "probe"),
    [
        ("chat", {"widest": 5}, "logprobs"),
        ("chat", {"structured": "YES"}, "structured output"),
        ("chat", {"reasoning_obeys_switch": False}, "reasoning switch"),
        ("guardian", {"guard": "I cannot help with that"}, "guardian verdict"),
        ("rerank", {"relevant_first": False}, "rerank"),
    ],
)
def test_each_flaw_fails_its_probe_and_the_exit_code(
    servers: dict[str, FakeVllm], model: str, flaw: dict[str, object], probe: str
) -> None:
    for key, value in flaw.items():
        setattr(servers[model], key, value)
    result = runner.invoke(app, ["doctor", *MANIFEST, "--json"])
    assert result.exit_code == 1
    outcomes = _results(result.stdout)
    assert outcomes[(model, probe)]["ok"] is False
    failed = [key for key, value in outcomes.items() if not value["ok"]]
    assert failed == [(model, probe)]


def test_a_model_that_is_down_is_a_failed_probe_not_a_crash(servers: dict[str, FakeVllm]) -> None:
    servers["embed"].status = 503
    result = runner.invoke(app, ["doctor", *MANIFEST, "--only", "embed", "--json"])
    assert result.exit_code == 1
    assert "HTTP 503" in _results(result.stdout)[("embed", "embeddings")]["detail"]  # type: ignore[operator]


def test_the_rerank_scale_is_reported(servers: dict[str, FakeVllm]) -> None:
    result = runner.invoke(app, ["doctor", *MANIFEST, "--only", "rerank", "--json"])
    assert _results(result.stdout)[("rerank", "rerank")]["data"]["scale"] == "logit"  # type: ignore[index]


def test_the_table_view_names_the_report(servers: dict[str, FakeVllm]) -> None:
    result = runner.invoke(app, ["doctor", *MANIFEST, "--only", "embed"])
    assert result.exit_code == 0
    assert "embeddings" in result.output and "Report:" in result.output
