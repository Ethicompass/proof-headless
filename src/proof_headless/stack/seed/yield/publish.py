"""The HTTP the seed script speaks. Standard library only, and no opinions about what it publishes.

Separate from `seed.sh` because a here-doc of Python inside a shell loop is unreadable and
unquotable, and because a slim Python base carries no curl -- which the compose healthcheck already
discovered about itself.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 60.0


def request(url: str, *, body: bytes | None = None, content_type: str | None = None) -> bytes:
    headers = {"content-type": content_type} if content_type else {}
    call = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(call, timeout=TIMEOUT) as answer:
            return bytes(answer.read())
    except urllib.error.HTTPError as refused:
        detail = refused.read().decode(errors="replace")
        raise SystemExit(f"{url} answered {refused.code}: {detail}") from refused
    except OSError as unreachable:
        raise SystemExit(f"{url} could not be reached: {unreachable}") from unreachable


def names(base: str, kind: str) -> list[str]:
    return sorted(json.loads(request(f"{base}/{kind}")))


def main(argv: list[str]) -> int:
    base, action = argv[1].rstrip("/"), argv[2]

    if action == "healthz":
        request(f"{base}/healthz")
        return 0

    if action == "names":
        print(" ".join(names(base, argv[3])))
        return 0

    if action == "carries":
        return 0 if argv[4] in names(base, argv[3]) else 1

    if action == "raw":
        route, name, path = argv[3], argv[4], Path(argv[5])
        request(
            f"{base}{route}?name={name}",
            body=path.read_bytes(),
            content_type="application/yaml",
        )
        return 0

    if action == "wrapped":
        route, key, name, path = argv[3], argv[4], argv[5], Path(argv[6])
        wrapped = {"name": name, key: json.loads(path.read_text())}
        request(
            f"{base}{route}",
            body=json.dumps(wrapped).encode(),
            content_type="application/json",
        )
        return 0

    if action == "mapping":
        name, path = argv[3], Path(argv[4])
        answered = json.loads(
            request(
                f"{base}/mappings",
                body=json.dumps({"name": name, "mapping": json.loads(path.read_text())}).encode(),
                content_type="application/json",
            )
        )
        # Compute answers an unchanged mapping with the version it holds and `created: false`, a
        # changed one with the next version (issue #108).
        version, created = answered.get("version"), answered.get("created", True)
        print(f"v{version} {'published' if created else 'already published, unchanged'}")
        return 0

    if action == "catalogue":
        name, directory = argv[3], Path(argv[4])
        catalogue: dict[str, object] = {
            "name": name,
            "catalogue": json.loads((directory / "catalogue.json").read_text()),
        }
        sidecar = directory / "pairs.json"
        if sidecar.is_file():
            catalogue["pairs"] = json.loads(sidecar.read_text())
        # The grounding sidecar, when a catalogue needs one. Most do not: a phrasing carrying
        # `{premise}` already says the strategy needs a base, and that is read rather than declared.
        grounding = directory / "grounding.json"
        if grounding.is_file():
            catalogue["needs_base"] = json.loads(grounding.read_text())["needs_base"]
        # The delivery sidecar, when a catalogue conducts any strategy as a conversation. Absent
        # means every strategy is one static turn, which is what every catalogue was.
        delivery = directory / "delivery.json"
        if delivery.is_file():
            catalogue["delivery"] = json.loads(delivery.read_text())
        answered = json.loads(
            request(
                f"{base}/catalogues",
                body=json.dumps(catalogue).encode(),
                content_type="application/json",
            )
        )
        # What the seed says about it: compute answers an unchanged catalogue with the version it
        # already holds and `created: false`, and a changed one with the next version.
        version, created = answered.get("version"), answered.get("created", True)
        print(f"v{version} {'published' if created else 'already published, unchanged'}")
        return 0

    raise SystemExit(f"unknown action: {action}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
