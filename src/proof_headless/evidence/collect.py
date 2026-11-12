"""Gathering one client's evidence into a directory, and saying what could not be gathered.

Every source is optional on its own: a service that is down, a run Lens never received, a store
without credentials. What is missing is recorded as a gap in `index.json` and the README rather
than stopping the export, because a package that says plainly what it lacks is evidence, and one
that was never written because a single source was down is none. The checksums and the secret
scan are what make a package acceptable, not its completeness.

The runs, and the Lens ids each run's delivery derives, come from `proof_headless.lens.runs`, the
module `proof lens` reads them with, so the package and `proof lens status` always agree on which
runs a client has: a queue state or specs that do not read refuse the export rather than leave a
run out silently.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from proof_headless import __version__, paths
from proof_headless.docker import Runner
from proof_headless.evidence.s3 import Store, StoreUnavailable
from proof_headless.lens import runs as yield_runs
from proof_headless.lens.runs import RunsUnknown, YieldRun
from proof_headless.models import manifest as manifests
from proof_headless.models.manifest import ManifestInvalid


class NothingToExport(ValueError):
    """The client's runs cannot be listed: nothing under `$PROOF_HOME`, or a queue state or spec
    that does not read."""


@dataclass(frozen=True)
class Endpoints:
    """Where the services are, all on the appliance's loopback by default."""

    yield_api: str = "http://127.0.0.1:8080"
    lens_api: str = "http://127.0.0.1:8090"
    model_host: str = "127.0.0.1"


