"""`proof evidence`: the package's layout, its checksums, and the scan that keeps secrets out."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import zstandard
from typer.testing import CliRunner

from proof_headless import paths
from proof_headless.cli import app
from proof_headless.evidence import commands, package
from proof_headless.evidence.s3 import Credentials, Store, sign
from tests.conftest import SPARK_MANIFEST

DECLARATION = "sha256:" + "d" * 64
PROJECT = "decl-" + "d" * 20
RESULT = "sha256:" + "e" * 64
TARGET_KEY = "tk-planted-0123456789"
STORE_SECRET = "store-secret-abcdef"
runner = CliRunner()

LIST = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
{contents}<IsTruncated>{truncated}</IsTruncated>{token}</ListBucketResult>"""


class World:
    """Yield, Lens, MinIO and the model servers on the appliance's loopback, in memory."""

    def __init__(self) -> None:
        self.objects = {
            "runs/r1/spec.json": b'{"run_id": "r1"}',
            "runs/r1/traces/a1/0.jsonl.zst": zstandard.compress(b'{"turn": "hello"}\n'),
            "runs/r1/dataset.json": b"[]",
            "runs/r1/probes.json": json.dumps({"digest": "a" * 64, "count": 1}).encode(),
            "blobs/" + "a" * 64: b'[{"probe": "p1"}]',
            "runs/r2/spec.json": b"{}",
        }
        self.manifest: dict[str, Any] = {"run_id": "r1", "target_declaration": DECLARATION}
        self.authorized: list[str] = []
        self.lens_body: bytes | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        port, path = request.url.port, request.url.path
        if port == 8080:
            if path == "/runs/r1":
                return httpx.Response(200, json={"run_id": "r1", "phase": "complete"})
            if path == "/runs/r1/result":
                return httpx.Response(200, json=self.manifest)
            return httpx.Response(404, json={"detail": "no run"})
        if port == 8090:
            if path == f"/projects/{PROJECT}/runs/r1":
                if self.lens_body is not None:
                    return httpx.Response(200, content=self.lens_body)
                return httpx.Response(200, json={"stage": "done", "result_ref": RESULT})
            if path == f"/artifacts/{RESULT}":
                return httpx.Response(200, json={"dimension_scores": {}})
            return httpx.Response(404)
        if port == 9000:
            self.authorized.append(request.headers.get("authorization", ""))
            return self.s3(request)
        return httpx.Response(200, json={"version": "0.11.0"})

    def s3(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/yield":
            prefix = request.url.params["prefix"]
            keys = sorted(key for key in self.objects if key.startswith(prefix))
            first = "continuation-token" not in request.url.params
            page = keys[:1] if first else keys[1:]
            contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in page)
            more = first and len(keys) > 1
            token = "<NextContinuationToken>t</NextContinuationToken>" if more else ""
            body = LIST.format(contents=contents, truncated=str(more).lower(), token=token)
            return httpx.Response(200, content=body.encode())
        key = path.removeprefix("/yield/")
        if key in self.objects:
            return httpx.Response(200, content=self.objects[key])
        return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")


def fake_docker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    args = list(args)
    if args[1] == "ps":
        stdout = "lens-lens-api-1\n"
    elif args[1] == "inspect":
        stdout = "registry.local/lens-api:1|sha256:" + "f" * 64
    else:
        stdout = json.dumps(["registry.local/lens-api@sha256:" + "c" * 64])
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    fake = World()
    monkeypatch.setattr(
        commands,
        "connect",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )
    monkeypatch.setattr(commands, "runner", fake_docker)
    home = paths.home()
    state = home / "bundles" / "acme"
    (state / "specs").mkdir(parents=True)
    (state / "receipt.json").write_text(json.dumps({"client": "acme", "complete": True}))
    for run_id in ("r1", "r2"):
        spec = {"run_id": run_id, "target_declaration": DECLARATION}
        (state / "specs" / f"{run_id}.json").write_text(json.dumps(spec))
    (home / "evidence").mkdir()
    (home / "evidence" / "doctor-20261112T000000Z.json").write_text('{"passed": true}')
    shutil.copyfile(SPARK_MANIFEST, home / "models.yaml")
    (home / "models").mkdir()
    (home / "models" / "compose.yaml").write_text("services: {}\n")
    secrets = paths.secrets_file()
    secrets.write_text(
        f"YIELD_S3_ACCESS_KEY=yieldadmin-key\nYIELD_S3_SECRET_KEY={STORE_SECRET}\n"
        f"ACME_TARGET_KEY={TARGET_KEY}\nEMPTY=\n"
    )
    secrets.chmod(0o600)
    return fake


