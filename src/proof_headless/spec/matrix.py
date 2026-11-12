"""The bundle's run matrix and role bindings: what each run measures, and who measures it.

Two files, kept apart because they change for different reasons. The matrix (`matrix.toml`) is
the engagement: which catalogue and strategies each run attacks, over which subjects of the
brain, how many times, measured by which metrics, against which connector. The roles
(`roles.toml`) are the appliance: which served model plays the judge, the generator, each
attacker, the embedder, the reranker and the guardian, and how fast each model may be asked.
Neither names a model id or an endpoint of a model; the models manifest does, and `render`
joins the three.

The defaults table holds what every run shares, and a run may override any of it. Connector,
knowledge base and budget are merged key by key, so a run that only paces the target more slowly
says only that.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUN_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$"
"""Yield's run id rule, spelled here rather than imported (ADR 0001 keeps the appliance free of
Yield's packages): a DNS label short enough that the driver's job name, `yield-driver-<id>`, fits
in 63 characters. A rendered id Yield would refuse is refused before it is written."""


class Kind(StrEnum):
    """What a role asks its model for, and so which manifest role the model must declare."""

    CHAT = "chat"
    EMBED = "embed"
    RERANK = "rerank"
    GUARDIAN = "guardian"


FIXED_ROLES: dict[str, Kind] = {
    "judge": Kind.CHAT,
    "metric_judge": Kind.CHAT,
    "generator": Kind.CHAT,
    "embedder": Kind.EMBED,
    "reranker": Kind.RERANK,
    "guardian": Kind.GUARDIAN,
}
"""The roles a run spec has a place for. Any other role name is an attacker's, and is chat."""

DEFAULT_REASONING: dict[str, bool] = {"judge": True, "metric_judge": False}
"""The principle judge grades with its reasoning on, under a budget; a metric judge asks for
structured output, and a reasoning model there has no cap on how long it thinks before answering,
so one uncapped call hangs a whole measure. Both are switched explicitly so the provenance says
which instrument each one was."""


