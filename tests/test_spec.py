"""Run specs: the example renders the reviewed specs, and what cannot be pinned is refused."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from proof_headless.bundle.commands import receipt_path
from proof_headless.bundle.loader import Loaded, Receipt
from proof_headless.bundle.manifest import load
from proof_headless.cli import app
from tests.conftest import ROOT, SPARK_MANIFEST

EXAMPLE = ROOT / "examples" / "bundle-acme"
GOLDEN = Path(__file__).parent / "golden" / "acme-specs.json"
BRAIN_DIGEST = "sha256:" + "b" * 64
runner = CliRunner()


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    copy = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, copy)
    return copy


def _receipt(bundle_dir: Path, *, complete: bool = True, drop: str | None = None) -> None:
    bundle = load(bundle_dir)
    loaded = [Loaded("catalogue", "acme-support", 2), Loaded("profile", "acme-assistant", 1)]
    loaded += [Loaded("prior", "acme-phrasings", 1), Loaded("mapping", "acme-dimensions", 3)]
    loaded.append(Loaded("brain", "acme", digest=BRAIN_DIGEST))
    loaded += [
        Loaded("declaration", d.path.name, digest=f"sha256:{d.path.name}")
        for d in bundle.declarations
    ]
    receipt = Receipt(
        client="acme",
        at="2026-11-12T00:00:00+00:00",
        loaded=[item for item in loaded if item.name != drop],
        complete=complete,
        error=None if complete else "profile acme-assistant: compute answered 422",
    )
    path = receipt_path(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.document()))


def _render(bundle_dir: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        ["spec", "render", str(bundle_dir), "--manifest", str(SPARK_MANIFEST), "--json", *extra],
    )


def _said(result: Any) -> str:
    """The output with Rich's line wrapping undone."""
    return " ".join(str(result.output).split())


def _specs(bundle_dir: Path, *extra: str) -> dict[str, dict[str, Any]]:
    result = _render(bundle_dir, *extra)
    assert result.exit_code == 0, result.output
    return {item["run_id"]: item["spec"] for item in json.loads(result.stdout)}


