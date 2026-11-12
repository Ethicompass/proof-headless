"""Where the appliance keeps what it writes, and where the operator keeps what it must not.

Two roots, kept apart on purpose. `home()` holds what `proof` renders and records: compose files,
state, receipts. `secrets_file()` holds credentials, outside any directory `proof` writes, so no
render, export or bundle can sweep one up by accident (ADR 0001). Both move with environment
variables, which is how a test or a second appliance on one host gets its own.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_VARIABLE = "PROOF_HOME"
SECRETS_VARIABLE = "PROOF_SECRETS_FILE"


def home() -> Path:
    """The appliance's working directory: `$PROOF_HOME`, else `~/.proof`."""
    return Path(os.environ.get(HOME_VARIABLE) or Path.home() / ".proof")


def secrets_file() -> Path:
    """The operator's secrets file: `$PROOF_SECRETS_FILE`, else `~/.config/proof/secrets.env`."""
    return Path(
        os.environ.get(SECRETS_VARIABLE) or Path.home() / ".config" / "proof" / "secrets.env"
    )


def default_manifest() -> Path:
    """The models manifest `proof models` reads when none is named."""
    return home() / "models.yaml"


def default_lock() -> Path:
    """The image lock `proof stack` reads and `proof stack pin` writes when none is named."""
    return home() / "appliance.lock"
