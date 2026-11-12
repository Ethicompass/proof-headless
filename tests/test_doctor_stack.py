"""`proof doctor --stack`: every stack service on its host port, and the lock in the report."""

from __future__ import annotations

import json
import shutil

import httpx
import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.stack import probes
from proof_headless.stack.render import PORTS
from tests.conftest import LOCK

runner = CliRunner()


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Status code per port; the registry wants a login, which still means it answers."""
    answers = {port: 200 for port in PORTS.values()}
    answers[PORTS["brain-registry"]] = 401

    def connect(base_url: str, timeout: float) -> httpx.Client:
        port = int(base_url.rsplit(":", 1)[1])

        def handle(request: httpx.Request) -> httpx.Response:
            if answers[port] == 0:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(answers[port])

        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(probes, "client_for", connect)
    monkeypatch.setattr(probes, "ping", lambda host, port, timeout: b"+PONG")
    return answers


def test_a_healthy_stack_passes_and_the_report_names_its_images(stack: dict[int, int]) -> None:
    paths.home().mkdir(parents=True)
    shutil.copy(LOCK, paths.default_lock())
    result = runner.invoke(app, ["doctor", "--no-models", "--stack", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    services = {r["model"] for r in document["results"]}
    assert services == {
        "yield-api",
        "yield-compute",
        "lens-api",
        "yield-valkey",
        "yield-minio",
        "lens-minio",
        "brain-registry",
    }
    assert document["stack"]["images"]["yield-api"].startswith("ghcr.io/ethicompass/yield-api:")


def test_a_service_that_does_not_answer_fails_doctor(stack: dict[int, int]) -> None:
    stack[PORTS["lens-api"]] = 0
    stack[PORTS["yield-compute"]] = 503
    result = runner.invoke(app, ["doctor", "--no-models", "--stack", "--json"])
    assert result.exit_code == 1
    failed = {r["model"] for r in json.loads(result.stdout)["results"] if not r["ok"]}
    assert failed == {"lens-api", "yield-compute"}


def test_valkey_must_answer_pong(monkeypatch: pytest.MonkeyPatch, stack: dict[int, int]) -> None:
    monkeypatch.setattr(probes, "ping", lambda host, port, timeout: b"-NOAUTH")
    result = runner.invoke(app, ["doctor", "--no-models", "--stack", "--json"])
    assert result.exit_code == 1


def test_nothing_to_probe_is_refused() -> None:
    assert runner.invoke(app, ["doctor", "--no-models"]).exit_code == 2
