"""`proof stack pin`: resolve each channel reference to the digest its registry serves today.

Resolution goes through `docker buildx imagetools inspect`, which asks the registry directly (no
daemon, no pull) with the credentials `docker login` already holds. Yield's and Lens's images are
private on ghcr.io, and an operator who can pull them has logged in for that already, so the lock
is resolved with the same access the stack is pulled with and `proof` handles no registry token.
The resolver is a function from a reference to a `Resolved`, so a test replaces the registry.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from proof_headless.docker import Runner, run
from proof_headless.stack.lock import IMAGES, Channel, Image, Lock, channels


@dataclass(frozen=True)
class Resolved:
    """What the registry answered for one reference."""

    digest: str
    platforms: tuple[str, ...]


class ResolveFailed(RuntimeError):
    """The registry did not resolve a reference: absent, private without a login, or offline."""


Resolver = Callable[[str], Resolved]


def _platform(entry: Mapping[str, Any]) -> str | None:
    os_name, arch = entry.get("os"), entry.get("architecture")
    if not os_name or not arch or "unknown" in (os_name, arch):
        return None
    variant = entry.get("variant")
    return f"{os_name}/{arch}" + (f"/{variant}" if variant else "")


def platforms_of(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """`os/architecture` of each image in an index. Attestation manifests, which buildx lists as
    `unknown/unknown`, are not platforms an image runs on."""
    found: list[str] = []
    entries = manifest.get("manifests")
    for entry in entries if isinstance(entries, list) else []:
        name = _platform(entry.get("platform") or {})
        if name is not None and name not in found:
            found.append(name)
    return tuple(found)


def imagetools(runner: Runner = run) -> Resolver:
    """A resolver backed by `docker buildx imagetools inspect`.

    An index lists its platforms. A single-platform image does not, so its config is read for
    the one it has: the released images are amd64 only, and the lock has to say so for `up` to
    refuse them on an arm64 host instead of letting them die with `exec format error`.
    """

    def inspect(reference: str, template: str) -> Any:
        args = ["docker", "buildx", "imagetools", "inspect", reference, "--format", template]
        result = runner(args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise ResolveFailed(f"{reference}: {detail or 'imagetools failed'}")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise ResolveFailed(f"{reference}: unreadable answer ({error})") from None

    def resolve(reference: str) -> Resolved:
        manifest = inspect(reference, "{{json .Manifest}}")
        if not isinstance(manifest, dict) or "digest" not in manifest:
            raise ResolveFailed(f"{reference}: the registry answered no digest")
        platforms = platforms_of(manifest)
        if "manifests" not in manifest:
            config = inspect(reference, "{{json .Image}}")
            single = _platform(config) if isinstance(config, dict) else None
            platforms = (single,) if single else ()
        return Resolved(digest=str(manifest["digest"]), platforms=platforms)

    return resolve


def pin(
    channel: Channel,
    resolve: Resolver,
    overrides: Mapping[str, str] | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Lock:
    """A lock for `channel`, every reference resolved now. `overrides` replaces a channel's
    reference for one image, such as a release candidate's tag; an unknown name is refused."""
    references = dict(channels()[channel])
    unknown = sorted(set(overrides or {}) - set(IMAGES))
    if unknown:
        raise KeyError(f"no image {unknown}; the lock holds {list(IMAGES)}")
    references.update(overrides or {})
    images: dict[str, Image] = {}
    for name in IMAGES:
        resolved = resolve(references[name])
        images[name] = Image(
            reference=references[name], digest=resolved.digest, platforms=resolved.platforms
        )
    return Lock(channel=channel, resolved_at=now(), images=images)