@dataclass
class Collector:
    """Copies one client's evidence under `root`. `gaps` collects what it could not."""

    client: str
    root: Path
    http: httpx.Client
    runner: Runner
    endpoints: Endpoints = field(default_factory=Endpoints)
    store: Store | None = None
    store_gap: str | None = None
    """Why there is no store to read traces from, when `store` is `None`."""
    manifest: Path | None = None
    gaps: list[dict[str, str]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return paths.home() / "bundles" / self.client

    def gap(self, what: str, why: str) -> None:
        self.gaps.append({"what": what, "why": why})

    def _write(self, relative: str, data: bytes | str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            target.write_text(data)
        else:
            target.write_bytes(data)

    def _json(self, relative: str, document: Any) -> None:
        self._write(relative, json.dumps(document, indent=2, ensure_ascii=False) + "\n")

    def _copy(self, source: Path, relative: str) -> bool:
        if not source.is_file():
            return False
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return True

    def _fetch(self, url: str, what: str) -> httpx.Response | None:
        try:
            response = self.http.get(url)
        except httpx.HTTPError as error:
            self.gap(what, f"unreachable: {type(error).__name__}")
            return None
        if response.status_code >= 400:
            self.gap(what, f"answered {response.status_code}")
            return None
        return response

    def _fetch_json(self, url: str, what: str) -> tuple[httpx.Response, Any] | None:
        """The answer and its JSON, or `None` with a gap when either is missing."""
        response = self._fetch(url, what)
        if response is None:
            return None
        try:
            return response, response.json()
        except ValueError:
            self.gap(what, "answered something that is not JSON")
            return None

    def collect(self) -> None:
        """Everything, in the package's order."""
        runs = self.yield_runs()
        self.bundle()
        self.appliance()
        self.doctor()
        self.kills()
        for run in runs:
            self.run(run)

    def yield_runs(self) -> list[YieldRun]:
        """The client's runs, each one's Lens project read once here, so a declaration that is
        not a digest refuses the export before anything is copied, as `proof lens` refuses it."""
        queued = (self.state / "queue.json").is_file() or (self.state / "specs").is_dir()
        if not queued and (self.state / "receipt.json").is_file():
            self.gap("runs", "no queue state and no rendered specs; no run to export")
            return []
        try:
            runs = yield_runs.client_runs(self.client)
            for run in runs:
                run.project_id  # noqa: B018
        except RunsUnknown as error:
            raise NothingToExport(str(error)) from None
        return runs

    def bundle(self) -> None:
        if not self._copy(self.state / "receipt.json", "bundle/receipt.json"):
            self.gap("bundle receipt", "the bundle was never loaded on this appliance")
        self._copy(self.state / "queue.json", "bundle/queue.json")
        for spec in sorted((self.state / "specs").glob("*.json")):
            self._copy(spec, f"bundle/specs/{spec.name}")

    def appliance(self) -> None:
        manifest_path = self.manifest or paths.default_manifest()
        if not self._copy(manifest_path, "appliance/models.yaml"):
            self.gap("models manifest", f"none at {manifest_path}")
        if not self._copy(
            paths.home() / "models" / "compose.yaml", "appliance/models-compose.yaml"
        ):
            self.gap("models compose", "never rendered on this appliance")
        if not self._copy(paths.home() / "appliance.lock", "appliance/appliance.lock"):
            self.gap("appliance.lock", "no lock under $PROOF_HOME (written by `proof stack`)")
        stack = paths.home() / "stack"
        for path in sorted(stack.rglob("*.y*ml")) if stack.is_dir() else []:
            self._copy(path, f"appliance/stack/{path.relative_to(stack).as_posix()}")
        self.images()
        self.vllm(manifest_path)

    def images(self) -> None:
        """Each running container, its image reference, image id and repository digests."""
        listed = self.runner(["docker", "ps", "--format", "{{.Names}}"])
        if listed.returncode != 0:
            self.gap("image digests", "docker ps failed")
            return
        images: list[dict[str, Any]] = []
        for name in sorted(line.strip() for line in listed.stdout.splitlines() if line.strip()):
            described = self.runner(
                ["docker", "inspect", "--format", "{{.Config.Image}}|{{.Image}}", name]
            )
            if described.returncode != 0:
                self.gap(f"image of {name}", "docker inspect failed")
                continue
            reference, _, image_id = described.stdout.strip().partition("|")
            digests = self.runner(
                ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id]
            )
            repo = json.loads(digests.stdout) if digests.returncode == 0 else None
            images.append(
                {"container": name, "image": reference, "id": image_id, "repo_digests": repo}
            )
        self._json("appliance/images.json", images)

    def vllm(self, manifest_path: Path) -> None:
        """What each served model's server reports at `/version`."""
        try:
            loaded = manifests.load(manifest_path)
        except (ManifestInvalid, OSError):
            return
        versions: dict[str, Any] = {}
        for model in loaded.models:
            url = f"http://{self.endpoints.model_host}:{model.port}/version"
            answer = self._fetch_json(url, f"server version of {model.name}")
            versions[model.name] = answer[1] if answer is not None else None
        self._json("appliance/vllm-versions.json", versions)

    def doctor(self) -> None:
        reports = sorted((paths.home() / "evidence").glob("doctor-*.json"))
        if not reports:
            self.gap("doctor reports", "`proof doctor` never ran on this appliance")
        for report in reports:
            self._copy(report, f"doctor/{report.name}")

    def kills(self) -> None:
        """The records `proof lens run` kept of dead lens-run containers before removing them."""
        for record in sorted((self.state / "lens" / "kills").glob("*.json")):
            self._copy(record, f"lens/kills/{record.name}")

    def run(self, run: YieldRun) -> None:
        run_id = run.run_id
        base = f"runs/{run_id}"
        entry: dict[str, Any] = {"run_id": run_id}
        yield_api = self.endpoints.yield_api.rstrip("/")
        status = self._fetch(f"{yield_api}/runs/{run_id}", f"{run_id}: Yield status")
        if status is not None:
            self._write(f"{base}/yield/status.json", status.content)
        result = self._fetch(f"{yield_api}/runs/{run_id}/result", f"{run_id}: Yield manifest")
        entry["yield_manifest"] = result is not None
        if result is not None:
            self._write(f"{base}/yield/manifest.json", result.content)
        entry["store_objects"] = self.objects(run_id, base)
        entry["probe_set"] = self.probe_set(run_id, base)
        project, lens_run = run.project_id, run.lens_run_id
        entry["lens"] = {"project_id": project, "run_id": lens_run, "result": None}
        lens_api = self.endpoints.lens_api.rstrip("/")
        lens = self._fetch_json(
            f"{lens_api}/projects/{project}/runs/{lens_run}", f"{run_id}: Lens status"
        )
        if lens is not None:
            response, document = lens
            self._write(f"{base}/lens/status.json", response.content)
            ref = document.get("result_ref") if isinstance(document, dict) else None
            if not ref:
                self.gap(f"{run_id}: Lens result", "the Lens run has no result; see `proof lens`")
            else:
                artifact = self._fetch(f"{lens_api}/artifacts/{ref}", f"{run_id}: Lens result")
                if artifact is not None:
                    self._write(f"{base}/lens/result.json", artifact.content)
                    entry["lens"]["result"] = ref
        self.runs.append(entry)

    def probe_set(self, run_id: str, base: str) -> bool:
        """Copy the probe set the run's `probes.json` points at, a blob outside the run's prefix
        that two runs over the same knowledge base and catalogue share (Yield `layout.blob`)."""
        pointer = self.root / base / "yield" / "store" / "probes.json"
        if self.store is None or not pointer.is_file():
            return False
        what = f"{run_id}: probe set"
        try:
            digest = str(json.loads(pointer.read_text())["digest"]).removeprefix("sha256:")
        except (ValueError, KeyError, TypeError):
            self.gap(what, "probes.json names no digest")
            return False
        try:
            self._write(f"{base}/yield/probe-set.json", self.store.get(f"blobs/{digest}"))
        except StoreUnavailable as error:
            self.gap(what, str(error))
            return False
        return True

    def objects(self, run_id: str, base: str) -> int:
        """Copy the run's objects out of Yield's store; how many."""
        if self.store is None:
            self.gap(f"{run_id}: traces and dataset", self.store_gap or "no store configured")
            return 0
        prefix = f"runs/{run_id}/"
        count = 0
        try:
            for key in self.store.keys(prefix):
                relative = key.removeprefix(prefix)
                if not relative or ".." in relative.split("/"):
                    continue
                self._write(f"{base}/yield/store/{relative}", self.store.get(key))
                count += 1
        except StoreUnavailable as error:
            self.gap(f"{run_id}: traces and dataset", str(error))
        return count

    def index(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "proof": __version__,
            "runs": self.runs,
            "gaps": self.gaps,
        }