class Binding(BaseModel):
    """One role: the manifest model that plays it, and how it is asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    """A model's `name` in the models manifest, never its served name or its weights' id."""
    kind: Kind | None = None
    """Only for a role outside `FIXED_ROLES`, and then it must be chat: such a role plays an
    attacker."""
    reasoning: bool | None = None
    """On or off through the model's `reasoning_switch`. Unset leaves the chat template's own
    default, except for the judges (`DEFAULT_REASONING`)."""
    reasoning_budget: int | None = Field(default=None, ge=1)


class Roles(BaseModel):
    """`roles.toml`: role bindings and the pacing of every model they name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pacing: dict[str, float] = Field(default_factory=dict)
    """Requests per second per manifest model. Per model rather than per role or per run: Yield
    paces a model through one Valkey bucket shared by every run that names it, and two specs that
    disagree on its rate would each believe the other's."""
    roles: dict[str, Binding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _kinds(self) -> Roles:
        for name, binding in self.roles.items():
            fixed = FIXED_ROLES.get(name)
            if fixed is not None and binding.kind not in (None, fixed):
                raise ValueError(f"role {name} is a {fixed} role; drop its kind")
            if fixed is None and binding.kind not in (None, Kind.CHAT):
                raise ValueError(f"role {name} is an attacker's and must be chat")
        for model, rate in self.pacing.items():
            if rate <= 0:
                raise ValueError(f"pacing for {model} must be positive, got {rate}")
        return self

    def kind_of(self, role: str) -> Kind:
        """The kind a role asks for: fixed by its name, or chat for an attacker's."""
        return FIXED_ROLES.get(role, Kind.CHAT)

    def reasoning_of(self, role: str) -> bool | None:
        """Whether the role switches its model's reasoning, after the judges' defaults."""
        binding = self.roles[role]
        return binding.reasoning if binding.reasoning is not None else DEFAULT_REASONING.get(role)

    def bound(self, extra: dict[str, str]) -> Roles:
        """These roles with `extra` (role -> model) rebinding or adding roles, from `--bind`."""
        roles = dict(self.roles)
        for role, model in extra.items():
            current = roles.get(role)
            roles[role] = (
                current.model_copy(update={"model": model}) if current else Binding(model=model)
            )
        return Roles(pacing=self.pacing, roles=roles)


class MetricEntry(BaseModel):
    """What one metric needs beyond the dataset: the roles it measures with, and its knobs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    uses: tuple[str, ...] = ()
    """Role names; each fills the metric's slot for its kind (a chat role its judge). Declared
    rather than left to Yield's fallback, because the fallback for a judge is the run's principle
    judge, with its reasoning on. A chat role here must switch reasoning off, and with none
    the metric gets no judge."""
    params: dict[str, Any] = Field(default_factory=dict)


class Overridable(BaseModel):
    """What the defaults set and a run may override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: dict[str, Any] = Field(default_factory=dict)
    """kind, endpoint, secret_ref, profile (a profile's name in the bundle), safe_mode,
    min_interval_seconds, max_retries, declared_capabilities, options."""
    kb: dict[str, Any] = Field(default_factory=dict)
    """brain (a brain's name in the bundle) and registry (as the stack's containers reach it)."""
    budget: dict[str, Any] = Field(default_factory=dict)
    webhook_url: str | None = None
    """Where Yield delivers the run's results. Unset is the stack's Lens receiver
    (`endpoints.LENS_WEBHOOK`)."""
    metric_mapping: str | None = None
    """A mapping's name in the bundle, or Yield's own `default` when unset."""
    realism_prior: str | None = None
    declaration: str | None = None
    """A declaration's file name (its SHA-256). Unset takes the bundle's only one, if it has one."""


class Run(Overridable):
    """One run of the matrix."""

    id: str = Field(pattern=RUN_ID_PATTERN)
    catalogue: str = Field(min_length=1)
    strategies: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    kb_subjects: tuple[str, ...] = ()
    replicas: int = Field(ge=1)
    max_calls: int | None = Field(default=None, ge=1)
    """The run's `budget.max_target_calls`."""
    metric_ids: tuple[str, ...] = Field(min_length=1)
    """Required, never left to Yield's mapping: the mapping would resolve metrics with no
    `[metrics.<id>]` table, and a judged one falls back to the run's principle judge, reasoning on.
    With the ids given, each has a table and so its own judge, reasoning off."""
    dimensions: tuple[str, ...] = Field(min_length=1)
    attackers: dict[str, str] = Field(default_factory=dict)
    """Attacker id, as the catalogue's delivery sidecar names it, to the role that plays it."""

    @model_validator(mode="after")
    def _something_to_attack(self) -> Run:
        if not (self.strategies or self.plugins):
            raise ValueError(f"run {self.id} names no strategy and no plugin")
        return self


class Matrix(BaseModel):
    """`matrix.toml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Overridable = Overridable()
    metrics: dict[str, MetricEntry] = Field(default_factory=dict)
    runs: tuple[Run, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_ids(self) -> Matrix:
        ids = [run.id for run in self.runs]
        duplicates = sorted({run_id for run_id in ids if ids.count(run_id) > 1})
        if duplicates:
            raise ValueError(f"two runs share an id: {duplicates}")
        return self


class MatrixInvalid(ValueError):
    """The matrix or the roles file does not parse or does not validate."""


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise MatrixInvalid(f"no file {path}") from None
    except tomllib.TOMLDecodeError as error:
        raise MatrixInvalid(f"{path} is not TOML: {error}") from None


def load_matrix(path: Path) -> Matrix:
    """Read and validate a run matrix."""
    try:
        return Matrix.model_validate(_toml(path))
    except ValueError as error:
        if isinstance(error, MatrixInvalid):
            raise
        raise MatrixInvalid(f"{path}: {error}") from None


def load_roles(path: Path) -> Roles:
    """Read and validate a roles file."""
    try:
        return Roles.model_validate(_toml(path))
    except ValueError as error:
        if isinstance(error, MatrixInvalid):
            raise
        raise MatrixInvalid(f"{path}: {error}") from None
