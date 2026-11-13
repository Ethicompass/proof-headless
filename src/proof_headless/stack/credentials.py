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
from pathlib import Path

from proof_headless import secrets

LENGTH = 32
ALPHABET = string.ascii_letters + string.digits


STORE_NAMES = (
    "YIELD_S3_ACCESS_KEY",
    "YIELD_S3_SECRET_KEY",
    "LENS_S3_ACCESS_KEY",
    "LENS_S3_SECRET_KEY",
)
"""Both stores' root user and password, which are also the S3 keys Yield and Lens sign with."""
SIGNING_SECRET = "LENS_WEBHOOK_SIGNING_SECRET"
NAMES = (*STORE_NAMES, SIGNING_SECRET)
"""Every credential the stack's compose file refers to and `up` generates when absent."""


def generate() -> str:
    """A fresh credential value."""
    return "".join(random.choice(ALPHABET) for _ in range(LENGTH))


def ensure(path: Path) -> list[str]:
    """Generate every stack credential `path` does not define, write each there (mode 600), and
    return the names generated. Never overwrites a name the file defines; `NAME=` with nothing
    after it defines nothing (`secrets.names`), so it is replaced.

    A store already started with the old development defaults needs no care: MinIO reads its
    root credentials from the environment at every start and keeps none in its volume. Measured
    on a DGX Spark with the stack's MinIO image: a volume started as user A, restarted as user
    B, let B in, refused A, and still held the bucket written as A. Compose recreates the stores
    and the services whose environment changed on the next `up`, all with the same new values.
    """
    present = secrets.names(path)
    generated = [name for name in NAMES if name not in present]
    for name in generated:
        secrets.write(path, name, generate())
    return generated
