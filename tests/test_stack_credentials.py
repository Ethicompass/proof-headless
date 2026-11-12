"""The stack's own credentials: generated into the secrets file when absent, never defaulted."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from proof_headless import paths, secrets
from proof_headless.evidence import package
from proof_headless.models.manifest import load
from proof_headless.stack import credentials, render
from proof_headless.stack import lock as locks
from tests.conftest import LOCK, SPARK_MANIFEST
from tests.fakes import FakeCompose


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_every_absent_credential_is_generated_into_a_private_file() -> None:
    path = paths.secrets_file()
    generated = credentials.ensure(path, render.PROJECT, FakeCompose())
    assert generated == list(credentials.NAMES)
    held = secrets.values(path)
    assert set(credentials.NAMES) <= set(held)
    for name in credentials.NAMES:
        assert len(held[name]) >= 24 and held[name].isalnum(), name
    assert len({held[name] for name in credentials.NAMES}) == len(credentials.NAMES)
    assert _mode(path) == 0o600


def test_a_credential_the_file_defines_is_never_overwritten() -> None:
    path = paths.secrets_file()
    path.write_text("YIELD_S3_ACCESS_KEY='operator-chosen-key'\nACME_TARGET_KEY='k'\n")
    path.chmod(0o600)
    generated = credentials.ensure(path, render.PROJECT, FakeCompose())
    assert "YIELD_S3_ACCESS_KEY" not in generated
    held = secrets.values(path)
    assert held["YIELD_S3_ACCESS_KEY"] == "operator-chosen-key"
    assert held["ACME_TARGET_KEY"] == "k"
    assert _mode(path) == 0o600
    again = dict(held)
    assert credentials.ensure(path, render.PROJECT, FakeCompose()) == []
    assert secrets.values(path) == again


def test_a_store_started_without_them_is_refused_and_nothing_is_written() -> None:
    """A store whose volume exists was initialised with the old development defaults: new
    credentials would lock Yield or Lens out of it."""
    path = paths.secrets_file()
    fake = FakeCompose(volumes={"proof-stack_yield_minio"})
    with pytest.raises(credentials.StoreInitialised, match="docker volume rm proof-stack_yield"):
        credentials.ensure(path, render.PROJECT, fake)
    assert not path.exists()


def test_a_started_store_whose_credentials_the_file_holds_is_fine() -> None:
    path = paths.secrets_file()
    path.write_text("YIELD_S3_ACCESS_KEY='yieldadmin'\nYIELD_S3_SECRET_KEY='yieldadmin'\n")
    path.chmod(0o600)
    fake = FakeCompose(volumes={"proof-stack_yield_minio"})
    generated = credentials.ensure(path, render.PROJECT, fake)
    assert "YIELD_S3_ACCESS_KEY" not in generated and "LENS_S3_ACCESS_KEY" in generated


def test_the_rendered_stack_holds_no_credential_value_the_evidence_scan_would_find(
    tmp_path: Path,
) -> None:
    """The evidence package carries the stack's compose file; with the credentials generated
    into the secrets file, the scan finds none of them there."""
    path = paths.secrets_file()
    credentials.ensure(path, render.PROJECT, FakeCompose())
    directory = tmp_path / "package"
    render.write(locks.load(LOCK), load(SPARK_MANIFEST), directory)
    findings = package.scan(directory, secrets.values(path))
    assert findings.leaks == []
    assert findings.unscanned == []


def test_the_compose_file_refers_to_each_credential_by_name_only() -> None:
    document = render.compose(locks.load(LOCK), load(SPARK_MANIFEST))
    text = yaml.safe_dump(document)
    for default in ("yieldadmin", "lensadmin", "lens-local-signing-secret"):
        assert default not in text
    for name in credentials.NAMES:
        assert f"${{{name}:-" not in text, name
        assert f"${{{name}:?" in text, name
    services = document["services"]
    for store in ("yield-minio", "lens-minio"):
        for value in services[store]["environment"].values():
            assert value.startswith("${") and ":?" in value and value.endswith("}")
