"""The compose project that serves a manifest's models.

One service per model on one network, so Yield's and Lens's containers reach a model by its name
(`http://<name>:8000/v1`) and the host reaches it on loopback only (`127.0.0.1:<port>`): a model
server answers whoever reaches it, and nothing on the LAN should. Rendering is pure, so what would
run can be read (`proof models render`) and tested without a docker daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from proof_headless.models.manifest import Manifest, ModelEntry

PROJECT = "proof-models"
CONTAINER_PORT = 8000
CACHE_IN_CONTAINER = "/root/.cache/huggingface"
HEALTH_PROBE = (
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"
)
"""Python rather than curl: the vLLM images ship Python and do not promise curl."""


def command(model: ModelEntry) -> list[str]:
    """`vllm serve` arguments: the flags `proof` owns, then the manifest's own, verbatim."""
    return [
        model.hf_id,
        "--served-model-name",
        model.served_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(CONTAINER_PORT),
        "--gpu-memory-utilization",
        f"{model.gpu_memory_utilization:g}",
        *model.args,
    ]


def service(model: ModelEntry, manifest: Manifest) -> dict[str, Any]:
    """One model's compose service."""
    environment: dict[str, str] = {"HF_HOME": CACHE_IN_CONTAINER}
    if model.gated:
        environment["HF_TOKEN"] = "${HF_TOKEN:?HF_TOKEN must be set in the secrets file}"
    volumes = [f"{manifest.hf_cache.expanduser()}:{CACHE_IN_CONTAINER}"]
    volumes += [f"{mount.source}:{mount.target}:ro" for mount in model.mounts]
    return {
        "image": model.image,
        "entrypoint": list(model.entrypoint),
        "command": command(model),
        "ports": [f"127.0.0.1:{model.port}:{CONTAINER_PORT}"],
        "ipc": "host",
        "environment": environment,
        "volumes": volumes,
        "networks": [manifest.network],
        "restart": "unless-stopped",
        "deploy": {
            "resources": {
                "reservations": {
                    "devices": [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
                }
            }
        },
        "healthcheck": {
            "test": ["CMD", "python3", "-c", HEALTH_PROBE],
            "interval": "15s",
            "timeout": "10s",
            "retries": 5,
            "start_period": f"{model.start_period_seconds}s",
        },
    }


def compose(manifest: Manifest) -> dict[str, Any]:
    """The whole compose document for `manifest`."""
    return {
        "name": PROJECT,
        "services": {model.name: service(model, manifest) for model in manifest.models},
        "networks": {manifest.network: {"name": manifest.network}},
    }


def write(manifest: Manifest, directory: Path) -> Path:
    """Render `manifest` to `directory/compose.yaml` and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "compose.yaml"
    path.write_text(yaml.safe_dump(compose(manifest), sort_keys=False))
    return path
