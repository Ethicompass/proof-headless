"""What the stack would run with, read without a daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from proof_headless import endpoints
from proof_headless.models.manifest import Manifest, Role, load
from proof_headless.stack import lock as locks
from proof_headless.stack import render
from tests.conftest import LOCK, SPARK_MANIFEST

GOLDEN = Path(__file__).parent / "golden" / "stack-compose.yaml"
SECRETS = ("ACME_TARGET_KEY",)


def _document(manifest: Manifest | None = None, names: tuple[str, ...] = SECRETS) -> dict[str, Any]:
    return render.compose(locks.load(LOCK), manifest or load(SPARK_MANIFEST), names)


def _with(**changes: Any) -> Manifest:
    """The Spark manifest with its chat model changed."""
    spark = load(SPARK_MANIFEST)
    models = tuple(
        model.model_copy(update=changes) if Role.CHAT in model.roles else model
        for model in spark.models
    )
    return spark.model_copy(update={"models": models})


def test_every_service_runs_a_locked_image_by_digest() -> None:
    lock = locks.load(LOCK)
    pinned = {image.pinned for image in lock.images.values()}
    for name, service in _document()["services"].items():
        assert service["image"] in pinned, name


def test_the_images_the_apis_launch_are_the_locked_ones() -> None:
    lock = locks.load(LOCK)
    services = _document()["services"]
    assert (
        services["yield-api"]["environment"]["YIELD_DRIVER_IMAGE"]
        == lock.image("yield-driver").pinned
    )
    compute = services["yield-compute"]
    assert compute["environment"]["YIELD_COMPUTE_IMAGE"] == compute["image"]
    assert services["lens-api"]["environment"]["LENS_RUN_IMAGE"] == lock.image("lens-run").pinned


def test_every_host_port_is_bound_to_loopback() -> None:
    for name, service in _document()["services"].items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)


def test_jobs_join_the_shared_and_the_models_networks_on_top_of_their_own() -> None:
    services = _document()["services"]
    for api in ("yield-api", "yield-compute"):
        environment = services[api]["environment"]
        assert environment["YIELD_DOCKER_NETWORK"] == "proof-yield"
        assert environment["YIELD_EXTRA_NETWORKS"] == "proof-stack,proof-models"
    lens = services["lens-api"]["environment"]
    assert lens["LENS_DOCKER_NETWORK"] == "proof-lens"
    assert lens["LENS_EXTRA_NETWORKS"] == "proof-stack,proof-models"


def test_lens_reaches_yield_s_store_and_yield_reaches_lens_api_on_the_shared_network() -> None:
    document = _document()
    services = document["services"]
    assert "proof-stack" in services["yield-minio"]["networks"]
    assert "proof-stack" in services["lens-api"]["networks"]
    assert "proof-stack" not in services["lens-minio"]["networks"], "one name per store"
    lens = services["lens-api"]["environment"]
    assert lens["LENS_YIELD_STORE_ENDPOINT"] == "http://yield-minio:9000"
    assert lens["LENS_S3_ENDPOINT"] == "http://lens-minio:9000"
    assert document["networks"]["proof-models"] == {"name": "proof-models", "external": True}


def test_no_service_loads_the_secrets_file_and_yield_gets_only_the_bundles_names() -> None:
    services = _document()["services"]
    for name, service in services.items():
        assert "env_file" not in service, name
    for api in ("yield-api", "yield-compute"):
        environment = services[api]["environment"]
        assert environment["ACME_TARGET_KEY"] == "${ACME_TARGET_KEY:-}"
        assert "HF_TOKEN" not in environment
    lens = services["lens-api"]["environment"]
    assert "ACME_TARGET_KEY" not in lens
    assert not any(key.endswith("_CREDENTIAL_REF") for key in lens)


def test_a_bundle_cannot_declare_a_yield_or_lens_setting_as_a_secret() -> None:
    with pytest.raises(render.StackInvalid, match="YIELD_STORE_BACKEND"):
        _document(names=("YIELD_STORE_BACKEND",))


def test_lens_reads_and_curates_with_the_chat_model_reasoning_off() -> None:
    chat = load(SPARK_MANIFEST).named("chat")
    lens = _document()["services"]["lens-api"]["environment"]
    for role in ("READER", "CURATOR"):
        assert lens[f"LENS_MODEL_{role}_PROVIDER"] == "openai_compatible"
        assert lens[f"LENS_MODEL_{role}_BASE_URL"] == "http://chat:8000/v1"
        assert lens[f"LENS_MODEL_{role}_ID"] == chat.served_name
        assert lens[f"LENS_MODEL_{role}_STRUCTURED_OUTPUT"] == "json_schema"
        body = json.loads(lens[f"LENS_MODEL_{role}_EXTRA_BODY"])
        assert body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_a_chat_model_with_no_reasoning_switch_gets_no_extra_body() -> None:
    lens = _document(_with(reasoning_switch=None))["services"]["lens-api"]["environment"]
    assert lens["LENS_MODEL_READER_ID"]
    assert not any(key.endswith("_EXTRA_BODY") for key in lens)


def test_a_manifest_with_no_chat_model_is_refused() -> None:
    with pytest.raises(render.StackInvalid, match="chat role"):
        _document(_with(roles=frozenset({Role.GUARDIAN})))


def test_the_models_network_is_the_manifest_s() -> None:
    spark = load(SPARK_MANIFEST)
    document = _document(spark.model_copy(update={"network": "lab-models"}))
    assert document["networks"]["lab-models"] == {"name": "lab-models", "external": True}
    assert "proof-models" not in document["networks"]
    services = document["services"]
    assert services["yield-api"]["environment"]["YIELD_EXTRA_NETWORKS"] == "proof-stack,lab-models"
    assert services["lens-api"]["environment"]["LENS_EXTRA_NETWORKS"] == "proof-stack,lab-models"


def test_the_ports_the_stack_publishes_are_the_endpoints_commands_call() -> None:
    ports = render.PORTS
    assert endpoints.YIELD_API.endswith(f":{ports['yield-api']}")
    assert endpoints.YIELD_COMPUTE.endswith(f":{ports['yield-compute']}")
    assert endpoints.LENS_API.endswith(f":{ports['lens-api']}")
    assert endpoints.BRAIN_REGISTRY.endswith(f":{ports['brain-registry']}")


def test_lens_pins_the_digests_its_seed_checks() -> None:
    services = _document()["services"]
    environment = services["lens-api"]["environment"]
    for spec in services["lens-seed"]["entrypoint"][3:]:
        _, _, _, digest = spec.split("|")
        assert digest in environment.values()


def test_the_brain_registry_is_plain_http_and_reachable_from_compute_s_jobs() -> None:
    services = _document()["services"]
    compute = services["yield-compute"]["environment"]
    assert compute["YIELD_BRAIN_REGISTRY_INSECURE"] == "true"
    assert compute["YIELD_BRAIN_REGISTRY_SECRET_REF"] == ""
    assert {"proof-yield", "proof-stack"} <= set(services["brain-registry"]["networks"])
    assert services["brain-registry"]["ports"] == ["127.0.0.1:5051:5000"]


def test_seeds_and_buckets_are_one_shot_and_everything_else_stays_up() -> None:
    document = _document()
    assert set(render.one_shot(document)) == {
        "yield-minio-buckets",
        "yield-seed",
        "lens-minio-buckets",
        "lens-seed",
    }
    assert "yield-api" in render.long_running(document)


def test_write_copies_the_seed_beside_the_compose_file(tmp_path: Path) -> None:
    path = render.write(locks.load(LOCK), load(SPARK_MANIFEST), tmp_path / "stack", SECRETS)
    assert (path.parent / "seed" / "yield" / "seed.sh").is_file()
    assert (path.parent / "seed" / "lens" / "seed.py").is_file()
    assert (tmp_path / "declarations").is_dir(), "where `proof bundle load` puts declarations"
    assert not (path.parent / "seed" / "yield" / "catalogues" / "bpd").exists()


def test_the_lock_renders_the_reviewed_compose_file() -> None:
    """The whole document, against a reviewed copy: a change to what the stack runs with shows
    up as a diff of this file. `UPDATE_GOLDEN=1` rewrites it."""
    rendered = yaml.safe_dump(_document(), sort_keys=False)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(rendered)
    assert rendered == GOLDEN.read_text()
