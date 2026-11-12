"""The command surface renders and exits cleanly."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from proof_headless import __version__
from proof_headless.cli import app

runner = CliRunner()


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output


def test_version_as_json_is_parseable_and_names_the_host() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    facts = json.loads(result.output)
    assert facts["proof"] == __version__
    assert {"python", "machine", "system"} <= set(facts)


def test_version_as_a_table() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
