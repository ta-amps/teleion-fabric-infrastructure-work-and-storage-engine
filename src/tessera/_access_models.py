"""Public access response records shared by generated HTTP/OpenAPI descriptions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from tessera.grants import KeyGrant
from tessera.models import Event, TaskView


class PrincipalView(BaseModel):
    principal_kind: Literal["anonymous", "keyed", "service_binding"]
    principal_id: str
    session_id: str | None = None
    key_id: str | None = None
    account_id: str | None = None
    namespace: str
    key_prefix: str | None = None
    scopes: list[str]
    queues: list[str]
    kinds: list[str]
    effect_classes: list[str]
    labels: dict[str, Any]
    quotas: dict[str, int] | None = None
    quota_reason: str | None = None
    expires_at: float


class AnonymousSession(PrincipalView):
    session_token: str = Field(
        description="Shown once; send in Authorization: Bearer, never in a URL."
    )
    persisted: bool
    retention_expires_at: float


class AccessEvent(BaseModel):
    seq: int
    request_id: str
    ts: float
    namespace: str
    principal_kind: str
    principal_id: str | None = None
    session_id: str | None = None
    key_id: str | None = None
    account_id: str | None = None
    ip_hash: str | None = None
    ua_hash: str | None = None
    surface: str | None = None
    route_or_tool: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    receipt_id: str | None = None
    status: int | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    error_code: str | None = None
    labels: dict[str, Any]
    scopes_used: list[str]


class AccessCounter(BaseModel):
    """Per-minute count of denied, failed, no-op or pre-check requests for one principal."""

    window: int
    window_start: float
    window_seconds: int
    principal_id: str
    principal_kind: str
    namespace: str
    action: str
    outcome: str
    count: int
    first_ts: float
    last_ts: float


class AuditEvents(BaseModel):
    events: list[AccessEvent]
    counters: list[AccessCounter] = []
    counters_note: str | None = None


class PublicEvent(BaseModel):
    ts: float
    principal_kind: str
    action: str
    resource_type: str
    status: int
    labels: dict[str, Any]


class PublicActivity(BaseModel):
    events: list[PublicEvent]


class IssuedKey(KeyGrant):
    key: str = Field(description="Shown once; only its hash is stored.")


class KeyList(BaseModel):
    keys: list[KeyGrant]


class ClaimEvents(BaseModel):
    events: list[Event]
    cursor: int
    streaming: Literal["long-poll"]


class Verbs(BaseModel):
    verbs: list[TaskView]
