"""The rehearsal bundle: it validates, its declaration is its system prompt, and it renders two
brainless runs whose target is the manifest's own chat model behind `openai_chat`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.bundle.checks import canonical_digest
from proof_headless.bundle.commands import receipt_path
from proof_headless.bundle.loader import Loaded, Receipt
from proof_headless.bundle.manifest import load
from proof_headless.cli import app
from proof_headless.models.manifest import load as load_manifest
from tests.conftest import ROOT, SPARK_MANIFEST

EXAMPLE = ROOT / "examples" / "bundle-rehearsal"
runner = CliRunner()


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    copy = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, copy)
    secrets = paths.secrets_file()
    secrets.write_text("REHEARSAL_TARGET_KEY=unused\n")
    secrets.chmod(0o600)
    return copy


def _receipt(bundle_dir: Path) -> None:
    bundle = load(bundle_dir)
    loaded = [Loaded("catalogue", "rehearsal", 1)]
    loaded += [
        Loaded("declaration", d.path.name, digest=f"sha256:{d.path.name}")
        for d in bundle.declarations
    ]
    receipt = Receipt(client="rehearsal", loaded=loaded, complete=True)
    path = receipt_path(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.document()))


def _specs(bundle_dir: Path) -> dict[str, dict[str, Any]]:
    _receipt(bundle_dir)
    result = runner.invoke(
        app,
        ["spec", "render", str(bundle_dir), "--manifest", str(SPARK_MANIFEST), "--json"],
    )
    assert result.exit_code == 0, result.output
    return {item["run_id"]: item["spec"] for item in json.loads(result.stdout)}


def test_the_rehearsal_bundle_is_valid(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "validate", str(bundle_dir)])
    assert result.exit_code == 0, result.output


def test_the_declaration_is_the_target_s_system_prompt(bundle_dir: Path) -> None:
    [declaration] = load(bundle_dir).declarations
    payload = json.loads(declaration.path.read_text())
    assert declaration.path.name == canonical_digest(payload)
    prompt = (bundle_dir / "target" / "system-prompt.txt").read_text().strip()
    assert payload["sections"] == [{"field": "system_prompt", "text": prompt}]


def test_every_strategy_stands_without_a_brain(bundle_dir: Path) -> None:
    catalogue = json.loads((bundle_dir / "catalogues/rehearsal/catalogue.json").read_text())
    assert catalogue["strategies"]
    assert all("{premise}" not in s["phrasing_hint"] for s in catalogue["strategies"])
    assert load(bundle_dir).brains == ()


def test_two_small_runs_attack_the_appliance_s_chat_model(bundle_dir: Path) -> None:
    specs = _specs(bundle_dir)
    assert sorted(specs) == ["r1-disclosure", "r2-injection"]
    chat = load_manifest(SPARK_MANIFEST).named("chat")
    for spec in specs.values():
        connector = spec["connector"]
        assert connector["kind"] == "openai_chat"
        assert connector["endpoint"] == f"http://{chat.name}:8000/v1/chat/completions"
        assert connector["options"] == {"model": chat.served_name}
        assert "kb_ref" not in spec and spec["kb_subjects"] == []
        assert spec["attackers"] == {}
        assert spec["budget"]["max_target_calls"] <= 20
        assert spec["replicas"] == 1
        assert spec["target_declaration"].startswith("sha256:")
        assert set(spec["metric_ids"]) == {"context", "ai_disclosure", "toxicity", "bias"}
        assert spec["metrics"]["bias"]["guardian"]["endpoint"] == "http://guardian:8000"
        assert spec["judge"]["model"] == chat.served_name
