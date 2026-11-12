"""The secrets file: names only, and only from a file nobody else can read."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.secrets import SecretRefused, SecretsFileExposed, names, write

runner = CliRunner()


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
    path.write_text(
        "HF_TOKEN= # paste here\nQUOTED='' # x\nDOUBLE=\"\" # note\nEMPTY=\n"
        "HASHED='a #b'\nTARGET_KEY=x # ok\n"
    )
    path.chmod(0o600)
    assert names(path) == {"HASHED", "TARGET_KEY"}


def test_write_sets_a_value_single_quoted_at_mode_600_and_keeps_other_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conf" / "secrets.env"
    write(path, "TARGET_KEY", "a$b")
    write(path, "HF_TOKEN", "hf_1")
    write(path, "TARGET_KEY", "c$d")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "HF_TOKEN='hf_1'\nTARGET_KEY='c$d'\n"
    assert names(path) == {"HF_TOKEN", "TARGET_KEY"}


@pytest.mark.parametrize(
    ("name", "value"),
    [("1BAD", "x"), ("OK", ""), ("OK", "it's"), ("OK", "two\nlines")],
)
def test_write_refuses_what_dotenv_cannot_hold(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(SecretRefused):
        write(tmp_path / "secrets.env", name, value)
    assert not (tmp_path / "secrets.env").exists()


def test_write_refuses_to_add_to_a_file_others_can_read(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("A=b\n")
    path.chmod(0o644)
    with pytest.raises(SecretsFileExposed):
        write(path, "TARGET_KEY", "x")


def test_set_reads_the_value_from_stdin_and_never_prints_it() -> None:
    result = runner.invoke(app, ["secrets", "set", "TARGET_KEY"], input="very-secret\n")
    assert result.exit_code == 0, result.output
    assert "very-secret" not in result.output
    assert paths.secrets_file().read_text() == "TARGET_KEY='very-secret'\n"
    listed = runner.invoke(app, ["secrets", "list", "--json"])
    assert json.loads(listed.stdout)["names"] == ["TARGET_KEY"]
    assert "very-secret" not in listed.output


def test_list_refuses_an_exposed_file() -> None:
    path = paths.secrets_file()
    path.write_text("A=b\n")
    path.chmod(0o640)
    assert runner.invoke(app, ["secrets", "list"]).exit_code == 2
