"""Records that cross Tessera's boundary: what callers send and what every backend returns."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EffectClass = Literal["read", "write-internal", "write-repo", "public"]
VisibilityScope = Literal["private", "surface_only", "allowlist_surfaces", "public_commons"]
VISIBILITY_SCOPES: frozenset[str] = frozenset(
    {"private", "surface_only", "allowlist_surfaces", "public_commons"}
)
TaskState = Literal["ready", "leased", "waiting", "done", "dead", "cancelled"]
JobStatus = Literal[
    "queued", "in_progress", "completed", "failed", "cancelled", "incomplete", "cancel_requested"
]
TERMINAL_STATES: frozenset[str] = frozenset({"done", "dead", "cancelled"})
# Effect classes from narrowest to broadest; a split child may not be broader than its parent.
EFFECT_RANK: dict[str, int] = {"read": 0, "write-internal": 1, "write-repo": 2, "public": 3}
# Why a task is `waiting`: for its split children, for the tasks it depends on, or parked for input.
WaitReason = Literal["children", "dependencies", "input"]
MAX_DEPENDENCIES = 100
MAX_PARK_SECONDS = 2_592_000  # 30 days

NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
NAMESPACE_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
SURFACE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,47}$"
BUCKET_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,511}$"


def normalize_namespace(namespace: str | None) -> str:
    """Normalize an isolation namespace; None or blank maps to 'main'."""
    if namespace is None:
        return "main"
    clean = str(namespace).strip()
    return clean if clean else "main"


def normalize_surface(surface: str | None) -> str:
    """Normalize a surface identifier such as 'eranos' or 'surface:eranos' -> 'surface:eranos'."""
    if surface is None:
        return "surface:tessera"
    clean = str(surface).strip().lower()
    if not clean:
        return "surface:tessera"
    if clean.startswith("surface:"):
        slug = clean[len("surface:") :].strip()
        return f"surface:{slug}" if slug else "surface:tessera"
    return f"surface:{clean}"


def surface_slug(surface: str | None) -> str:
    """Extract the bare surface slug ('surface:eranos' -> 'eranos')."""
    return normalize_surface(surface).split(":", 1)[1]


def normalize_surface_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list | tuple | set):
        raise ValueError("allowed_surfaces must be a list")
    out: list[str] = []
    for item in values:
        raw = str(item).strip()
        if raw:
            norm = normalize_surface(raw)
            if norm not in out:
                out.append(norm)
    return out


def normalize_cohorts(values: Any) -> list[str]:
    if values is None:
        return ["all"]
    if not isinstance(values, list | tuple | set):
        raise ValueError("target_cohorts must be a list")
    out: list[str] = []
    for item in values:
        clean = str(item).strip()
        if clean and clean not in out:
            out.append(clean)
    return out or ["all"]


def normalize_tags(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list | tuple | set):
        raise ValueError("tags must be a list")
    out: list[str] = []
    for item in values:
        clean = str(item).strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def can_view_governed(
    *,
    visibility_scope: str,
    producer_surface: str,
    allowed_surfaces: list[str] | tuple[str, ...],
    owner_key_id: str | None,
    tags: list[str] | tuple[str, ...],
    viewer_surface: str | None = None,
    viewer_key_id: str | None = None,
    is_admin: bool = False,
    direct_get: bool = False,
    include_restricted: bool = False,
) -> bool:
    """Layer 1 server-side governance lens: quarantine hiding + visibility_scope check."""
    norm_prod = normalize_surface(producer_surface)
    norm_viewer = normalize_surface(viewer_surface) if viewer_surface else None
    if "status:quarantined" in tags:
        if direct_get:
            return bool(is_admin or (norm_viewer is not None and norm_viewer == norm_prod))
        return bool(is_admin and include_restricted)
    if is_admin:
        return True
    if visibility_scope == "public_commons":
        return True
    if visibility_scope == "private":
        if viewer_key_id is not None and owner_key_id is not None:
            return viewer_key_id == owner_key_id
        if owner_key_id is None and norm_viewer is not None:
            return norm_viewer == norm_prod
        return False
    if visibility_scope == "surface_only":
        return norm_viewer is not None and norm_viewer == norm_prod
    if visibility_scope == "allowlist_surfaces":
        if norm_viewer is None:
            return False
        allowed = {norm_prod, *(normalize_surface(s) for s in allowed_surfaces)}
        return norm_viewer in allowed
    return False


def cohort_matches(
    target_cohorts: list[str] | tuple[str, ...],
    cohort: str | None = None,
    *,
    is_admin: bool = False,
    include_restricted: bool = False,
) -> bool:
    """Layer 2 cohort filter: ['all'] matches default and any ?cohort=X; restricted cohorts
    without 'all' match only explicit ?cohort=X (or admin with include_restricted=True)."""
    cohorts = list(target_cohorts) if target_cohorts else ["all"]
    req = (cohort or "").strip()
    if not req:
        return ("all" in cohorts) or (is_admin and include_restricted)
    if "all" in cohorts or req in cohorts:
        return True
    alt = req[len("cohort:") :] if req.startswith("cohort:") else f"cohort:{req}"
    return alt in cohorts


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _GovernedModel(_Model):
    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    producer_surface: str = "surface:tessera"
    visibility_scope: VisibilityScope = "public_commons"
    allowed_surfaces: list[str] = Field(default_factory=list)
    target_cohorts: list[str] = Field(default_factory=lambda: ["all"])
    tags: list[str] = Field(default_factory=list)
    owner_key_id: str | None = None

    @field_validator("namespace", mode="before")
    @classmethod
    def _validate_namespace(cls, v: Any) -> str:
        return normalize_namespace(v)

    @field_validator("producer_surface", mode="before")
    @classmethod
    def _validate_producer_surface(cls, v: Any) -> str:
        return normalize_surface(v)

    @field_validator("allowed_surfaces", mode="before")
    @classmethod
    def _validate_allowed_surfaces(cls, v: Any) -> list[str]:
        return normalize_surface_list(v)

    @field_validator("target_cohorts", mode="before")
    @classmethod
    def _validate_target_cohorts(cls, v: Any) -> list[str]:
        return normalize_cohorts(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, v: Any) -> list[str]:
        return normalize_tags(v)


class QueueConfig(_Model):
    """A queue's lease and retry settings (SPEC §4). Defaults are the plan's starting values."""

    name: str = Field(pattern=NAME_PATTERN)
    lease_seconds: int = Field(default=600, ge=1, le=3600)
    heartbeat_seconds: int = Field(default=60, ge=1, le=600)
    miss_limit: int = Field(default=3, ge=1, le=10)
    max_attempts: int = Field(default=3, ge=1, le=20)
    backoff_base_seconds: float = Field(default=30.0, ge=0)
    backoff_cap_seconds: float = Field(default=900.0, ge=0)
    weight: int = Field(default=1, ge=1)


class TaskSpec(_GovernedModel):
    queue: str = Field(pattern=NAME_PATTERN)
    brief: dict[str, Any]
    task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    idempotency_key: str | None = Field(default=None, max_length=256)
    kind: str = Field(default="task", pattern=NAME_PATTERN)
    effect_class: EffectClass = "write-internal"
    priority: int = Field(default=50, ge=0, le=100)
    not_before: float | None = None
    depends_on: list[str] = Field(
        default_factory=list,
        max_length=MAX_DEPENDENCIES,
        description="Existing task ids; the task waits until every one is done, dead or cancelled.",
    )


class ChildSpec(_Model):
    brief: dict[str, Any]
    queue: str | None = Field(default=None, pattern=NAME_PATTERN)
    kind: str | None = Field(default=None, pattern=NAME_PATTERN)
    effect_class: EffectClass | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    tags: list[str] = Field(default_factory=list)
    after: list[int] = Field(
        default_factory=list,
        max_length=MAX_DEPENDENCIES,
        description="Indexes of earlier children in the same split that must resolve first (waves).",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_child_tags(cls, v: Any) -> list[str]:
        return normalize_tags(v)


class ChildResult(_Model):
    task_id: str
    state: TaskState
    ok: bool | None = None
    output: Any = None
    receipt_hash: str | None = None


class Lease(_Model):
    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    task_id: str
    queue: str
    fencing_token: int
    worker: str
    attempt: int
    kind: str
    effect_class: EffectClass
    brief: dict[str, Any]
    claimed_at: float
    expires_at: float
    hard_expires_at: float
    heartbeat_seconds: int
    children: list[ChildResult] = Field(default_factory=list)
    dependencies: list[ChildResult] = Field(default_factory=list)
    inputs: list[dict[str, Any]] = Field(default_factory=list)


class LeaseInfo(_Model):
    task_id: str
    fencing_token: int
    worker: str
    key_id: str | None
    expires_at: float
    hard_expires_at: float


class Receipt(_Model):
    task_id: str
    fencing_token: int
    idempotency_key: str
    ok: bool
    receipt_hash: str
    state_after: TaskState
    created_at: float
    replayed: bool = False


class ResultView(_Model):
    task_id: str
    fencing_token: int
    idempotency_key: str
    ok: bool
    output: Any = None
    error: str | None
    receipt_hash: str
    state_after: TaskState
    created_at: float
    completed_by_key: str | None = None


TaskResult = ResultView


class TaskView(_GovernedModel):
    task_id: str
    queue: str
    parent_id: str | None
    state: TaskState
    kind: str
    effect_class: EffectClass
    priority: int
    brief: dict[str, Any]
    attempts: int
    failures: int
    fencing_token: int
    not_before: float
    pending_children: int
    created_at: float
    updated_at: float
    idempotency_key: str | None = None
    wait_reason: WaitReason | None = None
    pending_deps: int = 0
    depends_on: list[str] = Field(default_factory=list)
    wake_at: float | None = None
    parked_by: str | None = None
    parks: int = 0  # how many times the task has been parked; a resume can name the park it answers


Task = TaskView


class QueueCounts(_Model):
    ready: int = 0
    leased: int = 0
    waiting: int = 0
    done: int = 0
    dead: int = 0
    cancelled: int = 0


class QueueHealth(_Model):
    name: str
    counts: QueueCounts
    oldest_ready_age_seconds: float | None
    expired_leases: int


class Health(_Model):
    ok: bool
    version: str
    time: float
    db_ok: bool
    reaper_last_run: float | None
    queues: list[QueueHealth]


class Event(_Model):
    seq: int
    ts: float
    type: str
    task_id: str | None
    queue: str | None
    worker: str | None
    fencing_token: int | None
    detail: dict[str, Any]


class Job(_Model):
    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    job_id: str
    task_id: str
    fencing_token: int
    model: str
    status: JobStatus
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    created_at: float
    updated_at: float


class RecordVersion(_GovernedModel):
    store: str
    key: str
    version: int
    value: Any
    created_at: float


Record = RecordVersion


class RecordWrite(_GovernedModel):
    store: str = Field(pattern=NAME_PATTERN)
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    value: Any
    expected_version: int | None = Field(
        default=None, description="Head version the caller read; None or 0 means create-only."
    )


class StoreSummary(_Model):
    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    store: str
    records: int = 0
    updated_at: float


class BucketConfig(_GovernedModel):
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default="", max_length=500)


class BucketInfo(_GovernedModel):
    name: str
    description: str = ""
    objects: int = 0
    bytes: int = 0
    created_at: float
    updated_at: float


class ObjectMeta(_GovernedModel):
    bucket: str
    key: str
    version: int
    sha256: str
    size: int
    content_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float


class NounScent(_GovernedModel):
    """Progressive L0 scent summary (<=150 tokens) returned by 0-call search before L1/L2 reads."""

    kind: Literal["record", "object"]
    container: str = ""
    key: str
    version: int
    title: str
    l0_summary: str
    size_bytes: int
    sha256: str | None = None
    score: float
    updated_at: float


class FeedEntry(_GovernedModel):
    """Monotonic cursor-sequenced delta entry in a live feed stream."""

    seq: int
    feed: str
    kind: str = "delta"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float


class FeedSummary(_Model):
    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    feed: str
    entries: int = 0
    latest_seq: int = 0
    updated_at: float


class ReflexHit(_GovernedModel):
    """Matched muscle-memory reflex or fuzzy prior step recipe."""

    reflex_id: str
    pattern: str
    tier: str
    verified_hits: int
    exact_hit: bool
    score: float
    l0_summary: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    version: int


class ReflexMatch(_Model):
    """Dual-resolution reflex recall paired with negative-knowledge dead-end warnings."""

    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    query: str
    recommended_tier: Literal["0_call_reflex", "hybrid_1_call", "agent_decompose"]
    hits: list[ReflexHit] = Field(default_factory=list)
    dead_ends: list[NounScent] = Field(default_factory=list)


class HandshakeRequest(_Model):
    """Surface Handshake v1.0 negotiation request sent by frontends, services, or harnesses."""

    protocol_version: str = Field(default="1.0", max_length=16)
    surface_id: str | None = Field(default=None, pattern=SURFACE_ID_PATTERN)
    surface_version: str = Field(default="0.1.0", max_length=64)
    requested_max_chars: int = Field(default=4000, ge=0, le=10_000_000)
    requested_queues: list[str] = Field(default_factory=list, max_length=128)
    requested_stores: list[str] = Field(default_factory=list, max_length=128)


class HandshakeResponse(_Model):
    """Negotiated Surface Handshake v1.0 receipt with clamped limits and isolated namespace."""

    namespace: str = Field(default="main", pattern=NAMESPACE_PATTERN)
    protocol_version: str = "1.0"
    backend: str = "tessera"
    backend_version: str
    surface_id: str
    isolated_store_prefix: str
    shared_read_stores: list[str] = Field(default_factory=list)
    clamped_max_chars: int
    max_output_bytes: int
    heartbeat_seconds: int
    allowed_queues: list[str] = Field(default_factory=list)
    store_scope: bool
    transactional_db_exempt_from_middleware: bool = True
    capabilities: list[str] = Field(default_factory=list)
    handshake_id: str
