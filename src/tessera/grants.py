"""What a worker key may do: its queues, kinds, effect classes and scopes, and the rules that narrow a
request to them. Pure policy with no storage, so the store, the service, the MCP door and the pages all
read it from here. No key may hold the `public` effect class."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

from pydantic import Field

from tessera.models import _Model

EFFECTS = ("read", "write-internal", "write-repo")
SCOPES = frozenset({"admin", "store"})


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

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    @property
    def can_store(self) -> bool:
        return "store" in self.scopes or self.is_admin

    def allows_queue(self, queue: str) -> bool:
        return any(fnmatchcase(queue, pattern) for pattern in self.queues)

    def kind_filter(self, requested: Sequence[str] | None) -> list[str] | None:
        """Kinds a claim may take: the request's, narrowed to the key's (None means any)."""
        if "*" in self.kinds:
            return list(requested) if requested else None
        return [k for k in (requested or self.kinds) if k in self.kinds]

    def effect_filter(self, requested: Sequence[str] | None) -> list[str]:
        allowed = [e for e in self.effect_classes if e != "public"]
        return [e for e in (requested or allowed) if e in allowed]
