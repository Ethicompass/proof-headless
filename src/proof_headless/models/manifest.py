"""The models manifest: which models this host serves, with what, and for which roles.

The manifest is the only place a model id, an image or a vLLM flag is written (ADR 0001). It is
YAML because operators edit it by hand on the appliance, and it is validated in full before
anything starts: a typo found after three models loaded is twenty minutes lost.

Memory is declared, not discovered. vLLM claims `gpu_memory_utilization` of the device's memory
when a server starts, and several servers on one device each claim their own share; on unified
memory (a DGX Spark) that share also comes out of what the operating system and the stack have
left. So each model states its share and the manifest states the ceiling the shares must fit
under, and the arithmetic is checked here, before a container exists.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
"""A model's name is its compose service and its host name on the models network."""


class Role(StrEnum):
    """What a run may use a served model for. A role is bound to models by `proof spec render`."""

    CHAT = "chat"
    EMBED = "embed"
    RERANK = "rerank"
    GUARDIAN = "guardian"


class Api(StrEnum):
    """Which endpoint a model answers on, so `proof doctor` knows how to ask it."""

    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"
    CLASSIFY = "classify"


class Mount(BaseModel):
    """A host file or directory a model needs inside its container, read-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Path
    """Relative paths resolve against the manifest's directory."""
    target: str


class ModelEntry(BaseModel):
    """One served model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    hf_id: str = Field(min_length=1)
    served_name: str = Field(min_length=1)
    """What a request's `model` field says. Run specs bind to this, never to `hf_id`."""
    image: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    """The host port, bound to loopback. Containers reach the model by name on port 8000."""
    gpu_memory_utilization: float = Field(gt=0, lt=1)
    roles: frozenset[Role] = Field(min_length=1)
    api: Api
    args: tuple[str, ...] = ()
    """Further `vllm serve` flags, verbatim, after the ones `proof` sets."""
    entrypoint: tuple[str, ...] = ("vllm", "serve")
    gated: bool = False
    """Whether downloading the weights needs `HF_TOKEN`, from the secrets file."""
    mounts: tuple[Mount, ...] = ()
    start_period_seconds: int = Field(default=1200, ge=0)
    """How long the health check waits before counting failures: download plus load plus
    compilation, which for a 30B model on first start is tens of minutes."""
    reasoning_switch: str | None = None
    """The `chat_template_kwargs` key that turns the model's reasoning on and off, when it has
    one (`enable_thinking` for the Nemotron chat templates). Run specs use it to ask one served
    model with reasoning on for the control judge and off for structured output; `proof doctor`
    checks that the switch does what it says."""

    @field_validator("args")
    @classmethod
    def _no_flag_proof_sets(cls, args: tuple[str, ...]) -> tuple[str, ...]:
        owned = {"--served-model-name", "--port", "--host", "--gpu-memory-utilization"}
        clashes = sorted({arg.split("=", 1)[0] for arg in args} & owned)
        if clashes:
            raise ValueError(f"{clashes} are set by proof from the manifest's fields; drop them")
        return args


class Manifest(BaseModel):
    """Every model the host serves, and the share of memory they may claim together."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    memory_budget: float = Field(default=0.85, gt=0, le=1)
    """The ceiling on the sum of the models' shares. What is left is the operating system's and
    the stack's: Yield, Lens, MinIO and Valkey run on the same memory on a unified-memory host."""
    network: str = Field(default="proof-models", pattern=NAME_PATTERN)
    hf_cache: Path = Path("~/.cache/huggingface")
    models: tuple[ModelEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_and_within_budget(self) -> Manifest:
        for field in ("name", "port", "served_name"):
            seen = [getattr(model, field) for model in self.models]
            duplicates = sorted({str(value) for value in seen if seen.count(value) > 1})
            if duplicates:
                raise ValueError(f"two models share a {field}: {duplicates}")
        claimed = self.claimed()
        if claimed > self.memory_budget + 1e-9:
            raise ValueError(
                f"the models claim {claimed:.2f} of memory together and the budget is "
                f"{self.memory_budget:.2f}; lower a gpu_memory_utilization or raise memory_budget"
            )
        return self

    def claimed(self) -> float:
        """The sum of the models' memory shares."""
        return sum(model.gpu_memory_utilization for model in self.models)

    def named(self, name: str) -> ModelEntry:
        """The model called `name`, or a `KeyError` that lists the ones there are."""
        for model in self.models:
            if model.name == name:
                return model
        raise KeyError(f"no model {name!r}; the manifest has {[m.name for m in self.models]}")


class ManifestInvalid(ValueError):
    """The manifest file does not parse or does not validate."""


def load(path: Path) -> Manifest:
    """Read and validate a manifest; mount sources resolve against its directory."""
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ManifestInvalid(f"no manifest at {path}") from None
    except yaml.YAMLError as error:
        raise ManifestInvalid(f"{path} is not YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ManifestInvalid(f"{path} must be a mapping with a `models` list")
    try:
        manifest = Manifest.model_validate(raw)
    except ValueError as error:
        raise ManifestInvalid(f"{path}: {error}") from None
    base = path.parent
    return manifest.model_copy(
        update={
            "models": tuple(
                model.model_copy(
                    update={
                        "mounts": tuple(
                            mount.model_copy(update={"source": (base / mount.source).resolve()})
                            for mount in model.mounts
                        )
                    }
                )
                for model in manifest.models
            )
        }
    )
