"""What a worker key may do: its queues, kinds, effect classes and scopes, and the rules that narrow a
request to them. Pure policy with no storage, so the store, the service, the MCP door and the pages all
read it from here. No key may hold the `public` effect class."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

from pydantic import Field

from tessera.models import SURFACE_ID_PATTERN, _Model

EFFECTS = ("read", "write-internal", "write-repo")
SCOPES = frozenset({"admin", "store"})
PROTECTED_SURFACES: tuple[str, ...] = ("shamash", "eranos", "herma", "kothar", "kinema")
_SURFACE_SCOPE_RE = re.compile(rf"^surface:({SURFACE_ID_PATTERN[1:-1]})$")


def is_valid_scope(scope: str) -> bool:
    return scope in SCOPES or bool(_SURFACE_SCOPE_RE.fullmatch(scope))


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

    def bound_surface(self) -> str | None:
        """Surface slug bound to this key via `surface:<slug>` scope or surface-prefixed label."""
        for s in self.scopes:
            m = _SURFACE_SCOPE_RE.fullmatch(s)
            if m:
                return m.group(1)
        lbl = self.label.strip().lower()
        if lbl.startswith("surface:"):
            rest = lbl[len("surface:") :].strip()
            if rest:
                return rest
        for surf in PROTECTED_SURFACES:
            if lbl == surf or lbl.startswith((f"{surf}-", f"{surf}_", f"{surf}.")):
                return surf
        return None

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
