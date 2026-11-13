"""Publish Lens's seed assets through its API and check each lands on the digest compose pins.

Runs in a bare `python:3.12-slim`, so standard library only. Lens's own compose posts the three
files with curl and trusts the digests it pins; here the answer is checked, because a vendored
file that drifted from the pin would leave `LENS_DEFAULT_*_REF` naming an asset the store does
not hold, and every run would fail on it instead of this seed. Idempotent: Lens answers a file it
already holds with the version and digest it has.

    python seed.py http://lens-api:8080 'kind|name|file|digest' ...
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def publish(base: str, kind: str, name: str, file: str) -> str:
    request = urllib.request.Request(
        f"{base}/assets/{kind}/{name}",
        data=(HERE / file).read_bytes(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return str(json.loads(answer.read())["digest"])
    except urllib.error.HTTPError as refused:
        detail = refused.read().decode(errors="replace")
        raise SystemExit(f"{kind}/{name}: lens-api answered {refused.code}: {detail}") from None


def main(argv: list[str]) -> int:
    base = argv[1].rstrip("/")
    for spec in argv[2:]:
        kind, name, file, expected = spec.split("|")
        digest = publish(base, kind, name, file)
        if digest != expected:
            print(f"{kind}/{name}: stored as {digest}, compose pins {expected}", file=sys.stderr)
            return 1
        print(f"{kind}/{name}: {digest}")
    print("lens seed assets published")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
