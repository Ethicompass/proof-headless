"""The anomaly rules as tables, the detector's memory, and parsing what docker prints."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from proof_headless.docker import ServiceState
from proof_headless.runs import anomalies
from proof_headless.runs.anomalies import Detector, Kind
from proof_headless.runs.client import RunStatus
from proof_headless.runs.observe import (
    Container,
    JobContainers,
    LogLine,
    Observation,
    Service,
    parse_logs,
    parse_ps,
    parse_services,
)
from proof_headless.runs.state import Severity
from tests.fake_yield import status

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)


def run(phase: str, **fields: Any) -> RunStatus:
    return RunStatus.model_validate({"run_id": "r1", **status(phase, **fields)})


def at(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


@pytest.mark.parametrize(
    ("phase", "quiet_minutes", "expected"),
    [
        ("attacking", 5, False),
        ("attacking", 15, True),
        ("generating", 40, True),
        ("measuring", 20, True),
        ("complete", 90, False),
        ("failed", 90, False),
    ],
)
def test_stalled(phase: str, quiet_minutes: float, expected: bool) -> None:
    found = anomalies.stalled(run(phase), since=at(quiet_minutes), now=NOW)
    assert (found is not None) is expected
    if found is not None:
        assert found.kind is Kind.STALLED
        assert f"{quiet_minutes} min" in found.message


@pytest.mark.parametrize(
    ("liveness", "severity"),
    [("running", None), ("unknown", None), ("stalled", Severity.WARNING), ("dead", Severity.ERROR)],
)
def test_liveness(liveness: str, severity: Severity | None) -> None:
    found = anomalies.liveness(run("attacking", liveness=liveness, detail="exited 137"))
    assert (found.severity if found else None) == severity


@pytest.mark.parametrize(
    ("before", "failed", "tally"), [(0, 0, None), (2, 2, None), (2, 5, 5), (0, 1, 1), (5, 3, None)]
)
def test_failed_traces(before: int, failed: int, tally: int | None) -> None:
    found = anomalies.failed_traces(before, run("attacking", closed=8, failed=failed))
    assert (found.tally if found else None) == tally


DRIVER = Container("yield-driver-r1", "running")
MEASURE = Container("yield-meas-r1", "running")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('HTTP Request: POST http://chat:8000/v1/chat/completions "HTTP/1.1 429 Too Many"', "rl"),
        ("openai.RateLimitError: Error code: 429", Kind.RATE_LIMITED),
        ("httpx.ReadTimeout: timed out", Kind.TIMEOUT),
        ("TargetServerError: 502 from the target", Kind.TARGET_ERROR),
        ("connection refused", Kind.TARGET_ERROR),
        ('HTTP Request: POST http://chat:8000/v1/chat/completions "HTTP/1.1 200 OK"', None),
        ("closed 50 units", None),
    ],
)
def test_log_problems(text: str, kind: Kind | str | None) -> None:
    found = anomalies.log_problems(DRIVER, [LogLine(NOW, text)])
    kinds = {a.kind for a in found}
    if kind is None:
        assert kinds == set()
    else:
        assert (Kind.RATE_LIMITED if kind == "rl" else kind) in kinds
        assert all(a.run_id == "r1" for a in found)


def test_log_problems_tally_grows_from_what_was_seen() -> None:
    lines = [LogLine(NOW, "RateLimitError")] * 3
    (found,) = anomalies.log_problems(DRIVER, lines, seen={Kind.RATE_LIMITED: 4})
    assert found.tally == 7
    assert "3 new" in found.message


CALL = 'HTTP Request: POST http://judge/v1/chat/completions "HTTP/1.1 200 OK"'


@pytest.mark.parametrize(
    ("container", "lines", "expected"),
    [
        (MEASURE, [LogLine(at(2), CALL)], False),
        (MEASURE, [LogLine(at(7), CALL), LogLine(at(1), "loading the dataset")], True),
        (MEASURE, [LogLine(at(30), "loading the dataset")], False),
        (MEASURE, [], False),
        (DRIVER, [LogLine(at(30), CALL)], False),
        (Container("yield-meas-r1", "exited", 0), [LogLine(at(30), CALL)], False),
    ],
)
def test_measure_idle(container: Container, lines: list[LogLine], expected: bool) -> None:
    found = anomalies.measure_idle(container, lines, now=NOW)
    assert (found is not None) is expected


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        (Container("yield-driver-r1", "running"), False),
        (Container("yield-driver-r1", "exited", 0), False),
        (Container("yield-driver-r1", "exited", 1), True),
        (Container("yield-meas-r1", "exited", 137), True),
        (Container("yield-meas-r1", "dead"), True),
        (Container("yield-meas-r1", "restarting"), True),
    ],
)
def test_container_exited(container: Container, expected: bool) -> None:
    found = anomalies.container_exited(container)
    assert (found is not None) is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ServiceState("chat", "running", "healthy"), None),
        (ServiceState("chat", "running", "starting"), None),
        (ServiceState("chat", "running", "unhealthy"), "health check fails"),
        (ServiceState("chat", "exited", ""), "is exited"),
        (ServiceState("chat", "restarting", ""), "is restarting"),
        (None, "no container"),
    ],
)
def test_model_down(state: ServiceState | None, expected: str | None) -> None:
    found = anomalies.model_down("chat", state)
    if expected is None:
        assert found is None
    else:
        assert found is not None
        assert expected in found.message


def test_the_detector_reports_a_holding_condition_once_and_again_after_it_clears() -> None:
    detector = Detector()
    down = Observation(at=NOW, models={"chat": ServiceState("chat", "exited", "")})
    up = Observation(at=NOW, models={"chat": ServiceState("chat", "running", "healthy")})
    assert [a.kind for a in detector.examine(down)] == [Kind.MODEL_DOWN]
    assert detector.examine(down) == []
    assert detector.examine(up) == []
    assert [a.kind for a in detector.examine(down)] == [Kind.MODEL_DOWN]


def test_the_detector_reads_each_log_line_once() -> None:
    detector = Detector()
    first = [LogLine(at(3), "RateLimitError"), LogLine(at(2), "fine")]
    second = [*first, LogLine(at(1), "RateLimitError"), LogLine(at(0.5), "RateLimitError")]

    def look(lines: list[LogLine]) -> list[anomalies.Anomaly]:
        return detector.examine(
            Observation(at=NOW, containers={DRIVER.name: DRIVER}, logs={DRIVER.name: lines})
        )

    (one,) = look(first)
    assert one.tally == 1
    assert look(first) == []
    (two,) = look(second)
    assert two.tally == 3
    assert "2 new" in two.message


def test_the_detector_measures_a_stall_from_the_last_change() -> None:
    detector = Detector(stall_after=timedelta(minutes=10))
    stuck = run("attacking", closed=4)

    def look(minute: int, status: RunStatus) -> list[Kind]:
        observed = Observation(at=NOW + timedelta(minutes=minute), runs={"r1": status})
        return [a.kind for a in detector.examine(observed)]

    assert look(0, stuck) == []
    assert look(9, stuck) == []
    assert look(10, stuck) == [Kind.STALLED]
    assert look(15, stuck) == []
    assert look(16, run("attacking", closed=5)) == []
    assert look(27, run("attacking", closed=5)) == [Kind.STALLED]


def test_parse_logs_reads_docker_timestamps() -> None:
    lines = parse_logs(
        "2026-11-12T02:59:00.123456789Z first line\nno stamp here\n2026-11-12T02:59:30Z second\n"
    )
    assert lines[0] == LogLine(datetime(2026, 11, 12, 2, 59, 0, 123456, tzinfo=UTC), "first line")
    assert lines[1] == LogLine(None, "no stamp here")
    assert lines[2].at == datetime(2026, 11, 12, 2, 59, 30, tzinfo=UTC)


def test_parse_ps_reads_exit_codes_and_roles() -> None:
    rows = [
        {"Names": "yield-driver-r1", "State": "exited", "Status": "Exited (0) 2 minutes ago"},
        {"Names": "yield-meas-r1", "State": "exited", "Status": "Exited (137) 1 minute ago"},
        {"Names": "yield-meas-r2", "State": "running", "Status": "Up 3 minutes"},
    ]
    found = parse_ps("\n".join(json.dumps(r) for r in rows))
    assert found["yield-driver-r1"].exit_code == 0
    assert found["yield-meas-r1"].exit_code == 137
    assert found["yield-meas-r1"].role == "measure"
    assert found["yield-meas-r2"].run_id == "r2"
    assert found["yield-meas-r2"].exit_code is None


def test_job_containers_asks_docker_by_label_and_reads_both_streams() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[1] == "ps":
            row = {"Names": "yield-driver-r1", "State": "running", "Status": "Up"}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(row), stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout="2026-11-12T02:59:00Z out\n", stderr="2026-11-12T02:59:01Z err\n"
        )

    containers = JobContainers(runner=runner)
    assert list(containers.find()) == ["yield-driver-r1"]
    assert [line.text for line in containers.logs("yield-driver-r1")] == ["out", "err"]
    assert "label=yield.role" in calls[0]


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (Service("yield-api", "running", "healthy"), None),
        (Service("brain-registry", "running", ""), None),
        (Service("yield-seed", "exited", exit_code=0), None),
        (Service("lens-api", "running", "unhealthy"), "the health check fails"),
        (Service("lens-api", "exited", exit_code=1), "is exited with code 1"),
        (Service("yield-valkey", "restarting"), "is restarting"),
    ],
)
def test_stack_down(service: Service, expected: str | None) -> None:
    found = anomalies.stack_down(service)
    assert (found.message if found else None) == expected


def test_an_empty_stack_is_down_and_an_unasked_one_is_not() -> None:
    assert anomalies.stack_empty({}) is not None
    assert anomalies.stack_empty(None) is None


def test_parse_services_reads_the_compose_service_and_health() -> None:
    rows = [
        {
            "Names": "proof-stack-lens-api-1",
            "Labels": "com.docker.compose.project=proof-stack,com.docker.compose.service=lens-api",
            "State": "running",
            "Status": "Up 5 minutes (unhealthy)",
        },
        {
            "Names": "proof-stack-yield-seed-1",
            "Labels": "com.docker.compose.service=yield-seed",
            "State": "exited",
            "Status": "Exited (0) 1 hour ago",
        },
    ]
    found = parse_services("\n".join(json.dumps(r) for r in rows))
    assert found["lens-api"] == Service("lens-api", "running", "unhealthy")
    assert found["yield-seed"].exit_code == 0


def test_a_generation_job_is_one_of_the_run_s_containers() -> None:
    generate = Container("yield-gen-r1", "running")
    assert (generate.role, generate.run_id) == ("generate", "r1")
    (found,) = anomalies.log_problems(generate, [LogLine(NOW, "RateLimitError")])
    assert "generate" in found.message


def test_log_lines_older_than_the_detector_s_start_are_history() -> None:
    detector = Detector(since=at(5))
    lines = [LogLine(at(30), "RateLimitError"), LogLine(at(1), "RateLimitError")]
    (found,) = detector.examine(
        Observation(at=NOW, containers={DRIVER.name: DRIVER}, logs={DRIVER.name: lines})
    )
    assert found.tally == 1