def export(*extra: str) -> tuple[Any, Path]:
    destination = paths.home() / "out" / "acme"
    result = runner.invoke(app, ["evidence", "export", "acme", "--out", str(destination), *extra])
    return result, destination


def test_the_package_has_its_layout_and_verifies(world: World) -> None:
    result, package_dir = export("--json")
    assert result.exit_code == 0, result.output
    names = {path.as_posix() for path in package.files(package_dir)}
    assert {
        "README.md",
        "SHA256SUMS",
        "index.json",
        "bundle/receipt.json",
        "bundle/specs/r1.json",
        "appliance/models.yaml",
        "appliance/models-compose.yaml",
        "appliance/images.json",
        "appliance/vllm-versions.json",
        "doctor/doctor-20261112T000000Z.json",
        "runs/r1/yield/status.json",
        "runs/r1/yield/manifest.json",
        "runs/r1/yield/store/spec.json",
        "runs/r1/yield/store/traces/a1/0.jsonl.zst",
        "runs/r1/yield/store/dataset.json",
        "runs/r1/yield/probe-set.json",
        "runs/r1/lens/status.json",
        "runs/r1/lens/result.json",
    } <= names
    assert not any(name.startswith("runs/r2/yield/store/traces") for name in names)
    index = json.loads((package_dir / "index.json").read_text())
    r1 = next(run for run in index["runs"] if run["run_id"] == "r1")
    assert r1["lens"] == {"project_id": PROJECT, "run_id": "r1", "result": RESULT}
    assert r1["store_objects"] == 4 and r1["probe_set"]
    gaps = {gap["what"] for gap in index["gaps"]}
    assert "appliance.lock" in gaps and "r2: Yield manifest" in gaps
    readme = (package_dir / "README.md").read_text()
    assert "## Layout" in readme and "appliance.lock" in readme
    assert all(header.startswith("AWS4-HMAC-SHA256 ") for header in world.authorized)
    assert package.verify(package_dir) == []
    verified = runner.invoke(app, ["evidence", "verify", str(package_dir)])
    assert verified.exit_code == 0, verified.output
    assert STORE_SECRET not in result.output


def test_a_tampered_file_fails_verification(world: World) -> None:
    result, package_dir = export()
    assert result.exit_code == 0, result.output
    manifest = package_dir / "runs" / "r1" / "yield" / "manifest.json"
    manifest.write_text(manifest.read_text().replace("r1", "r9"))
    (package_dir / "doctor" / "doctor-20261112T000000Z.json").unlink()
    (package_dir / "extra.txt").write_text("slipped in")
    problems = {(p.path, p.problem) for p in package.verify(package_dir)}
    assert problems == {
        ("runs/r1/yield/manifest.json", "changed"),
        ("doctor/doctor-20261112T000000Z.json", "missing"),
        ("extra.txt", "unlisted"),
    }
    verified = runner.invoke(app, ["evidence", "verify", str(package_dir), "--json"])
    assert verified.exit_code == 1
    assert {"path": "runs/r1/yield/manifest.json", "problem": "changed"} in json.loads(
        verified.stdout
    )


def test_sha256sums_reads_as_sha256sum_writes_it(world: World) -> None:
    _, package_dir = export()
    line = (package_dir / "SHA256SUMS").read_text().splitlines()[0]
    digest, name = line.split("  ", 1)
    assert package.digest(package_dir / name) == digest


def test_a_planted_secret_refuses_the_package_and_names_only_file_and_secret(
    world: World,
) -> None:
    world.manifest["connector"] = {"note": f"Bearer {TARGET_KEY}"}
    result, package_dir = export()
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "runs/r1/yield/manifest.json holds the value of ACME_TARGET_KEY" in flat
    assert TARGET_KEY not in result.output
    assert not package_dir.exists()
    assert list(package_dir.parent.iterdir()) == []


def test_a_secret_written_json_escaped_is_caught(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "a.json").write_text(json.dumps({"v": 'pa"ss/w\\rd'}))
    found = package.scan(root, {"TOKEN": 'pa"ss/w\\rd', "OTHER": "absent-value"})
    assert [(leak.path, leak.secret) for leak in found.leaks] == [("a.json", "TOKEN")]


