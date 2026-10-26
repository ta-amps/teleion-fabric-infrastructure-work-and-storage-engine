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

NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"


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


class ChildSpec(_Model):
    brief: dict[str, Any]
    queue: str | None = Field(default=None, pattern=NAME_PATTERN)
    kind: str | None = Field(default=None, pattern=NAME_PATTERN)
    effect_class: EffectClass | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


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
