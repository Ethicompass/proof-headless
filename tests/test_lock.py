"""`appliance.lock`: every image by digest, or a refusal that says what is missing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from proof_headless.stack import lock as locks
from proof_headless.stack.lock import IMAGES, Image, LockInvalid
from tests.conftest import LOCK

DIGEST = "sha256:" + "a" * 64


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(LOCK.read_text())
    return raw


def test_the_shipped_lock_pins_every_image_by_digest() -> None:
    loaded = locks.load(LOCK)
    assert set(loaded.images) == set(IMAGES)
    assert loaded.channel == "develop"
    for image in loaded.images.values():
        assert "@" not in image.reference
        assert image.pinned == f"{image.reference}@{image.digest}"
        assert image.runs_on("linux/arm64") and image.runs_on("linux/amd64")


def test_a_lock_missing_an_image_says_which(tmp_path: Path) -> None:
    raw = _raw()
    del raw["images"]["lens-run"]
    path = tmp_path / "appliance.lock"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(LockInvalid, match="lens-run"):
        locks.load(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("digest", "sha256:short"), ("reference", "registry:2@" + DIGEST)],
)
def test_a_tag_is_never_a_digest_and_a_digest_is_never_a_tag(
    tmp_path: Path, field: str, value: str
) -> None:
    raw = _raw()
    raw["images"]["registry"][field] = value
    path = tmp_path / "appliance.lock"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(LockInvalid):
        locks.load(path)


def test_an_absent_lock_tells_the_operator_how_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(LockInvalid, match="proof stack pin"):
        locks.load(tmp_path / "absent.lock")


def test_dump_reads_back_as_the_same_lock(tmp_path: Path) -> None:
    loaded = locks.load(LOCK)
    path = tmp_path / "again.lock"
    path.write_text(locks.dump(loaded))
    assert locks.load(path) == loaded


def test_an_image_without_the_host_s_platform_does_not_run_on_it() -> None:
    amd64_only = Image(reference="r:1", digest=DIGEST, platforms=("linux/amd64",))
    assert not amd64_only.runs_on("linux/arm64")
    assert Image(reference="r:1", digest=DIGEST).runs_on("linux/arm64")
    assert Image(reference="r:1", digest=DIGEST, platforms=("linux/arm64/v8",)).runs_on(
        "linux/arm64"
    )


def test_both_channels_name_every_image() -> None:
    for references in locks.channels().values():
        assert set(references) == set(IMAGES)