def test_without_store_credentials_the_traces_are_a_gap(world: World) -> None:
    paths.secrets_file().write_text(f"ACME_TARGET_KEY={TARGET_KEY}\n")
    result, package_dir = export("--json")
    assert result.exit_code == 0, result.output
    gaps = {gap["what"]: gap["why"] for gap in json.loads(result.stdout)["gaps"]}
    assert "YIELD_S3_SECRET_KEY" in gaps["r1: traces and dataset"]
    assert not (package_dir / "runs" / "r1" / "yield" / "store").exists()


def test_an_existing_destination_is_never_overwritten(world: World) -> None:
    destination = paths.home() / "out" / "acme"
    destination.mkdir(parents=True)
    result, _ = export()
    assert result.exit_code == 1
    assert "never overwritten" in result.output


def test_a_client_with_nothing_on_the_appliance_is_refused(world: World) -> None:
    result = runner.invoke(app, ["evidence", "export", "nobody", "--out", str(paths.home() / "x")])
    assert result.exit_code == 2


def test_an_exposed_secrets_file_is_refused(world: World) -> None:
    paths.secrets_file().chmod(0o644)
    result, _ = export()
    assert result.exit_code == 2


def test_requests_are_signed_as_botocore_signs_them() -> None:
    """Reference signatures computed with botocore's `S3SigV4Auth` for the same requests."""
    at = datetime(2026, 11, 12, 10, 20, 30)
    credentials = Credentials("AK", "SK")
    listing = sign(
        "GET",
        "http://127.0.0.1:9000/yield",
        {"list-type": "2", "prefix": "runs/r-1/", "continuation-token": "a b/+="},
        credentials,
        at,
    )
    assert listing["authorization"].endswith(
        "Signature=defe2e264ae878fb7fc7461cfa3e586ec0474ea1c18609880d46b271b532bae5"
    )
    item = sign(
        "GET", "http://127.0.0.1:9000/yield/runs/r-1/traces/a_1/0.jsonl.zst", {}, credentials, at
    )
    assert item["authorization"].endswith(
        "Signature=34124b71721bba163c20d8d8f6e3638c43fd12a76d3947b2fee2743114908d93"
    )
    unusual = sign("GET", "http://127.0.0.1:9000/yield/runs/a b+c/x.json", {}, credentials, at)
    assert unusual["authorization"].endswith(
        "Signature=2186e22328b64d587b2b3ed7c4a1e1b0c89b5a32d9bba78929e04711ce6435a9"
    )


def test_credentials_never_show_in_their_repr() -> None:
    assert "SK" not in repr(Credentials("AK-visible", "SK"))


def test_the_store_follows_continuation_tokens(world: World) -> None:
    with httpx.Client(transport=httpx.MockTransport(world.handler)) as http:
        store = Store(http, "http://127.0.0.1:9000", "yield", Credentials("a", "b"))
        assert list(store.keys("runs/r1/")) == sorted(k for k in world.objects if "r1" in k)


def test_a_secret_inside_a_compressed_trace_refuses_the_package(world: World) -> None:
    line = json.dumps({"turn": "x" * 70000 + f"the key is {TARGET_KEY}"}).encode()
    world.objects["runs/r1/traces/a1/0.jsonl.zst"] = zstandard.compress(line)
    result, package_dir = export()
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "runs/r1/yield/store/traces/a1/0.jsonl.zst holds the value of ACME_TARGET_KEY" in flat
    assert TARGET_KEY not in result.output
    assert not package_dir.exists()


def test_a_trace_that_does_not_decompress_refuses_the_package(world: World) -> None:
    world.objects["runs/r1/traces/a1/0.jsonl.zst"] = b"\x28\xb5\x2f\xfd not a frame"
    result, package_dir = export()
    assert result.exit_code == 1
    assert "does not decompress" in " ".join(result.output.split())
    assert not package_dir.exists()


@pytest.mark.parametrize("cut", [4, 100, 900])
def test_a_truncated_trace_refuses_the_package(world: World, cut: int) -> None:
    lines = b"".join(
        json.dumps({"turn": i, "text": hashlib.sha256(str(i).encode()).hexdigest()}).encode()
        + b"\n"
        for i in range(400)
    )
    whole = zstandard.ZstdCompressor(write_checksum=True).compress(lines)
    assert len(whole) > cut
    world.objects["runs/r1/traces/a1/0.jsonl.zst"] = whole[:-cut]
    result, package_dir = export()
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "runs/r1/yield/store/traces/a1/0.jsonl.zst does not decompress" in flat
    assert not package_dir.exists()


