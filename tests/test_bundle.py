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
from proof_headless.bundle import checks, commands, loader
from proof_headless.bundle.manifest import BundleInvalid, load
from proof_headless.cli import app
from tests.conftest import ROOT

EXAMPLE = ROOT / "examples" / "bundle-acme"
DIGEST = "sha256:" + "b" * 64
runner = CliRunner()

LENS_SEED = {
    "contract_version": "lens_declaration/0.1",
    "note": "Illustrative declaration for the compose stack's mock declaration source. Not a "
    "client's; nothing here was declared by anybody.",
    "sections": [{"field": "system_prompt", "text": "Eres el asistente virtual de un banco."}],
}


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
    assert bundle.catalogues[0].contract == "pi"
    assert bundle.brains[0].config == (bundle_dir / "brain/vitruvio.toml").resolve()


@pytest.mark.usefixtures("secret")
def test_the_example_bundle_is_valid(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "validate", str(bundle_dir)])
    assert result.exit_code == 0, result.output


def test_the_digest_is_lens_s_canonical_one_whatever_the_formatting() -> None:
    """Lens's `canonical_json`: sorted keys, compact separators, UTF-8 unescaped."""
    pretty = json.dumps(LENS_SEED, indent=4, ensure_ascii=True)
    assert checks.canonical_digest(json.loads(pretty)) == checks.canonical_digest(LENS_SEED)
    canonical = json.dumps(LENS_SEED, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    assert checks.canonical_digest(LENS_SEED) == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_missing_secret_is_named(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "validate", str(bundle_dir), "--json"])
    assert result.exit_code == 1
    problem = {"where": "secret ACME_TARGET_KEY", "what": "not in the secrets file, or empty"}
    assert problem in json.loads(result.stdout)


def _problems(bundle_dir: Path) -> dict[str, str]:
    found = checks.check(load(bundle_dir), frozenset({"ACME_TARGET_KEY"}))
    problems: dict[str, str] = {}
    for problem in found:
        problems[problem.where] = f"{problems.get(problem.where, '')} {problem.what}".strip()
    return problems


def test_each_problem_is_named(bundle_dir: Path) -> None:
    (bundle_dir / "catalogues/acme-support/catalogue.json").unlink()
    (bundle_dir / "catalogues/acme-support/grounding.json").write_text('{"other": []}')
    (bundle_dir / "priors/acme-phrasings.json").write_text("[]")
    (bundle_dir / "profiles/acme-assistant.json").write_text("{not json")
    declaration = next((bundle_dir / "declarations").iterdir())
    declaration.write_text(json.dumps({"contract_version": "lens_declaration/0.1", "sections": []}))
    problems = _problems(bundle_dir)
    assert "missing file" in problems["catalogue acme-support"]
    assert "non-empty list" in problems["prior acme-phrasings"]
    assert "not JSON" in problems["profile acme-assistant"]
    assert "canonical digest" in problems[f"declaration {declaration.name}"]


def test_a_grounding_sidecar_without_needs_base_is_named(bundle_dir: Path) -> None:
    (bundle_dir / "catalogues/acme-support/grounding.json").write_text('{"other": []}')
    assert "needs_base" in _problems(bundle_dir)["catalogue acme-support"]


def test_a_file_that_is_not_a_declaration_is_named(bundle_dir: Path) -> None:
    declaration = next((bundle_dir / "declarations").iterdir())
    declaration.write_text("{}")
    assert "not a Lens declaration" in _problems(bundle_dir)[f"declaration {declaration.name}"]


