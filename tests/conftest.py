"""Every test gets its own appliance home and secrets file, so nothing reads the operator's."""

from __future__ import annotations

from pathlib import Path

import pytest

from proof_headless import paths

ROOT = Path(__file__).resolve().parents[1]
SPARK_MANIFEST = ROOT / "manifests" / "dgx-spark" / "models.yaml"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(paths.HOME_VARIABLE, str(tmp_path / "home"))
    monkeypatch.setenv(paths.SECRETS_VARIABLE, str(tmp_path / "secrets.env"))
    return tmp_path


LOCK = ROOT / "appliance.lock"
