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


def test_without_a_terminal_no_command_prints_the_help_instead_of_the_shell() -> None:
    """CliRunner gives the command no terminal, the way `ssh host proof` or a pipe would."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "version" in result.output
