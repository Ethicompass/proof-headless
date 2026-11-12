"""Rendering Yield run specs from a bundle's matrix, its roles, its receipt and the models manifest.

A spec is built as plain JSON in the shape of Yield's `RunSpec`, without importing Yield (ADR
0001): the appliance runs whatever Yield its lock pins, and Yield's gate is what finally accepts a
spec. What this module checks is what only the appliance knows, and what would otherwise surface
as a run refused, or worse accepted, after the stack was up:

- every role is bound to a manifest model that declares that role, and every bound model is paced;
- every catalogue, profile, prior, mapping, brain and declaration a run names is in the bundle and
  in its receipt, so the spec pins the versions and the digest that were actually published;
- the connector's credential is a secret the bundle declares.

Every model is served in the appliance, so every `ModelSpec` is `openai_compatible`, `self_hosted`
and addressed by its container name on the models network. The embedder, the reranker and the
guardian post to their endpoint directly and Yield ignores their provider when building them; it
is still declared, because a run-level embedder without one does not count as an embedder at
Yield's gate, and the provenance should say where every instrument ran. `extra_body` goes only on
chat roles: Yield refuses it on a role that posts directly, since nothing would send it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from proof_headless.bundle import loader
from proof_headless.bundle.loader import Loaded, Piece
from proof_headless.bundle.manifest import Bundle
from proof_headless.models.manifest import Manifest, ModelEntry, Role
from proof_headless.models.render import CONTAINER_PORT
from proof_headless.spec.matrix import Kind, Matrix, Roles, Run

METRIC_JUDGE = "metric_judge"
"""The fixed role that judges for a metric, reasoning off unless the roles file says otherwise."""

PROVIDER = "openai_compatible"
"""Yield's provider for a chat model served by anything that speaks the OpenAI API (yield#143)."""

ENDPOINT_PATH: dict[Kind, str] = {
    Kind.CHAT: "/v1",
    Kind.EMBED: "/v1/embeddings",
    Kind.RERANK: "/v1/rerank",
    Kind.GUARDIAN: "",
}
"""Per kind, the path after `http://<name>:8000`. A chat model takes a base URL its client appends
to; the embedder and the reranker take the full URL, since which path is exactly what differs
between servers; the guardian takes the server's root and appends `/v1/chat/completions` itself."""

SLOT: dict[Kind, str] = {
    Kind.CHAT: "judge",
    Kind.EMBED: "embedder",
    Kind.RERANK: "reranker",
    Kind.GUARDIAN: "guardian",
}
"""Which field of a metric's config a role of each kind fills."""


class Refused(ValueError):
    """The specs cannot be rendered; the message names the run, role or piece at fault."""


class Connector(BaseModel):
    """The connector block once defaults and the run's overrides are merged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1)
    profile: str | None = None
    declared_capabilities: tuple[str, ...] = ()
    safe_mode: bool = True
    min_interval_seconds: float = Field(default=0.0, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)


class Kb(BaseModel):
    """Which of the bundle's brains grounds the run, and where the stack's containers reach it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brain: str = Field(min_length=1)
    registry: str = Field(min_length=1)


class Budget(BaseModel):
    """The run's ceilings. `max_tokens` is absent: Yield refuses it as unenforced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_target_calls: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class Receipt:
    """The receipt of the bundle's last complete load, indexed by kind and name."""

    pieces: dict[tuple[str, str], Loaded]

    @classmethod
    def read(cls, path: Path) -> Receipt:
        """The receipt at `path`, refused when the bundle was never loaded or its last load
        stopped: a stopped load's receipt can pin a catalogue version beside a brain digest from
        an earlier load, and a spec pinned to that mix measures a bundle nobody published."""
        try:
            receipt = loader.read_receipt(path)
        except (ValueError, KeyError, TypeError) as error:
            raise Refused(f"the receipt at {path} cannot be read: {error}") from None
        if receipt is None:
            raise Refused(
                f"no receipt at {path}: load the bundle with `proof bundle load` first, so the "
                f"specs pin what was published"
            )
        if not receipt.complete:
            raise Refused(
                f"the last load of {receipt.client} stopped "
                f"({receipt.error or 'no error recorded'}); load the bundle again"
            )
        return cls({(item.kind, item.name): item for item in receipt.loaded})

    def version(self, kind: Piece, name: str, why: str) -> int:
        item = self.pieces.get((kind, name))
        if item is None or item.version is None:
            raise Refused(f"{why}: {kind} {name} is not in the receipt; load the bundle again")
        return item.version

    def digest(self, kind: Piece, name: str, why: str) -> str:
        item = self.pieces.get((kind, name))
        if item is None or not item.digest:
            raise Refused(f"{why}: {kind} {name} has no digest in the receipt; load it again")
        return item.digest


