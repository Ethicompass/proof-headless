"""What `proof models up` shows while a model downloads: the bytes in its cache, and a rate."""

from __future__ import annotations

from pathlib import Path

import yaml

from proof_headless.models.manifest import Manifest
from proof_headless.models.progress import CacheProgress, cache_dir, directory_bytes, human_bytes
from tests.conftest import SPARK_MANIFEST


def _manifest(hf_cache: Path) -> Manifest:
    data = yaml.safe_load(SPARK_MANIFEST.read_text())
    data["hf_cache"] = str(hf_cache)
    return Manifest.model_validate(data)


def test_a_repository_downloads_under_the_hub_directory_of_the_cache(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    guardian = manifest.named("guardian")
    expected = tmp_path / "hub" / "models--meta-llama--Llama-Guard-4-12B"
    assert cache_dir(manifest, guardian) == expected
    local = guardian.model_copy(update={"hf_id": "/weights/guard"})
    assert cache_dir(manifest, local) is None


def test_progress_reads_a_growing_directory_and_its_rate(tmp_path: Path) -> None:
    directory = tmp_path / "models--org--name"
    blobs = directory / "blobs"
    now = [0.0]
    progress = CacheProgress(path=directory, clock=lambda: now[0])
    assert progress.sample() is None
    assert progress.describe() == ""

    blobs.mkdir(parents=True)
    (blobs / "a.incomplete").write_bytes(b"x" * 1024)
    assert progress.sample() == 1024
    assert progress.describe() == "1.0 KiB in cache"

    now[0] = 2.0
    (blobs / "a.incomplete").write_bytes(b"x" * 1024 * 1024 * 21)
    assert progress.sample() == 1024 * 1024 * 21
    assert progress.rate is not None and progress.rate > 10 * 1024 * 1024
    assert progress.describe() == "21.0 MiB in cache, 10.5 MiB/s"

    now[0] = 12.0
    progress.sample()
    assert progress.rate == 0
    assert progress.describe().endswith("0 B/s")


def test_snapshot_links_are_not_counted_twice(tmp_path: Path) -> None:
    (tmp_path / "blobs").mkdir()
    (tmp_path / "snapshots").mkdir()
    blob = tmp_path / "blobs" / "abc"
    blob.write_bytes(b"x" * 100)
    (tmp_path / "snapshots" / "model.safetensors").symlink_to(blob)
    assert directory_bytes(tmp_path) == 100


def test_human_bytes() -> None:
    assert human_bytes(512) == "512 B"
    assert human_bytes(3 * 1024**3) == "3.0 GiB"