def test_a_path_outside_the_bundle_is_named(bundle_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.json"
    outside.write_text('["x"]')
    manifest = bundle_dir / "bundle.toml"
    manifest.write_text(
        manifest.read_text().replace("priors/acme-phrasings.json", "../elsewhere.json")
    )
    assert "outside the bundle" in _problems(bundle_dir)["prior acme-phrasings"]


def test_a_symlink_inside_a_catalogue_that_leaves_the_bundle_is_named(
    bundle_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "elsewhere.json"
    secret.write_text("{}")
    (bundle_dir / "catalogues/acme-support/pairs.json").symlink_to(secret)
    assert "resolves outside the bundle" in _problems(bundle_dir)["catalogue acme-support"]


def test_a_declaration_without_sections_is_not_a_declaration(bundle_dir: Path) -> None:
    declaration = next((bundle_dir / "declarations").iterdir())
    declaration.write_text(json.dumps({"contract_version": "lens_declaration/0.1"}))
    assert "not a Lens declaration" in _problems(bundle_dir)[f"declaration {declaration.name}"]


def test_a_broken_manifest_exits_2(tmp_path: Path) -> None:
    (tmp_path / "bundle.toml").write_text('client = "Not A Name"\nlanguage = "en"\n')
    with pytest.raises(BundleInvalid):
        load(tmp_path)
    assert runner.invoke(app, ["bundle", "validate", str(tmp_path)]).exit_code == 2


class FakeCompute:
    """Compute's publish routes: a version per name, unchanged on a second identical publish."""

    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.versions: dict[tuple[str, str], tuple[int, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((str(request.url.raw_path.decode()), body))
        if request.url.path == self.refuse:
            return httpx.Response(422, json={"detail": "refused by the six rejections"})
        key = (request.url.path, body["name"])
        content = json.dumps(body, sort_keys=True)
        version, seen = self.versions.get(key, (0, ""))
        created = content != seen
        if created:
            version += 1
            self.versions[key] = (version, content)
        answer = {"version": version, "created": created}
        return httpx.Response(201 if created else 200, json=answer)


def _vitruvio(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    envelope = {"ok": True, "data": {"digest": DIGEST}}
    return subprocess.CompletedProcess(list(args), 0, stdout=json.dumps(envelope), stderr="")


def _client(compute: FakeCompute) -> httpx.Client:
    return httpx.Client(base_url="http://compute", transport=httpx.MockTransport(compute))


def _load(bundle_dir: Path, compute: FakeCompute, tmp: Path, **kwargs: Any) -> loader.Receipt:
    options: dict[str, Any] = {
        "registry": "localhost:5051",
        "declarations_dir": tmp / "decl",
        "runner": _vitruvio,
        "insecure": True,
    }
    return loader.load(load(bundle_dir), _client(compute), **{**options, **kwargs})


def test_load_publishes_every_piece_with_the_bodies_compute_takes(
    bundle_dir: Path, tmp_path: Path
) -> None:
    compute = FakeCompute()
    pushed: list[list[str]] = []

    def vitruvio(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        pushed.append(list(args))
        return _vitruvio(args)

    receipt = _load(bundle_dir, compute, tmp_path, runner=vitruvio)
    routes = [route for route, _ in compute.requests]
    assert routes == ["/catalogues?contract=pi", "/profiles", "/priors", "/mappings"]
    assert compute.requests[2][1]["phrasings"][0] == "where is my order"
    assert pushed[0][-2:] == ["--json", "localhost:5051/acme/acme-eval"]
    assert "--insecure" in pushed[0]
    assert receipt.complete and receipt.error is None
    brain = next(item for item in receipt.loaded if item.kind == "brain")
    assert brain.digest == DIGEST
    assert len(list((tmp_path / "decl").iterdir())) == 1
    again = _load(bundle_dir, compute, tmp_path)
    assert all(item.created is False for item in again.loaded if item.kind == "catalogue")


def test_a_refusal_stops_the_load_with_an_incomplete_receipt_of_what_was_published(
    bundle_dir: Path, tmp_path: Path
) -> None:
    receipt = _load(bundle_dir, FakeCompute(refuse="/priors"), tmp_path)
    assert not receipt.complete
    assert "prior acme-phrasings: compute answered 422" in (receipt.error or "")
    assert [item.kind for item in receipt.loaded] == ["catalogue", "profile"]


def test_a_failed_brain_push_names_the_brain(bundle_dir: Path, tmp_path: Path) -> None:
    def failing(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 8, stdout="", stderr="histories diverged")

    receipt = _load(bundle_dir, FakeCompute(), tmp_path, runner=failing)
    assert "brain acme: vitruvio exited 8: histories diverged" in (receipt.error or "")


def test_skipping_brains_without_an_earlier_digest_stops_the_load(
    bundle_dir: Path, tmp_path: Path
) -> None:
    receipt = _load(bundle_dir, FakeCompute(), tmp_path, skip_brains=True, previous=None)
    assert not receipt.complete
    assert "needs a digest from an earlier load" in (receipt.error or "")


def test_a_stopped_load_keeps_the_last_receipt_s_live_digests(
    bundle_dir: Path, tmp_path: Path
) -> None:
    first = _load(bundle_dir, FakeCompute(), tmp_path)
    stopped = _load(bundle_dir, FakeCompute(refuse="/priors"), tmp_path, previous=first)
    assert not stopped.complete
    carried = {(i.kind, i.name): i for i in stopped.loaded if i.carried}
    assert carried[("brain", "acme")].digest == DIGEST
    assert ("prior", "acme-phrasings") in carried
    assert ("catalogue", "acme-support") not in carried


def test_without_an_explicit_choice_the_brain_s_config_decides_plain_http(
    bundle_dir: Path, tmp_path: Path
) -> None:
    pushed: list[list[str]] = []

    def vitruvio(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        pushed.append(list(args))
        return _vitruvio(args)

    _load(bundle_dir, FakeCompute(), tmp_path, runner=vitruvio, insecure=None)
    assert "--insecure" not in pushed[0] and "--no-insecure" not in pushed[0]


def test_skipping_brains_keeps_the_last_receipt_s_digests(bundle_dir: Path, tmp_path: Path) -> None:
    first = _load(bundle_dir, FakeCompute(), tmp_path)
    second = _load(bundle_dir, FakeCompute(), tmp_path, skip_brains=True, previous=first)
    brain = next(item for item in second.loaded if item.kind == "brain")
    assert (brain.digest, brain.carried) == (DIGEST, True)


def test_a_non_json_answer_is_a_refusal_not_a_crash(bundle_dir: Path, tmp_path: Path) -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    client = httpx.Client(base_url="http://compute", transport=httpx.MockTransport(html))
    receipt = loader.load(
        load(bundle_dir),
        client,
        registry="r",
        declarations_dir=tmp_path / "d",
        runner=_vitruvio,
        insecure=True,
    )
    assert "answered no JSON" in (receipt.error or "")


def test_load_refuses_an_invalid_bundle_before_publishing(bundle_dir: Path) -> None:
    result = runner.invoke(app, ["bundle", "load", str(bundle_dir), "--compute", "http://x:1"])
    assert result.exit_code == 1
    assert "secret ACME_TARGET_KEY" in result.output


@pytest.mark.usefixtures("secret")
def test_the_cli_writes_the_receipt_and_exits_1_when_the_load_stops(
    bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute = FakeCompute(refuse="/mappings")
    original = httpx.Client

    def client(*args: Any, **kwargs: Any) -> httpx.Client:
        return original(*args, transport=httpx.MockTransport(compute), **kwargs)

    monkeypatch.setattr("proof_headless.bundle.commands.httpx.Client", client)
    monkeypatch.setattr("proof_headless.docker.run", _vitruvio)
    result = runner.invoke(app, ["bundle", "load", str(bundle_dir)])
    assert result.exit_code == 1
    document = json.loads(commands.receipt_path(load(bundle_dir)).read_text())
    assert document["complete"] is False
    assert [item["kind"] for item in document["loaded"]] == ["catalogue", "profile", "prior"]


def test_a_stopped_load_does_not_carry_pieces_the_bundle_no_longer_declares(
    bundle_dir: Path, tmp_path: Path
) -> None:
    first = _load(bundle_dir, FakeCompute(), tmp_path)
    stale = loader.Loaded("prior", "retired-phrasings", version=3)
    first.loaded.append(stale)
    stopped = _load(bundle_dir, FakeCompute(refuse="/priors"), tmp_path, previous=first)
    assert ("prior", "retired-phrasings") not in {(i.kind, i.name) for i in stopped.loaded}


def test_a_directory_outside_the_bundle_is_one_problem(bundle_dir: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "catalogue-elsewhere"
    elsewhere.mkdir()
    for n in range(5):
        (elsewhere / f"f{n}.json").write_text("{}")
    manifest = bundle_dir / "bundle.toml"
    manifest.write_text(
        manifest.read_text().replace("catalogues/acme-support", "../catalogue-elsewhere")
    )
    found = checks.check(load(bundle_dir), frozenset({"ACME_TARGET_KEY"}))
    outside = [p for p in found if "outside the bundle" in p.what]
    assert len(outside) == 1
