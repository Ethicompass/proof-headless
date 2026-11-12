"""`proof lens`: derived ids, status, and a launch that is idempotent and never shows a value."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.lens import commands, launcher
from proof_headless.lens.api import LensApi
from proof_headless.lens.ensure import Manual, Waiting, ensure
from proof_headless.lens.runs import RunsUnknown, YieldRun, client_runs
from tests.fake_lens import SECRET_VALUE, FakeDocker, FakeLens

DECLARATION = "sha256:" + "d" * 64
PROJECT = "decl-" + "d" * 20
runner = CliRunner()


def specs(*run_ids: str, declaration: str | None = DECLARATION) -> Path:
    directory = paths.home() / "bundles" / "acme" / "specs"
    directory.mkdir(parents=True, exist_ok=True)
    for run_id in run_ids:
        spec: dict[str, str] = {"run_id": run_id}
        if declaration:
            spec["target_declaration"] = declaration
        (directory / f"{run_id}.json").write_text(json.dumps(spec))
    return directory


@pytest.fixture
def lens(monkeypatch: pytest.MonkeyPatch) -> FakeLens:
    fake = FakeLens()
    monkeypatch.setattr(commands, "connect", lambda url, timeout: fake.client())
    return fake


@pytest.fixture
def daemon(lens: FakeLens, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker(lens)
    monkeypatch.setattr(commands, "runner", fake)
    return fake


def no_wait() -> Waiting:
    return Waiting(timeout=60, interval=1, sleep=lambda _: None)


def test_a_declared_run_derives_lens_project_from_its_declaration() -> None:
    run = YieldRun("r1", DECLARATION)
    assert run.project_id == PROJECT
    assert run.lens_run_id == "r1"


def test_an_undeclared_run_derives_a_project_of_its_own() -> None:
    expected = "undeclared-" + hashlib.sha256(b"r1").hexdigest()[:16]
    assert YieldRun("r1", None).project_id == expected


def test_runs_come_from_the_queue_state_when_there_is_one() -> None:
    specs("r1", "r2")
    queue = paths.home() / "bundles" / "acme" / "queue.json"
    queue.write_text(json.dumps({"runs": [{"run_id": "r2", "phase": "done"}, {"run_id": "r1"}]}))
    assert [run.run_id for run in client_runs("acme")] == ["r2", "r1"]
    assert client_runs("acme")[0].declaration == DECLARATION


def test_runs_come_from_the_rendered_specs_without_a_queue() -> None:
    specs("r2", "r1")
    assert [run.run_id for run in client_runs("acme")] == ["r1", "r2"]


def test_a_client_with_neither_is_refused() -> None:
    with pytest.raises(RunsUnknown, match="proof spec render"):
        client_runs("acme")


def test_status_lists_each_run_with_its_derived_ids_and_result(lens: FakeLens) -> None:
    specs("r1", "r2", "r3")
    lens.put(f"{PROJECT}/r1", "done", result_ref="sha256:" + "e" * 64)
    lens.put(f"{PROJECT}/r2", "accepted")
    result = runner.invoke(app, ["lens", "status", "acme", "--json"])
    assert result.exit_code == 1, result.output
    rows = {row["yield_run"]: row for row in json.loads(result.stdout)}
    assert rows["r1"]["result"] == "sha256:" + "e" * 64
    assert rows["r1"]["project_id"] == PROJECT
    assert rows["r2"]["received"] and rows["r2"]["admitted"] and not rows["r2"]["result"]
    assert rows["r3"] == {**rows["r3"], "received": False, "admitted": False, "stage": None}


def test_status_exits_zero_when_every_run_has_its_result(lens: FakeLens) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "done", result_ref="sha256:" + "e" * 64)
    result = runner.invoke(app, ["lens", "status", "acme"])
    assert result.exit_code == 0, result.output
    assert "r1" in result.stdout


def test_a_run_with_its_result_is_not_launched_again(lens: FakeLens, daemon: FakeDocker) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "done", result_ref="sha256:" + "e" * 64)
    result = runner.invoke(app, ["lens", "run", "acme", "r1"])
    assert result.exit_code == 0, result.output
    assert lens.relaunches == [] and daemon.calls == []


def test_a_run_with_a_live_job_is_waited_for_not_launched(lens: FakeLens) -> None:
    lens.put(f"{PROJECT}/r1", "running", attempts=1)
    lens.launched(f"{PROJECT}/r1")
    lens.polls_to_done = 2
    fake = FakeDocker(lens)
    with lens.client() as http:
        outcome = ensure(LensApi(http), YieldRun("r1", DECLARATION), Manual(fake), no_wait())
    assert outcome.ok and outcome.action == "waited"
    assert lens.relaunches == [] and fake.calls == []


def test_an_accepted_run_is_relaunched_through_lens_first(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    result = runner.invoke(app, ["lens", "run", "acme", "r1", "--interval", "0"])
    assert result.exit_code == 0, result.output
    assert lens.relaunches == [f"{PROJECT}/r1"]
    assert daemon.calls == []
    assert "sha256:" + "e" * 64 in result.stdout


def test_when_lens_cannot_launch_the_job_is_started_by_hand_with_its_environment(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    lens.relaunch_answer = "launch_failed"
    result = runner.invoke(app, ["lens", "run", "acme", "r1", "--interval", "0", "--json"])
    assert result.exit_code == 0, result.output
    assert SECRET_VALUE not in result.output
    run = next(call for call in daemon.calls if call[1] == "run")
    assert run[-3:] == ["run", PROJECT, "r1"]
    assert run[-4] == "registry.local/lens-run@sha256:" + "a" * 64
    assert run[run.index("--name") + 1] == launcher.container_name(PROJECT, "r1")
    assert run[run.index("--network") + 1] == "lens"
    assert "--add-host" not in run
    assert not any(SECRET_VALUE in arg for arg in run)
    assert sorted(daemon.env_seen) == sorted(
        [
            "LENS_STORE_BACKEND=s3",
            "LENS_MODEL_READER_CREDENTIAL_REF=READER_KEY",
            f"READER_KEY={SECRET_VALUE}",
            "LENS_MODEL_READER_BASE_URL=http://host.docker.internal:8000/v1",
        ]
    )
    assert daemon.env_modes == [0o600]
    assert not daemon.env_files[0].exists()
    assert json.loads(result.stdout)[0]["action"] == "launched"


def test_a_failing_docker_run_never_shows_a_value_and_deletes_the_env_file(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    daemon.run_fails = True
    result = runner.invoke(app, ["lens", "run", "acme", "r1", "--manual"])
    assert result.exit_code == 1
    assert SECRET_VALUE not in result.output
    assert "***" in result.output
    assert not daemon.env_files[0].exists()


def test_a_live_container_under_the_runs_name_is_not_launched_twice(lens: FakeLens) -> None:
    lens.put(f"{PROJECT}/r1", "failed", attempts=1)
    fake = FakeDocker(lens)
    fake.containers[launcher.container_name(PROJECT, "r1")] = "running"
    api = launcher.read_api(fake, "lens-lens-api-1")
    launched = launcher.launch(fake, api, PROJECT, "r1")
    assert launched.launch == "already_running"
    assert not any(call[1] == "run" for call in fake.calls)


def test_a_dead_container_is_recorded_redacted_before_it_is_replaced(
    lens: FakeLens, tmp_path: Path
) -> None:
    lens.put(f"{PROJECT}/r1", "failed", attempts=1)
    fake = FakeDocker(lens)
    name = launcher.container_name(PROJECT, "r1")
    fake.containers[name] = "exited"
    api = launcher.read_api(fake, "lens-lens-api-1")
    launched = launcher.launch(fake, api, PROJECT, "r1", kills=tmp_path / "kills")
    assert launched.launch == "launched" and launched.replaced == 137
    assert [call[1] for call in fake.calls[-4:]] == ["inspect", "logs", "rm", "run"]
    assert launched.record is not None
    assert launched.record.stat().st_mode & 0o777 == 0o600
    record = json.loads(launched.record.read_text())
    assert record["exit_code"] == 137 and record["container"] == name
    assert record["error"] == "lens-run killed by the runtime: exited 137"
    assert "***" in record["log_tail"] and SECRET_VALUE not in launched.record.read_text()


def test_a_dead_container_is_not_removed_without_a_place_for_its_record(lens: FakeLens) -> None:
    fake = FakeDocker(lens)
    fake.containers[launcher.container_name(PROJECT, "r1")] = "exited"
    api = launcher.read_api(fake, "lens-lens-api-1")
    with pytest.raises(launcher.LaunchRefused, match="nowhere to keep its record"):
        launcher.launch(fake, api, PROJECT, "r1")
    assert not any(call[1] == "rm" for call in fake.calls)


@pytest.mark.parametrize(("age", "expected"), [(30.0, "already_running"), (61.0, "launched")])
def test_a_container_created_and_never_started_is_dead_after_the_grace(
    lens: FakeLens, tmp_path: Path, age: float, expected: str
) -> None:
    lens.put(f"{PROJECT}/r1", "accepted")
    fake = FakeDocker(lens)
    fake.containers[launcher.container_name(PROJECT, "r1")] = "created"
    created = launcher._moment(fake.created)
    api = launcher.read_api(fake, "lens-lens-api-1")
    launched = launcher.launch(fake, api, PROJECT, "r1", kills=tmp_path, now=lambda: created + age)
    assert launched.launch == expected


def test_without_a_configured_network_the_job_goes_where_lens_api_would_put_it(
    lens: FakeLens,
) -> None:
    lens.put(f"{PROJECT}/r1", "accepted")
    fake = FakeDocker(lens)
    fake.api_env = [item for item in fake.api_env if not item.startswith("LENS_DOCKER_NETWORK")]
    api = launcher.read_api(fake, "lens-lens-api-1")
    assert api.network is None
    launcher.launch(fake, api, PROJECT, "r1")
    run = next(call for call in fake.calls if call[1] == "run")
    assert "--network" not in run


@pytest.mark.parametrize("status", [404, 409, 500])
def test_a_relaunch_lens_refuses_is_never_launched_by_hand(
    lens: FakeLens, daemon: FakeDocker, status: int
) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    lens.relaunch_status = status
    result = runner.invoke(app, ["lens", "run", "acme", "r1"])
    assert result.exit_code == 1
    assert "not admitted" in " ".join(result.stdout.split())
    assert daemon.calls == []


def test_a_lens_without_the_relaunch_route_falls_back_to_the_hand_launch(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    lens.relaunch_status = 405
    result = runner.invoke(app, ["lens", "run", "acme", "r1", "--interval", "0"])
    assert result.exit_code == 0, result.output
    assert any(call[1] == "run" for call in daemon.calls)


def test_a_run_lens_never_received_is_refused_without_launching(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1")
    result = runner.invoke(app, ["lens", "run", "acme", "r1"])
    assert result.exit_code == 1
    assert "never received" in " ".join(result.stdout.split())
    assert daemon.calls == []


def test_a_run_whose_yield_run_failed_is_not_launched(lens: FakeLens, daemon: FakeDocker) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "upstream_failed")
    result = runner.invoke(app, ["lens", "run", "acme", "r1"])
    assert result.exit_code == 1
    assert lens.relaunches == [] and daemon.calls == []


def test_a_relaunched_job_that_fails_reports_its_exit(lens: FakeLens) -> None:
    lens.put(f"{PROJECT}/r1", "failed", attempts=1)
    lens.fails = frozenset({f"{PROJECT}/r1"})
    with lens.client() as http:
        outcome = ensure(
            LensApi(http), YieldRun("r1", DECLARATION), Manual(FakeDocker(lens)), no_wait()
        )
    assert not outcome.ok
    assert "exit 3" in outcome.detail


def test_waiting_ends_at_the_bound(lens: FakeLens) -> None:
    lens.put(f"{PROJECT}/r1", "accepted")
    lens.polls_to_done = 1000
    ticks = iter(range(100))
    waiting = Waiting(timeout=5, interval=1, sleep=lambda _: None, clock=lambda: next(ticks))
    with lens.client() as http:
        outcome = ensure(
            LensApi(http), YieldRun("r1", DECLARATION), Manual(FakeDocker(lens)), waiting
        )
    assert not outcome.ok
    assert "no result within 5s" in outcome.detail


def test_no_fallback_leaves_a_failed_relaunch_failed(lens: FakeLens, daemon: FakeDocker) -> None:
    specs("r1")
    lens.put(f"{PROJECT}/r1", "accepted")
    lens.relaunch_answer = "launch_failed"
    result = runner.invoke(app, ["lens", "run", "acme", "r1", "--no-fallback"])
    assert result.exit_code == 1
    assert daemon.calls == []


def test_without_ids_every_run_of_the_client_is_brought_to_a_result(
    lens: FakeLens, daemon: FakeDocker
) -> None:
    specs("r1", "r2")
    lens.put(f"{PROJECT}/r1", "done", result_ref="sha256:" + "e" * 64)
    lens.put(f"{PROJECT}/r2", "accepted")
    result = runner.invoke(app, ["lens", "run", "acme", "--interval", "0", "--json"])
    assert result.exit_code == 0, result.output
    actions = {row["yield_run"]: row["action"] for row in json.loads(result.stdout)}
    assert actions == {"r1": "done", "r2": "relaunched"}


def test_lens_unreachable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    specs("r1")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        commands,
        "connect",
        lambda url, timeout: httpx.Client(transport=httpx.MockTransport(down), base_url=url),
    )
    result = runner.invoke(app, ["lens", "status", "acme"])
    assert result.exit_code == 2
    assert "could not be reached" in result.output
