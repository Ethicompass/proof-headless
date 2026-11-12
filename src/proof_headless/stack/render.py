"""The compose project that runs Yield, Lens and the brain registry next to the served models.

One project rendered from the lock, rather than each repository's compose driven with overrides,
because the appliance must not need a checkout of Yield or Lens: their compose files build from
source and mount `../seed`, and the overrides that made them run from images
(`pull_policy: never`, `host.docker.internal`, a patched compute) were per-engagement. The services
and settings here follow Yield's and Lens's own compose files, with their co-location changes
(Yield's `docs/self-hosted.md`, Lens's `docs/self-hosted.md`).

Networks, all with fixed names because the APIs hand them to the daemon for every job they launch:

- `proof-yield`: Yield's own. Its jobs reach `yield-minio`, `yield-valkey` and `yield-compute`.
- `proof-lens`: Lens's own. Its jobs reach `lens-minio` and `lens-declarations`.
- `proof-stack`: the hops between the two. Yield's measurement job posts its webhook to
  `lens-api`, and `lens-run` reads Yield's store as `yield-minio`. Lens's MinIO stays off it, so
  each store has one name everywhere. The brain registry is here as `brain-registry:5000`.
- `proof-models`: the models' network, made by `proof models` (external here). Jobs join it to
  reach `http://<model>:8000/v1`.

Every service lists the networks it joins, and every job joins `proof-stack` and `proof-models`
through `YIELD_EXTRA_NETWORKS` and `LENS_EXTRA_NETWORKS`. Every host port binds loopback only.

Rendering is pure: what would run can be read (`proof stack render`) and diffed in a golden test.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from proof_headless.stack.lock import Lock

PROJECT = "proof-stack"
YIELD_NET = "proof-yield"
LENS_NET = "proof-lens"
SHARED_NET = "proof-stack"
MODELS_NET = "proof-models"
JOB_NETWORKS = f"{SHARED_NET},{MODELS_NET}"

PORTS = {
    "yield-api": 8080,
    "yield-compute": 8081,
    "yield-minio": 9000,
    "yield-valkey": 6379,
    "lens-api": 8090,
    "lens-minio": 9100,
    "brain-registry": 5051,
}
"""Host ports, on 127.0.0.1. The ones Yield's and Lens's own compose files default to, and
5051 for the registry, where vitruvio pushes a brain from the host."""

CONSOLE_PORTS = {"yield-minio": 9001, "lens-minio": 9101}

YIELD_STORE_CREDENTIALS = (
    "${YIELD_S3_ACCESS_KEY:-yieldadmin}",
    "${YIELD_S3_SECRET_KEY:-yieldadmin}",
)
LENS_STORE_CREDENTIALS = ("${LENS_S3_ACCESS_KEY:-lensadmin}", "${LENS_S3_SECRET_KEY:-lensadmin}")
"""The stores' root credentials, interpolated from the secrets file when the operator set them
there, else the development defaults of each repository. The stores answer on loopback and on
the stack's own networks only."""

STACK_SECRETS = frozenset(
    {
        "YIELD_S3_ACCESS_KEY",
        "YIELD_S3_SECRET_KEY",
        "LENS_S3_ACCESS_KEY",
        "LENS_S3_SECRET_KEY",
        "LENS_WEBHOOK_SIGNING_SECRET",
    }
)
"""The `YIELD_`/`LENS_` names the secrets file may hold: the ones this file interpolates."""


def reconfigures(name: str) -> bool:
    """Whether a secret named `name` would reconfigure Yield or Lens, which load the secrets file:
    a `YIELD_`/`LENS_` name is a setting of theirs and cannot be a `secret_ref`."""
    return name.startswith(("YIELD_", "LENS_")) and name not in STACK_SECRETS


SEED_PACKAGE = "proof_headless.stack"
DECLARATIONS = "../declarations"
"""`$PROOF_HOME/declarations`, relative to the compose file under `$PROOF_HOME/stack`: where
`proof bundle load` places a bundle's declarations, each named by the hex of its digest, which is
the path Lens's Reader asks the declaration source for (`GET /declarations/{hex}`)."""


