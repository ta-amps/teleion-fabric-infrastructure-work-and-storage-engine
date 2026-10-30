"""What a worker key may do: its queues, kinds, effect classes and scopes, and the rules that narrow a
request to them. Pure policy with no storage, so the store, the service, the MCP door and the pages all
read it from here. No key may hold the `public` effect class."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

from pydantic import Field

from tessera.models import NAMESPACE_PATTERN, SURFACE_ID_PATTERN, _Model

EFFECTS = ("read", "write-internal", "write-repo")
SCOPES = frozenset(
    {"admin", "store", "nouns:read", "verbs:claim", "verbs:submit", "cas:write", "admin:keys"}
)
PROTECTED_SURFACES: tuple[str, ...] = ("shamash", "eranos", "herma", "kothar", "kinema")
_SURFACE_SCOPE_RE = re.compile(rf"^surface:({SURFACE_ID_PATTERN[1:-1]})$")

# Open verbs. A verb's producer may open it to tracked anonymous sessions by tagging it
# `open:anonymous`. Such sessions may then claim, heartbeat and submit it in the shared namespace.
# Review, approval and publication verbs stay closed whatever their tags say, and an open verb's
# effect class stays within the anonymous grant. Nothing else in the shared namespace opens.
OPEN_VERB_TAG = "open:anonymous"
OPEN_VERB_NAMESPACE = "main"
CLOSED_TO_ANONYMOUS: frozenset[str] = frozenset({"eq03", "frontier.review", "frontier.verify"})
OPEN_VERB_EFFECTS: frozenset[str] = frozenset({"read", "write-internal"})


# Requester keys. A self-serve sign-up mints a key that writes nouns only in its own `org.<id>`
# namespace and files request verbs into one request queue of the shared namespace, readable by the
# surfaces that answer requests. It reads its own request verbs there and nothing else.
REQUEST_QUEUE = "eq01"
REQUEST_KIND = "request"
REQUEST_SURFACES: tuple[str, ...] = ("surface:eranos",)
REQUESTER_STORE_PREFIX = "org."


def open_to_anonymous(queue: str, tags: Sequence[str], effect_class: str) -> bool:
    """True when a verb's producer opened it and it is not a review or publication verb."""
    return (
        OPEN_VERB_TAG in tags
        and queue not in CLOSED_TO_ANONYMOUS
        and effect_class in OPEN_VERB_EFFECTS
    )


def is_valid_scope(scope: str) -> bool:
    return (
        scope in SCOPES
        or bool(_SURFACE_SCOPE_RE.fullmatch(scope))
        or scope == "namespace:*"
        or (scope.startswith("namespace:") and bool(re.fullmatch(NAMESPACE_PATTERN, scope[10:])))
    )


class KeyGrant(_Model):
    """What a key may do. Queue patterns are shell-style globs: `ingest.*`, `q0?`, `*`."""

    key_id: str
    label: str
    queues: list[str]
    kinds: list[str] = Field(default_factory=lambda: ["*"])
    effect_classes: list[str] = Field(default_factory=lambda: list(EFFECTS))
    scopes: list[str] = Field(default_factory=list)
    created_at: float
    expires_at: float
    revoked_at: float | None = None
    principal_kind: str = "keyed"
    principal_id: str | None = None
    session_id: str | None = None
    account_id: str | None = None
    namespace: str = "main"
    key_prefix: str | None = None
    labels: dict[str, object] = Field(default_factory=dict)
    quota_tier: str = "standard"
    last_used_at: float | None = None
    purge_at: float | None = None

    @property
    def anonymous(self) -> bool:
        return self.principal_kind == "anonymous"

    @property
    def requester(self) -> bool:
        return self.principal_kind == "requester"

    def files_requests(self, namespace: str) -> bool:
        """A requester outside its own namespace may only file and follow its request verbs."""
        return self.requester and namespace == OPEN_VERB_NAMESPACE != self.namespace

    def allows_namespace(self, namespace: str) -> bool:
        scopes = [s for s in self.scopes if s.startswith("namespace:")]
        return self.is_admin or (
            f"namespace:{namespace}" in scopes or "namespace:*" in scopes
            if scopes
            else namespace == "main"
        )

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    @property
    def can_store(self) -> bool:
        return bool({"store", "nouns:read", "cas:write"}.intersection(self.scopes)) or self.is_admin

    def bound_surface(self) -> str | None:
        """Surface authorization comes only from an explicit authenticated scope."""
        if self.anonymous or self.requester:
            return None
        for s in self.scopes:
            m = _SURFACE_SCOPE_RE.fullmatch(s)
            if m:
                return m.group(1)
        return None

    def allows_queue(self, queue: str) -> bool:
        return any(fnmatchcase(queue, pattern) for pattern in self.queues)

    def uses_open_verbs(self, namespace: str) -> bool:
        """An anonymous session outside its own namespace may only touch open verbs there."""
        return self.anonymous and namespace == OPEN_VERB_NAMESPACE != self.namespace

    def may_use_queue(self, queue: str, namespace: str) -> bool:
        """Queue authority; for open verbs the task's own tags decide which verbs are visible."""
        if self.is_admin or self.allows_queue(queue):
            return True
        return self.uses_open_verbs(namespace) and queue not in CLOSED_TO_ANONYMOUS

    def kind_filter(self, requested: Sequence[str] | None) -> list[str] | None:
        """Kinds a claim may take: the request's, narrowed to the key's (None means any)."""
        if "*" in self.kinds:
            return list(requested) if requested else None
        return [k for k in (requested or self.kinds) if k in self.kinds]

    def effect_filter(self, requested: Sequence[str] | None) -> list[str]:
        allowed = [e for e in self.effect_classes if e != "public"]
        return [e for e in (requested or allowed) if e in allowed]
