"""What every door shares: the service settings, the claim rate limiter, reading the bearer key, and
the Gate that decides who may use which queue, effect class, surface store, and task. The HTTP service,
the MCP door and the agent pages all import these from here, never from each other."""

from __future__ import annotations

import os
import threading
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request

from tessera.errors import Forbidden, ScopeDenied, Unauthorized
from tessera.grants import PROTECTED_SURFACES, KeyGrant
from tessera.models import surface_slug
from tessera.store import Store


@dataclass(frozen=True)
class ServiceSettings:
    db_path: str = ".tessera/tessera.db"
    edge_secret: str | None = None
    claims_per_minute: int = 60
    reaper_seconds: float = 30.0
    public_url: str = "http://localhost:8080"
    allowed_hosts: tuple[str, ...] = ()
    repo_url: str = "https://github.com/ta-amps/tessera"

    @classmethod
    def from_env(cls) -> ServiceSettings:
        env = os.environ
        return cls(
            db_path=env.get("TESSERA_DB", ".tessera/tessera.db"),
            edge_secret=env.get("TESSERA_EDGE_SECRET") or None,
            claims_per_minute=int(env.get("TESSERA_CLAIMS_PER_MINUTE", "60")),
            reaper_seconds=float(env.get("TESSERA_REAPER_SECONDS", "30")),
            public_url=env.get("TESSERA_PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            allowed_hosts=tuple(
                h.strip() for h in env.get("TESSERA_ALLOWED_HOSTS", "").split(",") if h.strip()
            ),
        )


class ClaimLimiter:
    """At most `per_minute` claims per key in any rolling minute (admin keys are exempt)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, key_id: str, now: float) -> float | None:
        with self._lock:
            hits = self._hits[key_id]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return max(1.0, 60 - (now - hits[0]))
            hits.append(now)
            return None


def bearer_secret(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


class Gate:
    """Authorization rules shared by HTTP routes, web forms and the MCP door."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def grant(self, secret: str | None) -> KeyGrant:
        if not secret:
            raise Unauthorized("a bearer key is required")
        grant = self.store.find_key(secret)
        if grant is None:
            raise Unauthorized("the key is unknown, expired or revoked")
        return grant

    @staticmethod
    def admin(grant: KeyGrant) -> None:
        if not grant.is_admin:
            raise Forbidden("this needs a key with the admin scope")

    @staticmethod
    def queue(grant: KeyGrant, queue: str) -> None:
        if not (grant.is_admin or grant.allows_queue(queue)):
            raise Forbidden(f"this key may not use queue '{queue}'")

    @staticmethod
    def effect(grant: KeyGrant, effect_class: str) -> None:
        """Require the task's effect class; `write-repo` and `public` require `admin` (T22 / L25)."""
        if effect_class in ("write-repo", "public"):
            if not grant.is_admin:
                raise ScopeDenied(
                    f"effect class '{effect_class}' requires a key with the admin scope"
                )
            return
        if not grant.is_admin and effect_class not in grant.effect_classes:
            raise ScopeDenied(f"this key may not act on tasks with effect class '{effect_class}'")

    @staticmethod
    def effective_surface(grant: KeyGrant, surface_header: str | None) -> str | None:
        """Resolve the authenticated caller surface without allowing bound keys to spoof others."""
        hdr = surface_slug(surface_header) if (surface_header and surface_header.strip()) else None
        if grant.is_admin:
            return f"surface:{hdr}" if hdr else None
        bound = grant.bound_surface()
        if bound is not None:
            return f"surface:{bound}"
        return f"surface:{hdr}" if hdr else None

    @staticmethod
    def surface_write(grant: KeyGrant, store_name: str, surface_header: str | None = None) -> None:
        """Enforce surface ownership on protected surface stores (`<surface>.*`) and `eq03` on catalog/index."""
        if grant.is_admin:
            return
        for surf in PROTECTED_SURFACES:
            if store_name.startswith(f"{surf}."):
                bound = grant.bound_surface()
                hdr = (
                    surface_slug(surface_header)
                    if (surface_header and surface_header.strip())
                    else None
                )
                if bound is not None and hdr is not None and bound != hdr:
                    raise ScopeDenied(
                        f"key bound to surface '{bound}' cannot spoof X-Surface-Id '{hdr}'"
                    )
                caller_surf = bound or hdr
                if caller_surf != surf:
                    raise ScopeDenied(
                        f"non-admin writers to '{store_name}' must match surface '{surf}'"
                    )
                if store_name in ("eranos.catalog", "eranos.index") and "eq03" not in grant.queues:
                    raise ScopeDenied(
                        f"writing '{store_name}' requires an explicit 'eq03' queue grant"
                    )
                return

    def holder(self, grant: KeyGrant, task_id: str) -> None:
        """Only the key that claimed a task may heartbeat, split, submit or register jobs for it."""
        info = self.store.lease_info(task_id)
        if info is None:
            return  # no live lease: the store answers stale_lease, or replays an idempotent submit
        if info.key_id != grant.key_id and not (info.key_id is None and grant.is_admin):
            raise Forbidden("only the key that claimed this task may change it")

    @staticmethod
    def store_scope(grant: KeyGrant) -> None:
        if not grant.can_store:
            raise Forbidden("this needs a key with the store scope")