def test_a_trace_of_several_frames_is_scanned_whole(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    value = "second-frame-secret"
    data = zstandard.compress(b"first\n") + zstandard.compress(f"has {value}\n".encode())
    (root / "t.jsonl.zst").write_bytes(data)
    found = package.scan(root, {"S": value})
    assert found.unreadable == [] and [leak.secret for leak in found.leaks] == ["S"]


def test_a_value_split_across_read_chunks_is_found(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    value = "chunk-boundary-secret"
    (root / "big.txt").write_bytes(b"x" * ((1 << 20) - 5) + value.encode())
    assert [leak.secret for leak in package.scan(root, {"S": value}).leaks] == ["S"]


@pytest.mark.parametrize("lead", [b"", b"a", b"ab"])
@pytest.mark.parametrize("urlsafe", [False, True])
@pytest.mark.parametrize("padded", [False, True])
def test_a_base64_encoded_value_is_caught_at_every_alignment(
    tmp_path: Path, lead: bytes, urlsafe: bool, padded: bool
) -> None:
    value = "u?s>e~r:p/a+s=s!w?d"
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    encoded = encode(lead + value.encode() + b"z")
    if not padded:
        encoded = encoded.rstrip(b"=")
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "auth.log").write_bytes(b"Authorization: Basic " + encoded + b"\n")
    assert [leak.secret for leak in package.scan(root, {"S": value}).leaks] == ["S"]


def test_a_percent_encoded_value_is_caught(tmp_path: Path) -> None:
    value = "tok/en+with=chars&more"
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "error.txt").write_text(f"GET /v1?key={quote(value, safe='')} failed")
    assert [leak.secret for leak in package.scan(root, {"S": value}).leaks] == ["S"]


def test_a_value_shorter_than_the_floor_is_named_and_not_matched(world: World) -> None:
    paths.secrets_file().write_text(
        f"YIELD_S3_ACCESS_KEY=yieldadmin-key\nYIELD_S3_SECRET_KEY={STORE_SECRET}\nSHORT=acme\n"
    )
    result, package_dir = export("--json")
    assert result.exit_code == 0, result.output
    assert "SHORT" in result.stderr and "not scanned" in result.stderr
    assert json.loads(result.stdout)["unscanned_secrets"] == ["SHORT"]
    assert package.verify(package_dir) == []


def test_a_lens_answer_that_is_not_json_is_a_gap(world: World) -> None:
    world.lens_body = b"<html>502 Bad Gateway</html>"
    result, _ = export("--json")
    assert result.exit_code == 0, result.output
    gaps = {gap["what"]: gap["why"] for gap in json.loads(result.stdout)["gaps"]}
    assert gaps["r1: Lens status"] == "answered something that is not JSON"


def test_a_malformed_queue_refuses_the_export_as_proof_lens_does(world: World) -> None:
    queue = paths.home() / "bundles" / "acme" / "queue.json"
    queue.write_text(json.dumps({"runs": [{"run_id": "r1"}, {"phase": "done"}]}))
    result, package_dir = export()
    assert result.exit_code == 2
    assert "has no run_id" in " ".join(result.output.split())
    assert not package_dir.exists()


def test_kill_records_from_proof_lens_are_carried(world: World) -> None:
    kills = paths.home() / "bundles" / "acme" / "lens" / "kills"
    kills.mkdir(parents=True)
    (kills / "lens-run-r1-abcd1234-1.json").write_text('{"exit_code": 137}')
    result, package_dir = export()
    assert result.exit_code == 0, result.output
    assert (package_dir / "lens" / "kills" / "lens-run-r1-abcd1234-1.json").is_file()


def test_verify_refuses_absolute_paths_and_symlinks(world: World) -> None:
    _, package_dir = export()
    sums = package_dir / "SHA256SUMS"
    sums.write_text(sums.read_text() + f"{'0' * 64}  /etc/passwd\n")
    (package_dir / "link.json").symlink_to(package_dir / "index.json")
    problems = {(p.path, p.problem) for p in package.verify(package_dir)}
    assert ("link.json", "symlink") in problems
    assert any(problem == "malformed" for _, problem in problems)
    assert not any(path == "/etc/passwd" for path, _ in problems)
