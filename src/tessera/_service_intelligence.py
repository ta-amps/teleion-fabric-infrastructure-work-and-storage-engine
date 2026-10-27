"""HTTP routes for progressive L0 scent search, HTTP 206 byte-range slicing, live delta feeds,
and 0-call dual-resolution reflex recall.

Mounted by `tessera.service.create_app` behind the same `Gate` (`store` scope) and POST-only write rules.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tessera.access import Gate, bearer_secret
from tessera.errors import InvalidRequest
from tessera.grants import KeyGrant
from tessera.models import HandshakeRequest
from tessera.store import Store

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)


class PublishFeedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    kind: str = Field(default="delta", min_length=1, max_length=64)
    summary: str = Field(default="", max_length=520)


def slice_bytes_response(
    data: bytes,
    range_header: str | None,
    *,
    media_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> Response:
    """Serve full bytes (200) or an RFC 9110 single byte-range slice (206 Partial Content / 416)."""
    base_headers = {"accept-ranges": "bytes", **(headers or {})}
    if not range_header:
        return Response(data, status_code=200, media_type=media_type, headers=base_headers)

    total = len(data)
    match = _RANGE_RE.match(range_header.strip())
    if not match or total == 0:
        return Response(
            b"",
            status_code=416,
            headers={**base_headers, "content-range": f"bytes */{total}"},
        )
    s_raw, e_raw = match.group(1), match.group(2)
    if not s_raw and not e_raw:
        return Response(
            b"",
            status_code=416,
            headers={**base_headers, "content-range": f"bytes */{total}"},
        )
    if not s_raw:
        suffix = int(e_raw)
        if suffix <= 0:
            return Response(
                b"",
                status_code=416,
                headers={**base_headers, "content-range": f"bytes */{total}"},
            )
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(s_raw)
        end = total - 1 if not e_raw else min(int(e_raw), total - 1)
        if start >= total or start > end:
            return Response(
                b"",
                status_code=416,
                headers={**base_headers, "content-range": f"bytes */{total}"},
            )

    part = data[start : end + 1]
    return Response(
        part,
        status_code=206,
        media_type=media_type,
        headers={**base_headers, "content-range": f"bytes {start}-{end}/{total}"},
    )


def mount_intelligence_routes(app: FastAPI, store: Store, gate: Gate) -> None:
    def require_key(request: Request) -> KeyGrant:
        return gate.grant(bearer_secret(request))

    key: Any = Depends(require_key)

    @app.get("/v1/search")
    def search_nouns(
        q: str = "",
        store_name: str | None = None,
        store_filter: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
        request: Request = None,  # type: ignore[assignment]
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        chosen_store = (
            (request.query_params.get("store") if request else None) or store_name or store_filter
        )
        scents = store.search_nouns(q, store=chosen_store, bucket=bucket, limit=limit)
        return {"scents": [s.model_dump(mode="json") for s in scents]}

    @app.get("/v1/feeds")
    def list_feeds(grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"feeds": [f.model_dump(mode="json") for f in store.feeds()]}

    @app.get("/v1/feeds/{feed_name}")
    def poll_feed(
        feed_name: str,
        after: int = 0,
        limit: int = 100,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        entries = store.poll_feed(feed_name, after=after, limit=limit)
        next_cursor = entries[-1].seq if entries else max(0, after)
        return {
            "feed": feed_name,
            "entries": [e.model_dump(mode="json") for e in entries],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/feeds/{feed_name}", status_code=201)
    def publish_feed(
        feed_name: str,
        body: PublishFeedBody,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        entry = store.publish_feed(feed_name, body.payload, kind=body.kind, summary=body.summary)
        return entry.model_dump(mode="json")

    @app.get("/v1/reflexes/match")
    def match_reflex(
        q: str = "",
        limit: int = 5,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        return store.match_reflex(q, limit=limit).model_dump(mode="json")

    @app.post("/v1/handshake")
    def negotiate_handshake(
        body: HandshakeRequest,
        request: Request,
        grant: KeyGrant = key,
    ) -> JSONResponse:
        hdr_surface = (request.headers.get("x-surface-id") or "").strip()
        if hdr_surface and body.surface_id and hdr_surface != body.surface_id:
            raise InvalidRequest(
                f"X-Surface-Id header '{hdr_surface}' does not match body surface_id '{body.surface_id}'"
            )
        effective_surface = body.surface_id or hdr_surface or None
        req_obj = (
            body
            if effective_surface == body.surface_id
            else body.model_copy(update={"surface_id": effective_surface})
        )
        negotiated = store.handshake(
            req_obj,
            grant_queues=grant.queues,
            store_scope=grant.can_store,
        )
        return JSONResponse(
            negotiated.model_dump(mode="json"),
            headers={
                "x-surface-id": negotiated.surface_id,
                "x-handshake-id": negotiated.handshake_id,
            },
        )
