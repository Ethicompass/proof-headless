"""Bundles: the shipped example validates, each problem is named, and load publishes in order."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.bundle import checks, loader
from proof_headless.bundle.manifest import BundleInvalid, load
from proof_headless.cli import app
from tests.conftest import ROOT

EXAMPLE = ROOT / "examples" / "bundle-acme"
runner = CliRunner()


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    copy = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, copy)
    return copy


@pytest.fixture
def secret() -> None:
    path = paths.secrets_file()
    path.write_text("ACME_TARGET_KEY=k\n")
    path.chmod(0o600)


def test_paths_resolve_against_the_bundle(bundle_dir: Path) -> None:
    bundle = load(bundle_dir)
    assert bundle.client == "acme"
    assert bundle.catalogues[0].path == (bundle_dir / "catalogues/acme-support").resolve()
    assert bundle.brains[0].config == (bundle_dir / "brain/vitruvio.toml").resolve()


@pytest.mark.usefixtures("secret")
def test_the_example_bundle_is_valid(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "validate", str(bundle_dir)])
    assert result.exit_code == 0, result.output
    assert "Valid" in result.output


def test_a_missing_secret_is_named(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "validate", str(bundle_dir), "--json"])
    assert result.exit_code == 1
    assert {
        "where": "secret ACME_TARGET_KEY",
        "what": "not in the secrets file, or empty",
    } in json.loads(result.stdout)


@pytest.mark.usefixtures("secret")
def test_each_problem_is_named(bundle_dir: Path) -> None:
    (bundle_dir / "catalogues/acme-support/catalogue.json").unlink()
    (bundle_dir / "priors/acme-phrasings.json").write_text("[]")
    (bundle_dir / "profiles/acme-assistant.json").write_text("{not json")
    declaration = next((bundle_dir / "declarations").iterdir())
    declaration.write_text("edited after it was named")
    problems = {
        p.where: p.what for p in checks.check(load(bundle_dir), frozenset({"ACME_TARGET_KEY"}))
    }
    assert "missing file" in problems["catalogue acme-support"]
    assert "non-empty list" in problems["prior acme-phrasings"]
    assert "not JSON" in problems["profile acme-assistant"]
    assert "must be named by its SHA-256" in problems[f"declaration {declaration.name}"]


def test_a_broken_manifest_exits_2(tmp_path: Path) -> None:
    (tmp_path / "bundle.toml").write_text('client = "Not A Name"\nlanguage = "en"\n')
    with pytest.raises(BundleInvalid):
        load(tmp_path)
    assert runner.invoke(app, ["bundle", "validate", str(tmp_path)]).exit_code == 2


class FakeCompute:
    """Compute's publish routes: a version per name, unchanged on a second identical publish."""

    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.versions: dict[tuple[str, str], tuple[int, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append((request.url.path, body))
        if request.url.path == self.refuse:
            return httpx.Response(422, json={"detail": "refused by the six rejections"})
        key = (request.url.path, body["name"])
        content = json.dumps(body, sort_keys=True)
        version, seen = self.versions.get(key, (0, ""))
        created = content != seen
        if created:
            version += 1
            self.versions[key] = (version, content)
        return httpx.Response(
            201 if created else 200, json={"version": version, "created": created}
        )


def _vitruvio(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    envelope = {"ok": True, "data": {"digest": "sha256:" + "b" * 64}}
    return subprocess.CompletedProcess(list(args), 0, stdout=json.dumps(envelope), stderr="")


def test_load_publishes_every_piece_with_the_bodies_compute_takes(
    bundle_dir: Path, tmp_path: Path
) -> None:
    compute = FakeCompute()
    client = httpx.Client(base_url="http://compute", transport=httpx.MockTransport(compute))
    pushed: list[list[str]] = []

    def vitruvio(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        pushed.append(list(args))
        return _vitruvio(args)

    receipt = loader.load(
        load(bundle_dir),
        client,
        registry="localhost:5051",
        declarations_dir=tmp_path / "decl",
        runner=vitruvio,
    )
    assert [path for path, _ in compute.bodies] == [
        "/catalogues",
        "/profiles",
        "/priors",
        "/mappings",
    ]
    assert compute.bodies[2][1] == {
        "name": "acme-phrasings",
        "phrasings": ["where is my order", "can I return a toaster I bought last week"],
    }
    assert pushed[0][-1] == "localhost:5051/acme/acme-eval"
    kinds = [(item.kind, item.name) for item in receipt.loaded]
    assert kinds[:5] == [
        ("catalogue", "acme-support"),
        ("profile", "acme-assistant"),
        ("prior", "acme-phrasings"),
        ("mapping", "acme-dimensions"),
        ("brain", "acme"),
    ]
    assert receipt.loaded[4].digest == "sha256:" + "b" * 64
    assert len(list((tmp_path / "decl").iterdir())) == 1
    again = loader.load(
        load(bundle_dir), client, registry="r", declarations_dir=tmp_path / "decl", runner=_vitruvio
    )
    assert all(
        item.created is False for item in again.loaded if item.kind in {"catalogue", "profile"}
    )


def test_a_refusal_stops_the_load_and_names_the_piece(bundle_dir: Path, tmp_path: Path) -> None:
    client = httpx.Client(
        base_url="http://compute", transport=httpx.MockTransport(FakeCompute(refuse="/profiles"))
    )
    with pytest.raises(loader.LoadFailed, match="profile acme-assistant: compute answered 422"):
        loader.load(
            load(bundle_dir),
            client,
            registry="r",
            declarations_dir=tmp_path / "d",
            runner=_vitruvio,
        )


def test_a_failed_brain_push_names_the_brain(bundle_dir: Path, tmp_path: Path) -> None:
    def failing(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 8, stdout="", stderr="histories diverged")

    client = httpx.Client(base_url="http://compute", transport=httpx.MockTransport(FakeCompute()))
    with pytest.raises(
        loader.LoadFailed, match="brain acme: vitruvio exited 8: histories diverged"
    ):
        loader.load(
            load(bundle_dir), client, registry="r", declarations_dir=tmp_path / "d", runner=failing
        )


def test_load_refuses_an_invalid_bundle_before_publishing(bundle_dir: Path) -> None:
    result = runner.invoke(
        app, ["bundle", "load", str(bundle_dir), "--compute", "http://127.0.0.1:1"]
    )
    assert result.exit_code == 1
    assert "secret ACME_TARGET_KEY" in result.output
