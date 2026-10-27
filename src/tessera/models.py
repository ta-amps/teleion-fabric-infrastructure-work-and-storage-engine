"""Records that cross Tessera's boundary: what callers send and what every backend returns."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EffectClass = Literal["read", "write-internal", "write-repo", "public"]
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
SURFACE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,47}$"
BUCKET_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,511}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class TaskSpec(_Model):
    queue: str = Field(pattern=NAME_PATTERN)
    brief: dict[str, Any]
    task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
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
    after: list[int] = Field(
        default_factory=list,
        max_length=MAX_DEPENDENCIES,
        description="Indexes of earlier children in the same split that must resolve first (waves).",
    )


class ChildResult(_Model):
    task_id: str
    state: TaskState
    ok: bool | None = None
    output: dict[str, Any] | None = None
    receipt_hash: str | None = None


class Lease(_Model):
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
    output: dict[str, Any]
    error: str | None
    receipt_hash: str
    state_after: TaskState
    created_at: float


class TaskView(_Model):
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
    wait_reason: WaitReason | None = None
    pending_deps: int = 0
    depends_on: list[str] = Field(default_factory=list)
    wake_at: float | None = None
    parked_by: str | None = None
    parks: int = 0  # how many times the task has been parked; a resume can name the park it answers


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


class RecordVersion(_Model):
    store: str
    key: str
    version: int
    value: Any
    created_at: float


class RecordWrite(_Model):
    store: str = Field(pattern=NAME_PATTERN)
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    value: Any
    expected_version: int | None = Field(
        default=None, description="Head version the caller read; None means the key must not exist."
    )


class StoreSummary(_Model):
    store: str
    records: int = 0
    updated_at: float


class BucketConfig(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default="", max_length=500)


class BucketInfo(_Model):
    name: str
    description: str = ""
    objects: int = 0
    bytes: int = 0
    created_at: float
    updated_at: float


class ObjectMeta(_Model):
    bucket: str
    key: str
    version: int
    sha256: str
    size: int
    content_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float


class NounScent(_Model):
    """Progressive L0 scent summary (<=150 tokens) returned by 0-call search before L1/L2 reads."""

    kind: Literal["record", "object"]
    namespace: str
    key: str
    version: int
    title: str
    l0_summary: str
    tags: list[str] = Field(default_factory=list)
    size_bytes: int
    sha256: str | None = None
    score: float
    updated_at: float


class FeedEntry(_Model):
    """Monotonic cursor-sequenced delta entry in a live feed stream."""

    seq: int
    feed: str
    kind: str = "delta"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float


class FeedSummary(_Model):
    feed: str
    entries: int = 0
    latest_seq: int = 0
    updated_at: float


class ReflexHit(_Model):
    """Matched muscle-memory reflex or fuzzy prior step recipe."""

    reflex_id: str
    pattern: str
    tier: str
    verified_hits: int
    exact_hit: bool
    score: float
    l0_summary: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    version: int


class ReflexMatch(_Model):
    """Dual-resolution reflex recall paired with negative-knowledge dead-end warnings."""

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
