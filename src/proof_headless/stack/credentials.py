"""The stack's own credentials: generated once into the secrets file, never defaulted.

The stores' root credentials, the S3 keys Yield and Lens sign with, and Lens's webhook signing
secret used to fall back to each repository's development defaults (`yieldadmin`), written into
the rendered compose file. A well-known credential is no credential, and it also kept a run's
traces out of the evidence package: set in the secrets file so `proof evidence` could read the
store, the default's value sat in the compose file the package carries, and the secret scan
refused the package, rightly. So the first `proof stack up` generates each one the secrets file
does not define, and the compose file only refers to them by name.

Values are 32 letters and digits: far past the scan's 8-character floor, and free of what would
need quoting where they land (a dotenv line, compose's `$` interpolation, the `mc alias set`
shell line that creates the buckets, where a leading `-` would read as an option).
"""

from __future__ import annotations

import secrets as random
import string
from dataclasses import dataclass
from pathlib import Path

from proof_headless import secrets
from proof_headless.docker import ComposeFailed, Runner, docker

LENGTH = 32
ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class Store:
    """One MinIO store: the volume that keeps its data, and the names of its root credentials."""

    volume: str
    names: tuple[str, ...]


STORES = (
    Store("yield_minio", ("YIELD_S3_ACCESS_KEY", "YIELD_S3_SECRET_KEY")),
    Store("lens_minio", ("LENS_S3_ACCESS_KEY", "LENS_S3_SECRET_KEY")),
)
SIGNING_SECRET = "LENS_WEBHOOK_SIGNING_SECRET"
NAMES = (*(name for store in STORES for name in store.names), SIGNING_SECRET)
"""Every credential the stack's compose file refers to and `up` generates when absent."""


def generate() -> str:
    """A fresh credential value."""
    return "".join(random.choice(ALPHABET) for _ in range(LENGTH))


class StoreInitialised(RuntimeError):
    """A store's volume exists, so its data was written under credentials the secrets file does
    not hold: new ones would lock the stack out of it."""

    def __init__(self, store: Store, volume: str, missing: list[str]) -> None:
        self.store = store
        self.volume = volume
        self.missing = missing
        super().__init__(
            f"the volume {volume} exists, so its store was started with credentials the secrets "
            f"file does not hold ({missing}). Set them to the values the store was started with "
            f"(`proof secrets set NAME`), or remove the store with `docker volume rm {volume}` "
            "after `proof stack down`, which deletes its data."
        )


def _exists(runner: Runner, volume: str) -> bool:
    try:
        docker(runner, "volume", "inspect", volume)
    except ComposeFailed:
        return False
    return True


def ensure(path: Path, project: str, runner: Runner) -> list[str]:
    """Generate every stack credential `path` does not define, write each there (mode 600), and
    return the names generated. Never overwrites a name the file defines.

    Refuses, writing nothing, when a store whose credentials are missing already has a volume:
    a store started before this change was initialised with the development defaults, and
    generating new ones would leave Yield or Lens unable to read it.
    """
    present = secrets.names(path)
    for store in STORES:
        missing = [name for name in store.names if name not in present]
        volume = f"{project}_{store.volume}"
        if missing and _exists(runner, volume):
            raise StoreInitialised(store, volume, missing)
    generated = [name for name in NAMES if name not in present]
    for name in generated:
        secrets.write(path, name, generate())
    return generated