def _py_health(url: str) -> dict[str, Any]:
    """A health check in Python rather than curl: the Python images do not promise curl."""
    probe = f"import urllib.request as r; r.urlopen('{url}', timeout=5)"
    return {
        "test": ["CMD", "python", "-c", probe],
        "interval": "5s",
        "timeout": "5s",
        "retries": 24,
        "start_period": "10s",
    }


def _minio(
    image: str, alias: str, credentials: tuple[str, str], networks: list[str]
) -> dict[str, Any]:
    user, password = credentials
    return {
        "image": image,
        "command": 'server /data --console-address ":9001"',
        "ports": [
            f"127.0.0.1:{PORTS[alias]}:9000",
            f"127.0.0.1:{CONSOLE_PORTS[alias]}:9001",
        ],
        "environment": {"MINIO_ROOT_USER": user, "MINIO_ROOT_PASSWORD": password},
        "volumes": [f"{alias.replace('-', '_')}:/data"],
        "networks": networks,
        "restart": "unless-stopped",
        "healthcheck": {
            "test": ["CMD", "mc", "ready", "local"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 12,
        },
    }


def _buckets(
    image: str, store: str, credentials: tuple[str, str], buckets: list[str], network: str
) -> dict[str, Any]:
    """A one-shot `mc` that creates the buckets; `--ignore-existing` makes it idempotent."""
    user, password = credentials
    steps = [f"mc alias set local http://{store}:9000 {user} {password}"]
    steps += [f"mc mb --ignore-existing local/{bucket}" for bucket in buckets]
    return {
        "image": image,
        "depends_on": {store: {"condition": "service_healthy"}},
        "entrypoint": ["/bin/sh", "-c", " && ".join(steps)],
        "networks": [network],
    }


def _secrets(secrets_file: Path) -> list[dict[str, Any]]:
    """The operator's secrets file, for the services that resolve a `secret_ref`. Optional: absent,
    the stack still starts and a run naming a secret is refused at its gate."""
    return [{"path": str(secrets_file), "required": False}]


def _lens_assets() -> list[dict[str, str]]:
    text = resources.files(SEED_PACKAGE).joinpath("seed/SOURCE.yaml").read_text()
    assets: list[dict[str, str]] = yaml.safe_load(text)["lens"]["assets"]
    return assets


def _yield_services(lock: Lock, secrets_file: Path) -> dict[str, Any]:
    minio = lock.image("minio").pinned
    compute_image = lock.image("yield-compute").pinned
    user, password = YIELD_STORE_CREDENTIALS
    store = {
        "YIELD_STORE_BACKEND": "s3",
        "YIELD_S3_ENDPOINT": "http://yield-minio:9000",
        "YIELD_S3_BUCKET": "yield",
        "YIELD_S3_ACCESS_KEY": user,
        "YIELD_S3_SECRET_KEY": password,
        "YIELD_JOBS_BACKEND": "valkey",
        "YIELD_JOBS_URL": "redis://yield-valkey:6379/0",
        "YIELD_SECRETS_BACKEND": "env",
        "YIELD_DISPATCH_BACKEND": "docker",
        "YIELD_DOCKER_NETWORK": YIELD_NET,
        "YIELD_EXTRA_NETWORKS": JOB_NETWORKS,
    }
    socket = ["/var/run/docker.sock:/var/run/docker.sock"]
    return {
        "yield-minio": _minio(
            minio, "yield-minio", YIELD_STORE_CREDENTIALS, [YIELD_NET, SHARED_NET]
        ),
        "yield-minio-buckets": _buckets(
            minio, "yield-minio", YIELD_STORE_CREDENTIALS, ["yield", "yield-results"], YIELD_NET
        ),
        "yield-valkey": {
            "image": lock.image("valkey").pinned,
            "command": 'valkey-server --save "" --appendonly no',
            "ports": [f"127.0.0.1:{PORTS['yield-valkey']}:6379"],
            "networks": [YIELD_NET],
            "restart": "unless-stopped",
            "healthcheck": {
                "test": ["CMD", "valkey-cli", "ping"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 12,
            },
        },
        "yield-compute": {
            "image": compute_image,
            "depends_on": {
                "yield-minio-buckets": {"condition": "service_completed_successfully"},
                "yield-valkey": {"condition": "service_healthy"},
            },
            "ports": [f"127.0.0.1:{PORTS['yield-compute']}:8080"],
            "environment": store
            | {
                "YIELD_COMPUTE_IMAGE": compute_image,
                "YIELD_JUDGE_PROBE_TIMEOUT_SECONDS": "30",
                "YIELD_BRAIN_REGISTRY_INSECURE": "true",
                "YIELD_BRAIN_REGISTRY_SECRET_REF": "",
            },
            "env_file": _secrets(secrets_file),
            "volumes": socket,
            "networks": [YIELD_NET, SHARED_NET],
            "restart": "unless-stopped",
            "healthcheck": _py_health("http://localhost:8080/healthz"),
        },
        "yield-seed": {
            "image": lock.image("python").pinned,
            "depends_on": {"yield-compute": {"condition": "service_healthy"}},
            "volumes": ["./seed/yield:/seed:ro"],
            "environment": {"COMPUTE": "http://yield-compute:8080"},
            "entrypoint": ["bash", "/seed/seed.sh"],
            "networks": [YIELD_NET],
        },
        "yield-api": {
            "image": lock.image("yield-api").pinned,
            "depends_on": {
                "yield-minio-buckets": {"condition": "service_completed_successfully"},
                "yield-valkey": {"condition": "service_healthy"},
                "yield-seed": {"condition": "service_completed_successfully"},
            },
            "ports": [f"127.0.0.1:{PORTS['yield-api']}:8080"],
            "environment": store
            | {
                "YIELD_DRIVER_IMAGE": lock.image("yield-driver").pinned,
                "YIELD_COMPUTE_URL": "http://yield-compute:8080",
            },
            "env_file": _secrets(secrets_file),
            "volumes": socket,
            "networks": [YIELD_NET, SHARED_NET],
            "restart": "unless-stopped",
            "healthcheck": _py_health("http://localhost:8080/healthz"),
        },
    }


def _lens_services(lock: Lock, secrets_file: Path) -> dict[str, Any]:
    minio = lock.image("minio").pinned
    user, password = LENS_STORE_CREDENTIALS
    yield_user, yield_password = YIELD_STORE_CREDENTIALS
    assets = _lens_assets()
    environment = {
        "LENS_STORE_BACKEND": "s3",
        "LENS_DISPATCH_BACKEND": "docker",
        "LENS_RUN_IMAGE": lock.image("lens-run").pinned,
        "LENS_DOCKER_NETWORK": LENS_NET,
        "LENS_EXTRA_NETWORKS": JOB_NETWORKS,
        "LENS_JOBS_BACKEND": "memory",
        "LENS_SECRETS_BACKEND": "env",
        "LENS_PUBLIC_BASE_URL": f"http://localhost:{PORTS['lens-api']}",
        "LENS_S3_ENDPOINT": "http://lens-minio:9000",
        "LENS_S3_BUCKET": "lens",
        "LENS_S3_ACCESS_KEY": user,
        "LENS_S3_SECRET_KEY": password,
        **{asset["setting"]: asset["digest"] for asset in assets},
        "LENS_WEBHOOK_SIGNING_SECRET_REF": "LENS_WEBHOOK_SIGNING_SECRET",
        "LENS_WEBHOOK_SIGNING_SECRET": "${LENS_WEBHOOK_SIGNING_SECRET:-lens-local-signing-secret}",
        "LENS_YIELD_STORE_ENABLED": "true",
        "LENS_YIELD_STORE_ENDPOINT": "http://yield-minio:9000",
        "LENS_YIELD_STORE_BUCKET": "yield",
        "LENS_YIELD_STORE_ACCESS_KEY": yield_user,
        "LENS_YIELD_STORE_SECRET_KEY": yield_password,
        "LENS_DECLARATION_SOURCE": "http",
        "LENS_DECLARATION_SOURCE_URL": "http://lens-declarations:8080",
    }
    seeds = [
        f"{asset['kind']}|{asset['name']}|{asset['file']}|{asset['digest']}" for asset in assets
    ]
    return {
        "lens-minio": _minio(minio, "lens-minio", LENS_STORE_CREDENTIALS, [LENS_NET]),
        "lens-minio-buckets": _buckets(
            minio, "lens-minio", LENS_STORE_CREDENTIALS, ["lens", "lens-events"], LENS_NET
        ),
        "lens-declarations": {
            "image": lock.image("python").pinned,
            "command": ["python", "-m", "http.server", "8080", "--directory", "/srv"],
            "volumes": [f"{DECLARATIONS}:/srv/declarations:ro"],
            "networks": [LENS_NET],
            "restart": "unless-stopped",
            "healthcheck": _py_health("http://localhost:8080/"),
        },
        "lens-api": {
            "image": lock.image("lens-api").pinned,
            "depends_on": {
                "lens-minio-buckets": {"condition": "service_completed_successfully"},
                "lens-declarations": {"condition": "service_started"},
            },
            "ports": [f"127.0.0.1:{PORTS['lens-api']}:8080"],
            "environment": environment,
            "env_file": _secrets(secrets_file),
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
            "networks": [LENS_NET, SHARED_NET],
            "restart": "unless-stopped",
            "healthcheck": _py_health("http://localhost:8080/healthz"),
        },
        "lens-seed": {
            "image": lock.image("python").pinned,
            "depends_on": {"lens-api": {"condition": "service_healthy"}},
            "volumes": ["./seed/lens:/seed:ro"],
            "entrypoint": ["python", "/seed/seed.py", "http://lens-api:8080", *seeds],
            "networks": [LENS_NET],
        },
    }


def _registry(lock: Lock) -> dict[str, Any]:
    """The brain registry: a plain-HTTP OCI registry, as on the first engagement. Compute's jobs
    pull a brain from `brain-registry:5000` (Yield's network and the shared one), and vitruvio
    pushes it from the host to `localhost:5051`; the digest is the same on both names."""
    return {
        "image": lock.image("registry").pinned,
        "ports": [f"127.0.0.1:{PORTS['brain-registry']}:5000"],
        "volumes": ["brain_registry:/var/lib/registry"],
        "networks": [YIELD_NET, SHARED_NET],
        "restart": "unless-stopped",
        "healthcheck": {
            "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:5000/v2/"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 12,
        },
    }


def compose(lock: Lock, secrets_file: Path) -> dict[str, Any]:
    """The whole compose document for `lock`."""
    return {
        "name": PROJECT,
        "services": {
            **_yield_services(lock, secrets_file),
            **_lens_services(lock, secrets_file),
            "brain-registry": _registry(lock),
        },
        "networks": {
            YIELD_NET: {"name": YIELD_NET},
            LENS_NET: {"name": LENS_NET},
            SHARED_NET: {"name": SHARED_NET},
            MODELS_NET: {"name": MODELS_NET, "external": True},
        },
        "volumes": {"yield_minio": {}, "lens_minio": {}, "brain_registry": {}},
    }


def long_running(document: dict[str, Any]) -> list[str]:
    """The services that stay up, as opposed to the one-shot bucket and seed jobs."""
    return [name for name, service in document["services"].items() if "restart" in service]


def one_shot(document: dict[str, Any]) -> list[str]:
    """The services that run once and exit 0: buckets and seeds."""
    return [name for name, service in document["services"].items() if "restart" not in service]


def write(lock: Lock, directory: Path, secrets_file: Path) -> Path:
    """Render `lock` to `directory/compose.yaml`, with the seed assets beside it, make sure the
    declarations directory exists, and return the compose file's path.

    The seed is copied out of the package on every render, so the stack mounts the assets of the
    `proof` that rendered it, and a stale copy from an older version cannot linger.
    """
    directory.mkdir(parents=True, exist_ok=True)
    seed = directory / "seed"
    if seed.exists():
        shutil.rmtree(seed)
    with resources.as_file(resources.files(SEED_PACKAGE).joinpath("seed")) as packaged:
        shutil.copytree(packaged, seed, ignore=shutil.ignore_patterns("__pycache__"))
    (directory / DECLARATIONS).resolve().mkdir(parents=True, exist_ok=True)
    path = directory / "compose.yaml"
    path.write_text(yaml.safe_dump(compose(lock, secrets_file), sort_keys=False))
    return path
