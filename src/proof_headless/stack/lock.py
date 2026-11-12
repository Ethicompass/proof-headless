"""`appliance.lock`: every image the stack runs, by reference and by digest.

An engagement's evidence names the lock it ran with (ADR 0001), so the lock is what says which
Yield and which Lens measured it. A tag moves and a digest does not: the stack runs
`reference@digest`, and the tag stays beside the digest only so a person can read where it came
from. The images a service launches per job (the driver, compute's jobs, `lens-run`) are in the
lock as well, because a job run from a moved tag would measure with code nobody pinned.
"""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

IMAGES = (
    "yield-api",
    "yield-driver",
    "yield-compute",
    "lens-api",
    "lens-run",
    "minio",
    "valkey",
    "registry",
    "python",
)
"""Every image the stack needs. `python` runs the seeds and the declaration source."""

Channel = Literal["develop", "release"]

HEADER = (
    "# The images `proof stack` runs, by digest. Written by `proof stack pin`; review a change\n"
    "# to it like a change of code, because it is one.\n"
)


class LockInvalid(ValueError):
    """The lock file is missing, unreadable, or does not hold every image by digest."""


class Image(BaseModel):
    """One image: the reference it was resolved from and the digest it resolved to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1)
    """`repository:tag`, as the channel names it. Never carries a digest of its own."""
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    """The digest of the image index (or of the single manifest, for a one-platform image)."""
    platforms: tuple[str, ...] = ()
    """`os/architecture` of each image the index carries; empty when the registry did not say."""

    @field_validator("reference")
    @classmethod
    def _no_digest(cls, reference: str) -> str:
        if "@" in reference:
            raise ValueError(f"{reference!r} carries a digest; put it in `digest` instead")
        return reference

    @property
    def pinned(self) -> str:
        """What compose and the dispatchers are given: the tag for the reader, the digest for
        docker, which resolves `name:tag@digest` by the digest alone."""
        return f"{self.reference}@{self.digest}"

    def runs_on(self, platform: str) -> bool:
        """Whether the image has a build for `platform`; unknown platforms are given the benefit
        of the doubt, because a single-manifest image does not say. The variant is not compared:
        `linux/arm64/v8` runs on a host docker calls `linux/arm64`."""
        wanted = platform.split("/")[:2]
        return not self.platforms or any(p.split("/")[:2] == wanted for p in self.platforms)


class Lock(BaseModel):
    """The whole lock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    channel: Channel
    resolved_at: datetime
    images: dict[str, Image]

    @model_validator(mode="after")
    def _every_image(self) -> Lock:
        missing = sorted(set(IMAGES) - set(self.images))
        unknown = sorted(set(self.images) - set(IMAGES))
        if missing:
            raise ValueError(f"the lock has no digest for {missing}; run `proof stack pin`")
        if unknown:
            raise ValueError(f"the lock names images the stack does not run: {unknown}")
        return self

    def image(self, name: str) -> Image:
        """The image called `name`."""
        return self.images[name]


def load(path: Path) -> Lock:
    """Read and validate a lock, or raise `LockInvalid` saying why."""
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise LockInvalid(
            f"{path} does not exist. Copy appliance.lock from the repository there, "
            "or run `proof stack pin`."
        ) from None
    except (OSError, yaml.YAMLError) as error:
        raise LockInvalid(f"{path}: {error}") from None
    try:
        return Lock.model_validate(raw)
    except ValidationError as error:
        raise LockInvalid(f"{path}: {error}") from None


def dump(lock: Lock) -> str:
    """The lock as the YAML `pin` writes: a header, then images in a stable order."""
    document = {
        "version": lock.version,
        "channel": lock.channel,
        "resolved_at": lock.resolved_at.isoformat(timespec="seconds"),
        "images": {
            name: {
                "reference": lock.images[name].reference,
                "digest": lock.images[name].digest,
                "platforms": list(lock.images[name].platforms),
            }
            for name in IMAGES
        },
    }
    return HEADER + yaml.safe_dump(document, sort_keys=False)


def channels() -> dict[str, dict[str, str]]:
    """The references each channel resolves, with the common images merged into both."""
    text = resources.files("proof_headless.stack").joinpath("channels.yaml").read_text()
    raw: dict[str, dict[str, str]] = yaml.safe_load(text)
    common = raw.get("common", {})
    return {name: {**raw[name], **common} for name in ("develop", "release")}