@dataclass(frozen=True)
class Rendered:
    """One run's spec, and the facts the summary table shows."""

    run: Run
    spec: dict[str, Any]
    judge_model: str


class Renderer:
    """Joins one bundle, its matrix, its roles, its receipt and a models manifest."""

    def __init__(
        self, bundle: Bundle, matrix: Matrix, roles: Roles, receipt: Receipt, manifest: Manifest
    ) -> None:
        self.bundle = bundle
        self.matrix = matrix
        self.roles = roles
        self.receipt = receipt
        self.manifest = manifest

    def render(self) -> list[Rendered]:
        """Every run's spec, in matrix order; refused whole at the first problem."""
        return [self._run(run) for run in self.matrix.runs]

    def model(self, role: str, why: str) -> dict[str, Any]:
        """The `ModelSpec` for `role`, checked against the manifest and the pacing."""
        binding = self.roles.roles.get(role)
        if binding is None:
            raise Refused(f"{why}: no role {role!r} in the roles file or --bind")
        kind = self.roles.kind_of(role)
        entry = self._entry(binding.model, role)
        if Role(kind.value) not in entry.roles:
            raise Refused(
                f"role {role} needs a {kind} model, and {entry.name} serves "
                f"{sorted(r.value for r in entry.roles)}"
            )
        rate = self.roles.pacing.get(entry.name)
        if rate is None:
            raise Refused(f"role {role}: model {entry.name} has no pacing in the roles file")
        spec: dict[str, Any] = {
            "model": entry.served_name,
            "provider": PROVIDER,
            "endpoint": f"http://{entry.name}:{CONTAINER_PORT}{ENDPOINT_PATH[kind]}",
            "self_hosted": True,
            "requests_per_second": rate,
        }
        if kind is not Kind.CHAT:
            if binding.reasoning is not None or binding.reasoning_budget is not None:
                raise Refused(f"role {role} is {kind}, which has no reasoning to configure")
            return spec
        if binding.reasoning_budget is not None:
            spec["reasoning_budget"] = binding.reasoning_budget
        reasoning = self.roles.reasoning_of(role)
        if reasoning is not None:
            if entry.reasoning_switch is None:
                state = "on" if reasoning else "off"
                raise Refused(
                    f"role {role} needs reasoning {state}, and {entry.name} declares no "
                    f"reasoning_switch in the manifest to switch it with; set the switch, or "
                    f"`reasoning` on the role to leave it to the chat template"
                )
            else:
                spec["extra_body"] = {
                    "chat_template_kwargs": {entry.reasoning_switch: reasoning},
                }
        return spec

    def _entry(self, name: str, role: str) -> ModelEntry:
        try:
            return self.manifest.named(name)
        except KeyError as error:
            raise Refused(f"role {role}: {error.args[0]}") from None

    def _merged(self, run: Run, field: str) -> dict[str, Any]:
        merged: dict[str, Any] = {**getattr(self.matrix.defaults, field), **getattr(run, field)}
        return merged

    def _setting(self, run: Run, field: str) -> str | None:
        value: str | None = getattr(run, field) or getattr(self.matrix.defaults, field)
        return value

    def _run(self, run: Run) -> Rendered:
        why = f"run {run.id}"
        names = {entry.name for entry in self.bundle.catalogues}
        if run.catalogue not in names:
            raise Refused(
                f"{why}: unknown catalogue {run.catalogue!r}; the bundle has {sorted(names)}"
            )
        spec: dict[str, Any] = {"run_id": run.id}
        kb = self._kb(run, why)
        if kb is not None:
            spec["kb_ref"] = kb
        spec["kb_subjects"] = list(run.kb_subjects)
        if run.kb_subjects and kb is None:
            raise Refused(f"{why}: names kb_subjects and no brain (kb.brain)")
        catalogue = next(entry for entry in self.bundle.catalogues if entry.name == run.catalogue)
        spec["catalogues"] = [run.catalogue]
        spec["catalogue_versions"] = {
            run.catalogue: self.receipt.version(Piece.CATALOGUE, run.catalogue, why)
        }
        spec["plugins"] = list(run.plugins)
        spec["strategies"] = list(run.strategies)
        spec["dimensions"] = list(run.dimensions)
        mapping = self._setting(run, "metric_mapping")
        if mapping is not None:
            self._in_bundle(Piece.MAPPING, mapping, why)
            self.receipt.version(Piece.MAPPING, mapping, why)
            spec["metric_mapping"] = mapping
        spec["metric_ids"] = list(run.metric_ids)
        spec["contract_name"] = catalogue.contract
        spec["connector"] = self._connector(run, why)
        context = self._context()
        if context is not None:
            spec["context"] = context
        spec["judge"] = self.model("judge", why)
        if "generator" in self.roles.roles:
            spec["generator"] = self.model("generator", why)
        if "embedder" in self.roles.roles:
            spec["embedder"] = self.model("embedder", why)
        spec["attackers"] = {}
        for attacker, role in sorted(run.attackers.items()):
            if self.roles.kind_of(role) is not Kind.CHAT:
                raise Refused(f"{why}: attacker {attacker} is bound to {role}, which is not chat")
            spec["attackers"][attacker] = self.model(role, f"{why}, attacker {attacker}")
        spec["metrics"] = self._metrics(run, why)
        prior = self._setting(run, "realism_prior")
        if prior is not None:
            self._in_bundle(Piece.PRIOR, prior, why)
            self.receipt.version(Piece.PRIOR, prior, why)
            spec["realism_prior"] = prior
        spec["replicas"] = run.replicas
        spec["budget"] = self._budget(run, why)
        webhook = self._setting(run, "webhook_url")
        if webhook is not None:
            spec["webhook_url"] = webhook
        declaration = self._declaration(run, why)
        if declaration is not None:
            spec["target_declaration"] = declaration
        return Rendered(run, spec, spec["judge"]["model"])

    def _in_bundle(self, kind: Piece, name: str, why: str) -> None:
        entries = {
            Piece.PROFILE: self.bundle.profiles,
            Piece.PRIOR: self.bundle.priors,
            Piece.MAPPING: self.bundle.mappings,
        }[kind]
        names = sorted(entry.name for entry in entries)
        if name not in names:
            raise Refused(f"{why}: unknown {kind} {name!r}; the bundle has {names}")

    def _kb(self, run: Run, why: str) -> dict[str, str] | None:
        raw = self._merged(run, "kb")
        if not raw:
            return None
        try:
            kb = Kb.model_validate(raw)
        except ValueError as error:
            raise Refused(f"{why}: kb: {error}") from None
        brain = next((b for b in self.bundle.brains if b.name == kb.brain), None)
        if brain is None:
            names = sorted(b.name for b in self.bundle.brains)
            raise Refused(f"{why}: unknown brain {kb.brain!r}; the bundle has {names}")
        return {
            "registry": kb.registry,
            "repository": brain.repository,
            "digest": self.receipt.digest(Piece.BRAIN, brain.name, why),
        }

    def _connector(self, run: Run, why: str) -> dict[str, Any]:
        try:
            connector = Connector.model_validate(self._merged(run, "connector"))
        except ValueError as error:
            raise Refused(f"{why}: connector: {error}") from None
        if connector.secret_ref not in self.bundle.secrets:
            raise Refused(
                f"{why}: connector secret_ref {connector.secret_ref!r} is not one of the bundle's "
                f"secrets {list(self.bundle.secrets)}"
            )
        block: dict[str, Any] = {
            "kind": connector.kind,
            "endpoint": connector.endpoint,
            "secret_ref": connector.secret_ref,
        }
        if connector.profile is not None:
            self._in_bundle(Piece.PROFILE, connector.profile, why)
            block["profile"] = {
                "name": connector.profile,
                "version": self.receipt.version(Piece.PROFILE, connector.profile, why),
            }
        block["declared_capabilities"] = list(connector.declared_capabilities)
        block["safe_mode"] = connector.safe_mode
        block["min_interval_seconds"] = connector.min_interval_seconds
        if connector.max_retries is not None:
            block["max_retries"] = connector.max_retries
        if connector.options:
            block["options"] = connector.options
        return block

    def _context(self) -> dict[str, str] | None:
        domain = self.bundle.context.get("domain")
        if domain is None:
            return None
        context = {"language": self.bundle.language, "domain": domain}
        tone = self.bundle.context.get("tone")
        if tone is not None:
            context["tone"] = tone
        return context

    def _metrics(self, run: Run, why: str) -> dict[str, Any]:
        """Every metric the run lists, with exactly the models its `uses` names.

        A metric gets a judge only when its roles name a chat role, and that role must switch its
        reasoning off: metric judges ask for structured output, and the principle judge's
        reasoning would leave the call uncapped. No judge goes on a metric that uses none, because
        Yield's measure loop probes a declared judge whether the metric needs it or not, and a
        failed probe would fail a metric that never asks it anything.
        """
        metrics: dict[str, Any] = {}
        for metric_id in run.metric_ids:
            entry = self.matrix.metrics.get(metric_id)
            if entry is None:
                raise Refused(
                    f"{why}: metric {metric_id} has no [metrics.{metric_id}] table naming the "
                    f"roles it uses (`uses = []` for one that needs no model)"
                )
            config: dict[str, Any] = {}
            for role in entry.uses:
                slot = SLOT[self.roles.kind_of(role)]
                if slot == "judge" and self.roles.reasoning_of(role) is not False:
                    raise Refused(
                        f"{why}: metric {metric_id} uses {role}, whose reasoning is not off; a "
                        f"metric judges with a role that sets `reasoning = false`, such as "
                        f"{METRIC_JUDGE}"
                    )
                if slot in config:
                    raise Refused(f"metric {metric_id}: two of its roles fill its {slot}")
                config[slot] = self.model(role, f"{why}, metric {metric_id}")
            if entry.params:
                config["params"] = entry.params
            metrics[metric_id] = config
        return metrics

    def _budget(self, run: Run, why: str) -> dict[str, int]:
        raw = self._merged(run, "budget")
        if run.max_calls is not None:
            raw["max_target_calls"] = run.max_calls
        try:
            budget = Budget.model_validate(raw)
        except ValueError as error:
            raise Refused(f"{why}: budget: {error}") from None
        return {key: value for key, value in budget.model_dump().items() if value is not None}

    def _declaration(self, run: Run, why: str) -> str | None:
        name = self._setting(run, "declaration")
        if name is None:
            if len(self.bundle.declarations) != 1:
                return None
            name = self.bundle.declarations[0].path.name
        names = [declaration.path.name for declaration in self.bundle.declarations]
        if name not in names:
            raise Refused(f"{why}: unknown declaration {name!r}; the bundle has {names}")
        return self.receipt.digest(Piece.DECLARATION, name, why)


def _rendered_before(path: Path) -> bool:
    """Whether `path` is a spec a render wrote: JSON whose `run_id` is its file name."""
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("run_id") == path.stem


def write(rendered: list[Rendered], directory: Path) -> list[Path]:
    """One `<run id>.json` per spec under `directory`, which is created.

    Specs an earlier render wrote are removed first: one for a run since dropped from the matrix
    is pinned to an older receipt, and whatever submits the directory would send it. Only files
    that read as a spec named by their own run id are removed, so `--out` pointed at a directory
    holding anything else leaves that alone.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        if _rendered_before(stale):
            stale.unlink()
    written = []
    for item in rendered:
        path = directory / f"{item.run.id}.json"
        path.write_text(json.dumps(item.spec, indent=2, ensure_ascii=False) + "\n")
        written.append(path)
    return written
