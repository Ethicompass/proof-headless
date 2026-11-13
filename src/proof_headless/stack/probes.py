"""The stack's health, as `proof doctor --stack` probes it: each service on its loopback port.

The probes ask what a run will ask first. Yield's API and compute and Lens's API answer
`/healthz`; a MinIO answers its liveness route; Valkey answers `PING` with `PONG`; the registry
answers `/v2/` (200, or 401 for one that wants a login), which is what a brain pull opens with. A
container that reports `healthy` to docker has passed its own check from the inside; these prove
the port the operator and vitruvio use from the host answers too.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from proof_headless.models.probes import Result
from proof_headless.stack.render import PORTS

Connect = Callable[[str, float], httpx.Client]
"""A client for a base URL; a test replaces it with one on a mock transport."""


def client_for(base_url: str, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout)


@dataclass(frozen=True)
class HttpProbe:
    service: str
    path: str
    accept: frozenset[int] = frozenset({200})


HTTP_PROBES = (
    HttpProbe("yield-api", "/healthz"),
    HttpProbe("yield-compute", "/healthz"),
    HttpProbe("lens-api", "/healthz"),
    HttpProbe("yield-minio", "/minio/health/live"),
    HttpProbe("lens-minio", "/minio/health/live"),
    HttpProbe("brain-registry", "/v2/", frozenset({200, 401})),
)


def _http(probe: HttpProbe, host: str, timeout: float, connect: Connect) -> Result:
    base = f"http://{host}:{PORTS[probe.service]}"
    try:
        with connect(base, timeout) as client:
            status = client.get(probe.path).status_code
    except httpx.HTTPError as error:
        return Result(probe.service, probe.path, False, f"{base}: {type(error).__name__}")
    ok = status in probe.accept
    return Result(probe.service, probe.path, ok, f"{base}{probe.path} answered {status}")


Ping = Callable[[str, int, float], bytes]


def ping(host: str, port: int, timeout: float) -> bytes:
    """Send a RESP `PING` and return the first line of the answer."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(b"*1\r\n$4\r\nPING\r\n")
        return connection.recv(64).split(b"\r\n", 1)[0]


def _valkey(host: str, timeout: float, send: Ping) -> Result:
    port = PORTS["yield-valkey"]
    try:
        answer = send(host, port, timeout)
    except OSError as error:
        return Result("yield-valkey", "PING", False, f"{host}:{port}: {error}")
    ok = answer == b"+PONG"
    return Result(
        "yield-valkey", "PING", ok, f"{host}:{port} answered {answer.decode(errors='replace')}"
    )


def examine(
    host: str,
    timeout: float,
    *,
    connect: Connect | None = None,
    send: Ping | None = None,
) -> list[Result]:
    """Every stack probe, in the order a run depends on them."""
    results = [_http(probe, host, timeout, connect or client_for) for probe in HTTP_PROBES]
    results.insert(3, _valkey(host, timeout, send or ping))
    return results
