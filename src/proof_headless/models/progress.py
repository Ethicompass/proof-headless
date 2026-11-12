"""How far a model's first start has got, read from the host's Hugging Face cache.

While vLLM downloads weights it logs nothing after "Initializing model", so a log tail looks
frozen for the half hour a 24 GB download takes at 10 MB/s. The bytes landing in the model's
cache directory are the one sign the host can read without asking the container, and a rate
beside them tells a download that is moving from one that stalled.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from proof_headless.models.manifest import Manifest, ModelEntry


def cache_dir(manifest: Manifest, model: ModelEntry) -> Path | None:
    """The host directory `huggingface_hub` downloads `model` into, or `None` when `hf_id` is not
    an `org/name` repository (a path to weights mounted from the host downloads nothing).

    The container's `HF_HOME` is the manifest's `hf_cache`, and the hub keeps each repository
    under `hub/models--<org>--<name>`.
    """
    parts = model.hf_id.split("/")
    if len(parts) != 2 or not all(parts) or model.hf_id.startswith((".", "~")):
        return None
    return manifest.hf_cache.expanduser() / "hub" / f"models--{parts[0]}--{parts[1]}"


def directory_bytes(path: Path) -> int | None:
    """Bytes of the regular files under `path`, or `None` when it does not exist or cannot be
    read. Symlinks are skipped: the hub's snapshots link to the blobs already counted. A file
    that vanishes mid-walk (a `.incomplete` blob renamed as it finishes) is skipped too."""
    if not path.is_dir():
        return None
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                info = (Path(root) / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def human_bytes(count: float) -> str:
    """`count` bytes in binary units with one decimal, as a status line shows them."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024:
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TiB"


@dataclass
class CacheProgress:
    """Successive readings of one cache directory, and the rate between the last two.

    The rate is over the last reading only, so a stall shows as 0 B/s on the next poll instead of
    hiding behind an average of the minutes before it.
    """

    path: Path | None
    clock: Callable[[], float]
    measure: Callable[[Path], int | None] = directory_bytes
    last: tuple[float, int] | None = field(default=None, init=False)
    rate: float | None = field(default=None, init=False)

    def sample(self) -> int | None:
        """Read the directory again; the bytes in it, or `None` when there is nothing to read."""
        if self.path is None:
            return None
        size = self.measure(self.path)
        if size is None:
            return None
        now = self.clock()
        if self.last is not None and now > self.last[0]:
            self.rate = max(0, size - self.last[1]) / (now - self.last[0])
        self.last = (now, size)
        return size

    def describe(self) -> str:
        """The latest reading as a status line fragment, or an empty string before there is one."""
        if self.last is None:
            return ""
        text = f"{human_bytes(self.last[1])} in cache"
        if self.rate is not None:
            text += f", {human_bytes(self.rate)}/s"
        return text
