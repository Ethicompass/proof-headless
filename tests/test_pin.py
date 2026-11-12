"""`proof stack pin`: tags to digests through the registry, never by guessing."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.docker import Runner
from proof_headless.stack import commands, lock
from proof_headless.stack.pin import Resolved, ResolveFailed, Resolver, imagetools, pin

runner = CliRunner()
INDEX_DIGEST = "sha256:" + "1" * 64
INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "digest": INDEX_DIGEST,
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "unknown", "architecture": "unknown"}},
        {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
    ],
}
SINGLE = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "2" * 64}


def fake_registry(seen: list[str]) -> Resolver:
    def resolve(reference: str) -> Resolved:
        seen.append(reference)
        return Resolved(digest="sha256:" + f"{len(seen):064x}", platforms=("linux/arm64",))

    return resolve


def test_pin_resolves_every_image_of_the_channel() -> None:
    seen: list[str] = []
    now = datetime(2026, 11, 12, tzinfo=UTC)
    pinned = pin("develop", fake_registry(seen), now=lambda: now)
    assert sorted(seen) == sorted(lock.channels()["develop"].values())
    assert pinned.channel == "develop" and pinned.resolved_at == now
    assert pinned.image("yield-api").reference.endswith(":develop")


def test_an_override_replaces_one_reference_and_an_unknown_name_is_refused() -> None:
    seen: list[str] = []
    pinned = pin("release", fake_registry(seen), {"lens-run": "ghcr.io/x/lens-run:rc1"})
    assert pinned.image("lens-run").reference == "ghcr.io/x/lens-run:rc1"
    with pytest.raises(KeyError, match="nope"):
        pin("release", fake_registry([]), {"nope": "x:1"})


def _imagetools_runner(answers: Mapping[str, object], code: int = 0) -> Runner:
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        template = args[-1]
        return subprocess.CompletedProcess(
            list(args), code, stdout=json.dumps(answers[template]), stderr="denied"
        )

    return run


def test_imagetools_reads_the_index_digest_and_its_platforms_without_attestations() -> None:
    resolve = imagetools(_imagetools_runner({"{{json .Manifest}}": INDEX}))
    assert resolve("r:1") == Resolved(INDEX_DIGEST, ("linux/amd64", "linux/arm64/v8"))


def test_a_single_platform_image_is_pinned_with_the_platform_its_config_names() -> None:
    answers = {
        "{{json .Manifest}}": SINGLE,
        "{{json .Image}}": {"os": "linux", "architecture": "amd64"},
    }
    assert imagetools(_imagetools_runner(answers))("r:1").platforms == ("linux/amd64",)


def test_a_registry_refusal_is_a_resolve_failure_naming_the_reference() -> None:
    resolve = imagetools(_imagetools_runner({"{{json .Manifest}}": {}}, code=1))
    with pytest.raises(ResolveFailed, match="r:1: denied"):
        resolve("r:1")


def test_the_command_writes_a_lock_that_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "imagetools", lambda: fake_registry([]))
    monkeypatch.setattr(commands, "host_platform_name", lambda: "linux/arm64")
    result = runner.invoke(app, ["stack", "pin", "--channel", "develop"])
    assert result.exit_code == 0, result.output
    assert lock.load(paths.default_lock()).channel == "develop"


def test_the_default_channel_is_develop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "imagetools", lambda: fake_registry([]))
    monkeypatch.setattr(commands, "host_platform_name", lambda: "linux/arm64")
    assert runner.invoke(app, ["stack", "pin"]).exit_code == 0
    assert lock.load(paths.default_lock()).channel == "develop"


def test_pin_refuses_a_channel_with_no_build_for_the_target_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commands, "imagetools", lambda: fake_registry([]))
    result = runner.invoke(app, ["stack", "pin", "--platform", "linux/amd64"])
    assert result.exit_code == 2
    assert "linux/amd64" in " ".join(result.output.split())
    assert not paths.default_lock().exists()


def test_the_command_refuses_an_unknown_channel() -> None:
    result = runner.invoke(app, ["stack", "pin", "--channel", "nightly"])
    assert result.exit_code == 2
