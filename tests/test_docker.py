"""The compose seam: the arguments it builds and the states it reads back."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from proof_headless.docker import Compose, ComposeFailed, Restarts


def _answer(stdout: str = "", code: int = 0):  # type: ignore[no-untyped-def]
    seen: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(args))
        return subprocess.CompletedProcess(list(args), code, stdout=stdout, stderr="boom")

    return runner, seen


def test_the_env_file_is_passed_only_when_it_exists(tmp_path: Path) -> None:
    runner, seen = _answer()
    env = tmp_path / "secrets.env"
    Compose("p", tmp_path / "c.yaml", env_file=env, runner=runner).up("chat")
    assert "--env-file" not in seen[-1]
    env.write_text("A=b\n")
    Compose("p", tmp_path / "c.yaml", env_file=env, runner=runner).up("chat")
    assert seen[-1][-5:] == [str(env), "up", "--detach", "--no-deps", "chat"]


@pytest.mark.parametrize("layout", ["lines", "array"])
def test_ps_reads_both_json_layouts_compose_has_printed(layout: str, tmp_path: Path) -> None:
    rows = [
        {"Service": "chat", "State": "running", "Health": "healthy"},
        {"Service": "embed", "State": "exited", "Health": ""},
    ]
    stdout = "\n".join(map(json.dumps, rows)) if layout == "lines" else json.dumps(rows)
    runner, _ = _answer(stdout)
    states = Compose("p", tmp_path / "c.yaml", runner=runner).ps()
    assert states["chat"].health == "healthy"
    assert states["embed"].state == "exited"


def test_a_non_zero_exit_raises_with_the_command_and_its_error(tmp_path: Path) -> None:
    runner, _ = _answer(code=1)
    with pytest.raises(ComposeFailed, match="exited 1: boom"):
        Compose("p", tmp_path / "c.yaml", runner=runner).down()


def test_down_removes_services_a_newer_manifest_dropped(tmp_path: Path) -> None:
    runner, seen = _answer()
    Compose("p", tmp_path / "c.yaml", runner=runner).down()
    assert seen[-1][-2:] == ["down", "--remove-orphans"]


def test_restarts_reads_the_container_s_restart_count(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        stdout = "abc123\n" if "--quiet" in args else "3\n"
        return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")

    found = Compose("p", tmp_path / "c.yaml", runner=runner).restarts("chat")
    assert (found.container, found.count) == ("abc123", 3)
    assert calls[-1] == ["docker", "inspect", "--format", "{{.RestartCount}}", "abc123"]


def test_restarts_since_a_baseline_belong_to_one_container() -> None:
    assert Restarts("a", 9).since(Restarts("a", 7)) == 2
    assert Restarts("b", 0).since(Restarts("a", 7)) == 0
    assert Restarts("b", 2).since(Restarts("a", 7)) == 2
    assert Restarts("a", 1).since(Restarts(None, 0)) == 1
