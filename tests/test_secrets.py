"""The secrets file: names only, and only from a file nobody else can read."""

from __future__ import annotations

from pathlib import Path

import pytest

from proof_headless.secrets import SecretsFileExposed, names


def test_names_and_never_values(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("# comment\nHF_TOKEN=hf_abc\nexport TARGET_KEY = xyz\n\nnot a line\n")
    path.chmod(0o600)
    assert names(path) == {"HF_TOKEN", "TARGET_KEY"}


def test_a_file_others_can_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("HF_TOKEN=hf_abc\n")
    path.chmod(0o644)
    with pytest.raises(SecretsFileExposed, match="chmod 600"):
        names(path)


def test_an_absent_file_defines_nothing(tmp_path: Path) -> None:
    assert names(tmp_path / "absent.env") == frozenset()


def test_a_name_with_an_empty_value_is_not_defined(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("HF_TOKEN=\nQUOTED=''\nTARGET_KEY=x\n")
    path.chmod(0o600)
    assert names(path) == {"TARGET_KEY"}
