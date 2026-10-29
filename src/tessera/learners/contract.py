"""The common learner contract: spec nouns, config nouns and run verbs.

A learner spec is an ordinary versioned noun describing what one swappable learner
reads, writes, when it runs, its budget, how it must beat a baseline and who may read
its outputs. A config noun enables, shadows or disables specs per category, surface
or queue. A run is an ordinary private verb. None of these grant permissions.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tessera.models import NAME_PATTERN, NAMESPACE_PATTERN, RecordWrite, TaskSpec
from tessera.observations import REPORTING_HINT
from tessera.receipts import canonical_json

SPEC_CONTRACT: Literal["tessera.learner-spec.v1"] = "tessera.learner-spec.v1"
CONFIG_CONTRACT: Literal["tessera.learner-config.v1"] = "tessera.learner-config.v1"
RUN_CONTRACT = "tessera.learner-run.v1"
SPEC_STORE = "learn.specs"
Facet = Literal[
    "memory",
    "path_statistics",
    "routing_weights",
    "phrasing",
    "searchability",
    "shim",
    "decomposition",
    "preference",
    "training_export",
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
Text = Annotated[str, Field(min_length=1, max_length=600)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def value_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class NounRef(_Frozen):
    store: str = Field(pattern=NAME_PATTERN)
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    version: int = Field(ge=1)
    value_sha256: Digest


class Output(_Frozen):
    kind: Label
    category_id: str = Field(pattern=r"^noun\.[a-z_]+$")
    store: str = Field(pattern=NAME_PATTERN)


class Trigger(_Frozen):
    on_demand: bool = True
    schedule: Text | None = None
    new_observations: int | None = Field(default=None, ge=1, le=100_000)


class Budget(_Frozen):
    max_observations: int = Field(ge=1, le=20_000)
    max_runtime_seconds: int = Field(ge=1, le=3_600)
    max_output_bytes: int = Field(ge=1_024, le=1_048_576)
    model_calls: int = Field(default=0, ge=0)


class Evaluation(_Frozen):
    baseline: Text
    metric: Text
    method: Text
    min_evidence: int = Field(ge=1)
    keep_baseline_valid: Literal[True] = True


class Consumers(_Frozen):
    visibility_scope: Literal["private", "surface_only"] = "private"
    allowed_surfaces: list[str] = Field(default_factory=list, max_length=16)
    advice_only: Literal[True] = True


class VerbTemplate(_Frozen):
    kind: str = Field(pattern=r"^verb\.[a-z_]+$")
    runner: str | None = Field(default=None, pattern=r"^tessera\.learners\.[a-z_]+:[a-z_]+$")
    deterministic: bool


class LearnerSpec(_Frozen):
    contract: Literal["tessera.learner-spec.v1"] = SPEC_CONTRACT
    learner: Facet
    strategy: Label
    version: int = Field(ge=1, le=10_000)
    status: Literal["implemented", "proposed"]
    summary: Text
    reads: list[Label] = Field(min_length=1, max_length=8)
    writes: list[Output] = Field(min_length=1, max_length=8)
    trigger: Trigger
    budget: Budget
    evaluation: Evaluation
    consumers: Consumers = Consumers()
    verb: VerbTemplate
    limitations: list[Text] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def runnable(self) -> LearnerSpec:
        if self.status == "implemented" and self.verb.runner is None:
            raise ValueError("an implemented learner names its deterministic runner")
        if self.verb.deterministic and self.budget.model_calls:
            raise ValueError("a deterministic learner has no model-call budget")
        return self

    @property
    def key(self) -> str:
        return f"{self.learner}:{self.strategy}:v{self.version}"

    def record(self, namespace: str) -> RecordWrite:
        """A private spec noun; storing it activates nothing."""
        return RecordWrite(
            namespace=namespace,
            store=SPEC_STORE,
            key=self.key,
            value=self.model_dump(mode="json"),
            expected_version=None,
            visibility_scope="private",
            tags=[
                "category:noun.skill_pack",
                "purpose:learner_spec",
                f"learner:{self.learner}",
                f"strategy:{self.strategy}",
                f"status:{self.status}",
            ],
        )


class Activation(_Frozen):
    learner: Facet
    spec: NounRef
    mode: Literal["active", "shadow", "off"]
    categories: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=32)
    surfaces: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=32)


class LearnerConfig(_Frozen):
    """Which learner specs are active, shadowed or off for which categories and surfaces."""

    contract: Literal["tessera.learner-config.v1"] = CONFIG_CONTRACT
    activations: list[Activation] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unambiguous(self) -> LearnerConfig:
        seen: set[tuple[str, str, str]] = set()
        for item in self.activations:
            if item.mode != "active":
                continue
            for category in item.categories:
                for surface in item.surfaces:
                    slot = (item.learner, category, surface)
                    if slot in seen:
                        raise ValueError("two active specs claim the same learner scope")
                    seen.add(slot)
        return self

    def select(self, learner: str, category: str, surface: str) -> list[Activation]:
        """Active then shadow activations for one scope; exact matches before wildcards."""

        def rank(item: Activation) -> tuple[int, int]:
            exact = (category in item.categories) + (surface in item.surfaces)
            return (0 if item.mode == "active" else 1, -exact)

        matches = [
            item
            for item in self.activations
            if item.learner == learner
            and item.mode != "off"
            and ({category, "*"} & set(item.categories))
            and ({surface, "*"} & set(item.surfaces))
        ]
        return sorted(matches, key=rank)


class RunRequest(_Frozen):
    """One bounded learner run; its identity pins the spec and the starting state version."""

    version: int = Field(default=1, ge=1, le=1)
    namespace: str = Field(pattern=NAMESPACE_PATTERN)
    queue: str = Field(pattern=NAME_PATTERN)
    spec: NounRef
    source_queue: str = Field(pattern=NAME_PATTERN)
    source_kind: str | None = Field(default=None, pattern=NAME_PATTERN)
    output_store: str = Field(pattern=NAME_PATTERN)
    state_version: int = Field(default=0, ge=0)
    max_observations: int | None = Field(default=None, ge=1, le=20_000)
    options: dict[str, Any] = Field(default_factory=dict)


def run_spec(request: RunRequest, spec: LearnerSpec) -> TaskSpec:
    """A private run verb for an already pinned spec; enqueueing it is a separate act."""
    if spec.status != "implemented" or spec.verb.runner is None:
        raise ValueError("only implemented learner specs can be run")
    if value_digest(spec.model_dump(mode="json")) != request.spec.value_sha256:
        raise ValueError("spec value differs from its pinned digest")
    limit = min(
        request.max_observations or spec.budget.max_observations, spec.budget.max_observations
    )
    brief = {
        "contract": RUN_CONTRACT,
        "category_id": spec.verb.kind,
        "goal": f"Run learner {spec.key} deterministically over permitted execution "
        f"observations of queue {request.source_queue} and write its versioned outputs.",
        "learner": spec.learner,
        "strategy": spec.strategy,
        "spec_version": spec.version,
        "spec": request.spec.model_dump(),
        "source": {
            "queue": request.source_queue,
            "kind": request.source_kind,
            "max_observations": limit,
        },
        "output_store": request.output_store,
        "state_version": request.state_version,
        "options": request.options,
        "consumers": spec.consumers.model_dump(),
        "evaluation": spec.evaluation.model_dump(),
        "execution": {
            "worker": "external",
            "runner": spec.verb.runner,
            "deterministic": spec.verb.deterministic,
            "model_calls": spec.budget.model_calls,
            "server_fallback": False,
            "max_runtime_seconds": spec.budget.max_runtime_seconds,
        },
        "required": [
            "Recheck the pinned spec digest and current grants. Read only permitted observations.",
            "Write outputs with expected versions; a conflict means another run advanced the state.",
            "Outputs are advice and evidence; they grant no permission and override no caller choice.",
            "A no_change or keep-baseline result is valid. Never call a model from a deterministic run.",
        ],
        "reporting": REPORTING_HINT,
    }
    digest = value_digest({"namespace": request.namespace, "queue": request.queue, "brief": brief})
    return TaskSpec(
        namespace=request.namespace,
        queue=request.queue,
        task_id="learn_" + digest,
        idempotency_key="learn:" + digest,
        kind=spec.verb.kind,
        effect_class="write-internal",
        visibility_scope="private",
        brief=brief,
        tags=[
            "category:" + spec.verb.kind,
            "purpose:learner_run",
            f"learner:{spec.learner}",
            f"strategy:{spec.strategy}",
        ],
    )
