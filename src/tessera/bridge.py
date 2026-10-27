"""Reusable Surface Bridge wrapper for stateless product frontends and services.

Every product surface negotiates Surface Handshake v1.0 (`POST /v1/handshake`), identifies itself
with `X-Surface-Id`, isolates its surface-owned CAS stores under `<surface_id>.*` while reading
shared canonical stores, publishes/polls bidirectional monotonic delta feeds, and optionally shims
outbound LLM prompts through request middleware (`Herma`) without ever placing middleware in front
of transactional databases.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import httpx

from tessera._version import __version__
from tessera.backend import Backend
from tessera.errors import Forbidden, InvalidRequest, TesseraError
from tessera.models import (
    SURFACE_ID_PATTERN,
    EffectClass,
    FeedEntry,
    HandshakeRequest,
    HandshakeResponse,
    Lease,
    NounScent,
    Receipt,
    RecordVersion,
    RecordWrite,
    TaskSpec,
    VisibilityScope,
)

_SURFACE_RE = re.compile(SURFACE_ID_PATTERN)
DEFAULT_PROMOTION_FEED = "noun.promotions"


class SurfaceBridge:
    """Domain-neutral bidirectional wrapper binding one product surface to a Tessera Backend."""

    def __init__(
        self,
        surface_id: str,
        backend: Backend,
        *,
        namespace: str = "main",
        surface_version: str = __version__,
        requested_max_chars: int = 4000,
        requested_queues: Sequence[str] | None = None,
        requested_stores: Sequence[str] | None = None,
        middleware_url: str | None = None,
        middleware_token: str | None = None,
        middleware_transport: httpx.BaseTransport | None = None,
    ) -> None:
        clean_id = surface_id.strip()
        if not clean_id or not _SURFACE_RE.fullmatch(clean_id):
            raise InvalidRequest(
                f"invalid surface_id '{surface_id}'; must match {SURFACE_ID_PATTERN}"
            )
        self.surface_id = clean_id
        self.namespace = namespace
        self.surface_version = surface_version
        self.backend = backend
        self.requested_max_chars = requested_max_chars
        self.requested_queues = list(requested_queues or [])
        self.requested_stores = list(requested_stores or [])
        self.middleware_url = middleware_url.rstrip("/") if middleware_url else None
        self._middleware_token = middleware_token
        self._middleware_transport = middleware_transport
        self._handshake: HandshakeResponse | None = None
        self._feed_cursors: dict[str, int] = {}

    def __repr__(self) -> str:
        return (
            f"SurfaceBridge(surface_id={self.surface_id!r}, "
            f"negotiated={self._handshake is not None})"
        )

    @property
    def isolated_store_prefix(self) -> str:
        if self._handshake is not None:
            return self._handshake.isolated_store_prefix
        return f"{self.surface_id}."

    @property
    def clamped_max_chars(self) -> int:
        if self._handshake is not None:
            return self._handshake.clamped_max_chars
        return max(120, min(16_000, self.requested_max_chars))

    @property
    def handshake_receipt(self) -> HandshakeResponse | None:
        return self._handshake

    def negotiate(self) -> HandshakeResponse:
        """Execute Surface Handshake v1.0 against the Tessera backend and cache the receipt."""
        req = HandshakeRequest(
            protocol_version="1.0",
            surface_id=self.surface_id,
            surface_version=self.surface_version,
            requested_max_chars=self.requested_max_chars,
            requested_queues=self.requested_queues,
            requested_stores=self.requested_stores,
        )
        self._handshake = self.backend.handshake(req)
        return self._handshake

    def ensure_negotiated(self) -> HandshakeResponse:
        if self._handshake is None:
            return self.negotiate()
        return self._handshake

    def surface_store(self, substore: str) -> str:
        """Resolve `<substore>` or `<surface_id>.<substore>` to the isolated surface store name."""
        prefix = self.isolated_store_prefix
        clean = substore.strip()
        if clean.startswith(prefix):
            return clean
        if "." in clean:
            raise Forbidden(
                f"surface '{self.surface_id}' cannot write cross-surface store '{clean}'; "
                f"expected prefix '{prefix}'"
            )
        return f"{prefix}{clean}"

    def assert_store_writable(self, store_name: str, *, allow_shared_store: bool = False) -> None:
        """Enforce surface schema isolation on CAS writes."""
        prefix = self.isolated_store_prefix
        if allow_shared_store:
            return
        if not store_name.startswith(prefix):
            raise Forbidden(
                f"surface '{self.surface_id}' may only write stores under '{prefix}', "
                f"got '{store_name}'"
            )

    def put_surface_noun(
        self,
        substore: str,
        key: str,
        value: Any,
        *,
        expected_version: int | None = None,
        publish_feed: str | None = DEFAULT_PROMOTION_FEED,
        summary: str = "",
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> tuple[RecordVersion, FeedEntry | None]:
        """Write an isolated surface noun with CAS and optionally broadcast a delta feed event."""
        hs = self.ensure_negotiated()
        store_name = self.surface_store(substore)
        self.assert_store_writable(store_name)
        write = RecordWrite(
            namespace=self.namespace,
            store=store_name,
            key=key,
            value=value,
            expected_version=expected_version,
            producer_surface=f"surface:{self.surface_id}",
            visibility_scope=visibility_scope,
            allowed_surfaces=list(allowed_surfaces or []),
            target_cohorts=list(target_cohorts or ["all"]),
            tags=list(tags or []),
        )
        rec = self.backend.put_records([write])[0]
        feed_entry: FeedEntry | None = None
        if publish_feed:
            event_summary = summary or (
                f"Surface '{self.surface_id}' promoted {store_name}/{key} v{rec.version}"
            )
            feed_entry = self.backend.publish_feed(
                publish_feed,
                {
                    "surface_id": self.surface_id,
                    "store": store_name,
                    "key": key,
                    "version": rec.version,
                    "handshake_id": hs.handshake_id,
                },
                kind=f"{self.surface_id}.promoted",
                summary=event_summary[:520],
            )
        return rec, feed_entry

    def get_surface_noun(
        self, substore: str, key: str, version: int | None = None
    ) -> RecordVersion:
        """Read a versioned noun from this surface's isolated namespace."""
        return self.backend.get_record(self.surface_store(substore), key, version=version)

    def get_shared_noun(
        self, store_name: str, key: str, version: int | None = None
    ) -> RecordVersion:
        """Read any shared or cross-surface noun without mutating foreign namespaces."""
        return self.backend.get_record(store_name, key, version=version)

    def search_shared_nouns(
        self,
        query: str = "",
        *,
        store: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
    ) -> list[NounScent]:
        """Search shared L0 scents with optional Layer 2 filters when supported by backend."""
        fn: Any = self.backend.search_nouns
        if any(x is not None for x in (tag, category, cohort, surface)):
            res: list[NounScent] = fn(
                query,
                store=store,
                bucket=bucket,
                limit=limit,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
            )
            return res
        return self.backend.search_nouns(query, store=store, bucket=bucket, limit=limit)

    def poll_updates(
        self,
        feed: str = DEFAULT_PROMOTION_FEED,
        *,
        after: int | None = None,
        limit: int = 50,
    ) -> tuple[list[FeedEntry], int]:
        """Poll bidirectional delta events from `feed` and advance this bridge's cursor."""
        start_cursor = self._feed_cursors.get(feed, 0) if after is None else max(0, after)
        entries = self.backend.poll_feed(feed, after=start_cursor, limit=limit)
        next_cursor = entries[-1].seq if entries else start_cursor
        self._feed_cursors[feed] = next_cursor
        return entries, next_cursor

    def enqueue_verb(
        self,
        queue: str,
        brief: dict[str, Any],
        *,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        kind: str = "task",
        effect_class: EffectClass = "write-internal",
        priority: int = 50,
        depends_on: Sequence[str] | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> str:
        """Enqueue an atomic verb tagged with `surface_id` after verifying negotiated queue access."""
        hs = self.ensure_negotiated()
        if hs.allowed_queues and queue not in hs.allowed_queues:
            raise Forbidden(
                f"queue '{queue}' is outside negotiated queues for surface '{self.surface_id}'"
            )
        enriched_brief = {"surface_id": self.surface_id, **brief}
        return self.backend.enqueue(
            TaskSpec(
                namespace=self.namespace,
                queue=queue,
                brief=enriched_brief,
                task_id=task_id,
                idempotency_key=idempotency_key,
                kind=kind,
                effect_class=effect_class,
                priority=priority,
                depends_on=list(depends_on or []),
                producer_surface=f"surface:{self.surface_id}",
                visibility_scope=visibility_scope,
                allowed_surfaces=list(allowed_surfaces or []),
                target_cohorts=list(target_cohorts or ["all"]),
                tags=list(tags or []),
            )
        )

    def claim_verb(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
    ) -> Lease | None:
        return self.backend.claim(queue, worker, kinds=kinds, effect_classes=effect_classes)

    def submit_verb(
        self,
        task_id: str,
        fencing_token: int,
        output: Any,
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
    ) -> Receipt:
        return self.backend.submit(
            task_id,
            fencing_token,
            output,
            idempotency_key=idempotency_key,
            ok=ok,
            error=error,
        )

    def negotiate_middleware(self) -> dict[str, Any]:
        """Negotiate Surface Handshake v1.0 with the configured request middleware (Herma)."""
        if not self.middleware_url:
            raise InvalidRequest("middleware_url is not configured on this SurfaceBridge")
        headers = {
            "X-Surface-Id": self.surface_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._middleware_token:
            headers["Authorization"] = f"Bearer {self._middleware_token}"
        with httpx.Client(timeout=15.0, transport=self._middleware_transport) as http:
            resp = http.post(
                f"{self.middleware_url}/v1/handshake",
                headers=headers,
                json={
                    "protocol_version": "1.0",
                    "surface_id": self.surface_id,
                    "surface_version": self.surface_version,
                    "requested_max_chars": self.clamped_max_chars,
                },
            )
        if resp.status_code >= 400:
            raise TesseraError(
                f"middleware handshake failed with HTTP {resp.status_code}: {resp.text[:240]}"
            )
        data: dict[str, Any] = resp.json()
        return data

    def shim_via_middleware(
        self,
        payload: dict[str, Any],
        *,
        wire_format: str = "chat_completions",
        workspace_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Dry-run or shim an outbound LLM request via Herma while keeping DB substrates exempt."""
        if not self.middleware_url:
            raise InvalidRequest("middleware_url is not configured on this SurfaceBridge")
        headers = {
            "X-Surface-Id": self.surface_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._middleware_token:
            headers["Authorization"] = f"Bearer {self._middleware_token}"
        with httpx.Client(timeout=20.0, transport=self._middleware_transport) as http:
            resp = http.post(
                f"{self.middleware_url}/v1/dry-run",
                headers=headers,
                json={
                    "wire_format": wire_format,
                    "payload": payload,
                    "workspace_paths": list(workspace_paths or []),
                },
            )
        if resp.status_code >= 400:
            raise TesseraError(
                f"middleware dry-run failed with HTTP {resp.status_code}: {resp.text[:240]}"
            )
        data: dict[str, Any] = resp.json()
        return data

    def close(self) -> None:
        self.backend.close()
