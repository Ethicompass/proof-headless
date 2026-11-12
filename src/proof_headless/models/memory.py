"""Whether the host has the memory a manifest's models will claim, before any of them starts.

vLLM sizes its KV cache from `gpu_memory_utilization` times the device's memory and fails at
start, minutes in, when it cannot have it. On a unified-memory host the device's memory is the
host's, so the check reads `/proc/meminfo`: the models not yet running must fit in what is
available now. Models already running hold their share already, so they are not counted twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proof_headless.models.manifest import Manifest

GIB = 1024**3
MEMINFO = Path("/proc/meminfo")


@dataclass(frozen=True)
class HostMemory:
    """Total and currently available memory, in GiB."""

    total: float
    available: float


def read_host_memory(meminfo: Path = MEMINFO) -> HostMemory | None:
    """The host's memory from `/proc/meminfo`, or `None` where there is none (macOS, tests)."""
    if not meminfo.exists():
        return None
    fields: dict[str, int] = {}
    for line in meminfo.read_text().splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0]) * 1024
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return HostMemory(total=fields["MemTotal"] / GIB, available=fields["MemAvailable"] / GIB)


@dataclass(frozen=True)
class Claim:
    """One model's share, in GiB, and whether it is already held."""

    name: str
    share: float
    gib: float
    running: bool


@dataclass(frozen=True)
class Preflight:
    """The arithmetic behind starting a manifest's models, and its verdict."""

    claims: tuple[Claim, ...]
    available: float

    @property
    def needed(self) -> float:
        """GiB the models not yet running will claim."""
        return sum(claim.gib for claim in self.claims if not claim.running)

    @property
    def fits(self) -> bool:
        return self.needed <= self.available


def preflight(manifest: Manifest, host: HostMemory, running: frozenset[str]) -> Preflight:
    """What starting every model of `manifest` not in `running` would claim against `host`."""
    claims = tuple(
        Claim(
            name=model.name,
            share=model.gpu_memory_utilization,
            gib=model.gpu_memory_utilization * host.total,
            running=model.name in running,
        )
        for model in manifest.models
    )
    return Preflight(claims=claims, available=host.available)