def _models(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = [
        (role, spec[role]) for role in ("judge", "generator", "embedder") if role in spec
    ]
    found += [(f"attackers.{a}", model) for a, model in spec["attackers"].items()]
    found += [
        (f"metrics.{m}.{slot}", model)
        for m, config in spec["metrics"].items()
        for slot, model in config.items()
        if slot != "params"
    ]
    return found


def test_the_example_bundle_renders_the_reviewed_specs(
    bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every spec, against a reviewed copy: a change to what a run asks Yield for shows up as a
    diff of this file. `UPDATE_GOLDEN=1` rewrites it."""
    monkeypatch.setenv("HOME", "/home/operator")
    _receipt(bundle_dir)
    rendered = json.dumps(_specs(bundle_dir), indent=2) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(rendered)
    assert rendered == GOLDEN.read_text()


def test_specs_are_written_to_the_bundle_s_state_directory(bundle_dir: Path) -> None:
    _receipt(bundle_dir)
    result = runner.invoke(
        app, ["spec", "render", str(bundle_dir), "--manifest", str(SPARK_MANIFEST)]
    )
    assert result.exit_code == 0, result.output
    assert "r1-disclosure" in _said(result)
    directory = receipt_path(load(bundle_dir)).parent / "specs"
    assert sorted(p.name for p in directory.iterdir()) == ["r1-disclosure.json", "r2-returns.json"]
    spec = json.loads((directory / "r2-returns.json").read_text())
    assert spec["kb_ref"]["digest"] == BRAIN_DIGEST
    assert spec["catalogue_versions"] == {"acme-support": 2}
    assert spec["connector"]["profile"] == {"name": "acme-assistant", "version": 1}
    assert spec["connector"]["min_interval_seconds"] == 1.0


def test_every_spec_sharing_a_model_paces_it_at_the_same_rate(bundle_dir: Path) -> None:
    _receipt(bundle_dir)
    rates: dict[str, set[float]] = {}
    for spec in _specs(bundle_dir).values():
        for _, model in _models(spec):
            rates.setdefault(model["endpoint"], set()).add(model["requests_per_second"])
    assert len(rates) == 3
    assert all(len(seen) == 1 for seen in rates.values()), rates


def test_extra_body_goes_only_on_chat_roles_and_switches_reasoning(bundle_dir: Path) -> None:
    _receipt(bundle_dir)
    for spec in _specs(bundle_dir).values():
        for role, model in _models(spec):
            chat = model["endpoint"].endswith("/v1")
            switched = chat and role != "generator" and "social" not in role
            assert ("extra_body" in model) == switched
            assert model["provider"] == "openai_compatible" and model["self_hosted"] is True
    spec = _specs(bundle_dir)["r2-returns"]
    switch = {"enable_thinking": True}
    assert spec["judge"]["extra_body"]["chat_template_kwargs"] == switch
    assert spec["metrics"]["context"]["judge"]["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert spec["metrics"]["toxicity"]["embedder"]["endpoint"] == "http://embed:8000/v1/embeddings"
    assert _specs(bundle_dir)["r1-disclosure"]["metrics"]["bias"]["guardian"]["endpoint"] == (
        "http://guardian:8000"
    )


def test_a_role_bound_to_a_model_without_that_role_is_refused(bundle_dir: Path) -> None:
    _receipt(bundle_dir)
    result = _render(bundle_dir, "--bind", "judge=embed")
    assert result.exit_code == 2
    assert "role judge needs a chat model, and embed serves ['embed']" in _said(result)


def test_a_role_bound_to_an_unknown_model_is_refused(bundle_dir: Path) -> None:
    _receipt(bundle_dir)
    result = _render(bundle_dir, "--bind", "guardian=absent")
    assert result.exit_code == 2
    assert "no model 'absent'" in _said(result)


def test_a_model_without_pacing_is_refused(bundle_dir: Path) -> None:
    roles = bundle_dir / "roles.toml"
    roles.write_text(roles.read_text().replace("embed = 4.0\n", ""))
    _receipt(bundle_dir)
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "model embed has no pacing" in _said(result)


def test_reasoning_off_on_a_model_with_no_switch_is_refused(
    bundle_dir: Path, tmp_path: Path
) -> None:
    manifest = tmp_path / "models.yaml"
    manifest.write_text(SPARK_MANIFEST.read_text().replace("reasoning_switch: enable_thinking", ""))
    _receipt(bundle_dir)
    result = runner.invoke(
        app, ["spec", "render", str(bundle_dir), "--manifest", str(manifest), "--json"]
    )
    assert result.exit_code == 2
    assert "needs reasoning off" in _said(result)


def test_an_unknown_catalogue_is_refused(bundle_dir: Path) -> None:
    matrix = bundle_dir / "matrix.toml"
    text = matrix.read_text()
    matrix.write_text(text.replace('catalogue = "acme-support"', 'catalogue = "x"', 1))
    _receipt(bundle_dir)
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "unknown catalogue 'x'" in _said(result)


def test_a_bundle_never_loaded_is_refused(bundle_dir: Path) -> None:
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "no receipt" in _said(result)


def test_a_bundle_whose_last_load_stopped_is_refused(bundle_dir: Path) -> None:
    _receipt(bundle_dir, complete=False)
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "compute answered 422" in _said(result)


def test_a_piece_missing_from_the_receipt_is_refused(bundle_dir: Path) -> None:
    _receipt(bundle_dir, drop="acme")
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "brain acme has no digest in the receipt" in _said(result)


def test_a_metric_without_declared_roles_is_refused(bundle_dir: Path) -> None:
    matrix = bundle_dir / "matrix.toml"
    matrix.write_text(matrix.read_text().replace('"bias"]', '"conversational"]'))
    _receipt(bundle_dir)
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "metric conversational has no [metrics.conversational] table" in _said(result)


def test_a_connector_secret_the_bundle_does_not_declare_is_refused(bundle_dir: Path) -> None:
    matrix = bundle_dir / "matrix.toml"
    matrix.write_text(matrix.read_text().replace('"ACME_TARGET_KEY"', '"OTHER_KEY"'))
    _receipt(bundle_dir)
    result = _render(bundle_dir)
    assert result.exit_code == 2
    assert "'OTHER_KEY' is not one of the bundle's secrets" in _said(result)


def test_a_run_id_yield_would_refuse_is_refused_here(bundle_dir: Path) -> None:
    matrix = bundle_dir / "matrix.toml"
    matrix.write_text(matrix.read_text().replace('id = "r1-disclosure"', 'id = "R1_disclosure"'))
    _receipt(bundle_dir)
    assert _render(bundle_dir).exit_code == 2


def test_the_reranker_takes_the_full_rerank_url_and_no_extra_body(bundle_dir: Path) -> None:
    matrix = bundle_dir / "matrix.toml"
    matrix.write_text(
        matrix.read_text().replace('["context", "toxicity"]', '["context", "regulatory"]')
    )
    _receipt(bundle_dir)
    reranker = _specs(bundle_dir)["r2-returns"]["metrics"]["regulatory"]["reranker"]
    assert reranker["endpoint"] == "http://rerank:8000/v1/rerank"
    assert "extra_body" not in reranker
