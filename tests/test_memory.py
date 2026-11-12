"""The memory preflight's arithmetic."""

from __future__ import annotations

from pathlib import Path

import pytest

from proof_headless.models.manifest import load
from proof_headless.models.memory import HostMemory, preflight, read_host_memory
from tests.conftest import SPARK_MANIFEST


def test_meminfo_is_read_in_gib(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       125829120 kB\nMemFree: 1 kB\nMemAvailable:   104857600 kB\n"
    )
    host = read_host_memory(meminfo)
    assert host == HostMemory(total=120.0, available=100.0)


def test_no_meminfo_is_no_answer(tmp_path: Path) -> None:
    assert read_host_memory(tmp_path / "absent") is None


def test_models_already_running_are_not_counted_twice() -> None:
    manifest = load(SPARK_MANIFEST)
    host = HostMemory(total=100.0, available=50.0)
    cold = preflight(manifest, host, frozenset())
    assert cold.needed == pytest.approx(76.0)
    assert not cold.fits
    warm = preflight(manifest, host, frozenset({"chat"}))
    assert warm.needed == pytest.approx(36.0)
    assert warm.fits


def test_only_the_models_being_started_are_counted() -> None:
    manifest = load(SPARK_MANIFEST)
    check = preflight(
        manifest, HostMemory(total=100.0, available=10.0), frozenset(), frozenset({"embed"})
    )
    assert [claim.name for claim in check.claims] == ["embed"]
    assert check.needed == pytest.approx(5.0)
    assert check.fits
