"""The manifest refuses what would fail minutes into a start, and reads what the repo ships."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from proof_headless.models.manifest import Manifest, ManifestInvalid, Role, load
from tests.conftest import SPARK_MANIFEST


def _model(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "chat",
        "hf_id": "org/model",
        "served_name": "model",
        "image": "vllm/vllm-openai:v0",
        "port": 8001,
        "gpu_memory_utilization": 0.4,
        "roles": ["chat"],
        "api": "chat",
    }
    return {**base, **fields}


def test_the_shipped_spark_manifest_is_valid_and_within_its_budget() -> None:
    manifest = load(SPARK_MANIFEST)
    assert {m.name for m in manifest.models} == {"chat", "guardian", "rerank", "embed"}
    assert manifest.claimed() <= manifest.memory_budget
    assert manifest.named("guardian").gated
    assert Role.GUARDIAN in manifest.named("guardian").roles


@pytest.mark.parametrize("field", ["name", "port", "served_name"])
def test_two_models_sharing_an_identity_are_refused(field: str) -> None:
    other = _model(name="other", port=8002, served_name="other")
    other[field] = _model()[field]
    with pytest.raises(ValueError, match=f"share a {field}"):
        Manifest.model_validate({"models": [_model(), other]})


def test_shares_over_the_budget_are_refused_with_the_arithmetic() -> None:
    models = [_model(), _model(name="b", port=8002, served_name="b", gpu_memory_utilization=0.5)]
    with pytest.raises(ValueError, match=r"claim 0\.90 .* budget is 0\.85"):
        Manifest.model_validate({"models": models})


@pytest.mark.parametrize("flag", ["--port=9000", "--served-model-name", "--gpu-memory-utilization"])
def test_a_flag_proof_sets_is_refused_in_args(flag: str) -> None:
    with pytest.raises(ValueError, match="set by proof"):
        Manifest.model_validate({"models": [_model(args=[flag])]})


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="extra"):
        Manifest.model_validate({"models": [_model(gpu_memory=0.3)]})


def test_mount_sources_resolve_against_the_manifest_directory(tmp_path: Path) -> None:
    path = tmp_path / "m" / "models.yaml"
    path.parent.mkdir()
    mount = {"source": "templates/t.jinja", "target": "/t.jinja"}
    path.write_text(yaml.safe_dump({"models": [_model(mounts=[mount])]}))
    loaded = load(path)
    assert loaded.models[0].mounts[0].source == (path.parent / "templates/t.jinja").resolve()


def test_a_missing_or_broken_file_says_which(tmp_path: Path) -> None:
    with pytest.raises(ManifestInvalid, match="no manifest"):
        load(tmp_path / "absent.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("models: [")
    with pytest.raises(ManifestInvalid, match="not YAML"):
        load(broken)
    listed = tmp_path / "list.yaml"
    listed.write_text("- a\n")
    with pytest.raises(ManifestInvalid, match="mapping"):
        load(listed)
