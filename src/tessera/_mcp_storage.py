"""Storage, bucket, 0-call L0 search, live feed, and reflex-recall MCP tools for `tessera.mcp_door`."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tessera.grants import KeyGrant
from tessera.models import (
    BucketConfig,
    HandshakeRequest,
    HandshakeResponse,
    RecordWrite,
    ReflexMatch,
)

if TYPE_CHECKING:
    from tessera.access import Gate
    from tessera.store import Store


def register_storage_and_intelligence_tools(
    server: MCPServer[Any],
    store: Store,
    gate: Gate,
    run: Callable[[Callable[[], Any]], Awaitable[Any]],
    current_grant: Callable[[], Awaitable[KeyGrant]],
) -> None:
    @server.tool(
        description="Write one versioned noun (record) using compare-and-set. Pass "
        "expected_version=null when creating a new key, or the version you read when updating."
    )
    async def put_record(
        store_name: str,
        key: str,
        value: Any,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        write = RecordWrite(
            store=store_name, key=key, value=value, expected_version=expected_version
        )
        saved = await run(lambda: store.put_records([write]))
        return {"record": saved[0].model_dump(mode="json")}

    @server.tool(
        description="Atomically write a batch of versioned nouns (records) across stores using "
        "compare-and-set; every write lands together or none does."
    )
    async def put_records(writes: list[dict[str, Any]]) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        parsed = [RecordWrite.model_validate(w) for w in writes]
        saved = await run(lambda: store.put_records(parsed))
        return {"records": [r.model_dump(mode="json") for r in saved]}

    @server.tool(description="Read the latest (or a specific) version of an atomic noun (record).")
    async def get_record(store_name: str, key: str, version: int | None = None) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        rec = await run(lambda: store.get_record(store_name, key, version))
        return {"record": rec.model_dump(mode="json")}

    @server.tool(description="List every saved version of an atomic noun (record) in order.")
    async def record_history(store_name: str, key: str) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        versions = await run(lambda: store.record_history(store_name, key))
        return {"versions": [v.model_dump(mode="json") for v in versions]}

    @server.tool(
        description="List every atomic noun store with its key count and last update time."
    )
    async def list_stores() -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        items = await run(store.stores)
        return {"stores": [s.model_dump(mode="json") for s in items]}

    @server.tool(
        description="Query current head records in a noun store, optionally filtered by key prefix."
    )
    async def list_records(store_name: str, prefix: str = "", limit: int = 100) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        recs = await run(lambda: store.list_records(store_name, prefix=prefix, limit=limit))
        return {"records": [r.model_dump(mode="json") for r in recs]}

    @server.tool(
        description="Create or update a named persistent file bucket (e.g. nouns, verbs, kits, cases, provisions, traces)."
    )
    async def define_bucket(name: str, description: str = "") -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        cfg = BucketConfig(name=name, description=description)
        info = await run(lambda: store.define_bucket(cfg))
        return {"bucket": info.model_dump(mode="json")}

    @server.tool(description="List persistent file buckets with object counts and byte totals.")
    async def buckets() -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        items = await run(store.buckets)
        return {"buckets": [b.model_dump(mode="json") for b in items]}

    @server.tool(
        description="Store a file in a named bucket using compare-and-set (expected_version=null for new, "
        "or current version to update). Pass encoding='utf-8' for text/JSON/Markdown or 'base64' for binary."
    )
    async def put_object(
        bucket: str,
        key: str,
        content: str,
        encoding: str = "utf-8",
        content_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        if encoding == "base64":
            data = base64.b64decode(content.encode("ascii"))
        elif encoding == "utf-8":
            data = content.encode("utf-8")
        else:
            raise ToolError("invalid_request: encoding must be 'utf-8' or 'base64'")
        saved = await run(
            lambda: store.put_object(
                bucket,
                key,
                data,
                content_type=content_type,
                metadata=metadata,
                expected_version=expected_version,
            )
        )
        return {"object": saved.model_dump(mode="json")}

    @server.tool(
        description="Read a versioned file (or targeted L2 byte slice via offset/max_bytes) from a "
        "named bucket, verifying its SHA-256."
    )
    async def get_object(
        bucket: str,
        key: str,
        version: int | None = None,
        encoding: str = "auto",
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        meta, full_data = await run(lambda: store.get_object(bucket, key, version))
        start = max(0, offset)
        data = (
            full_data[start:] if max_bytes is None else full_data[start : start + max(0, max_bytes)]
        )
        if encoding == "base64":
            body, used_enc = base64.b64encode(data).decode("ascii"), "base64"
        else:
            try:
                body, used_enc = data.decode("utf-8"), "utf-8"
            except UnicodeDecodeError as exc:
                if encoding == "utf-8":
                    raise ToolError(
                        "invalid_request: object is binary; use encoding='base64'"
                    ) from exc
                body, used_enc = base64.b64encode(data).decode("ascii"), "base64"
        return {
            "object": meta.model_dump(mode="json"),
            "content": body,
            "encoding": used_enc,
            "slice": {"offset": start, "returned_bytes": len(data), "total_bytes": len(full_data)},
        }

    @server.tool(description="List head files in a bucket, optionally filtered by key prefix.")
    async def list_objects(bucket: str, prefix: str = "", limit: int = 100) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        objs = await run(lambda: store.list_objects(bucket, prefix=prefix, limit=limit))
        return {"objects": [o.model_dump(mode="json") for o in objs]}

    @server.tool(description="List every saved version of a file in a bucket in order.")
    async def object_history(bucket: str, key: str) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        versions = await run(lambda: store.object_history(bucket, key))
        return {"versions": [v.model_dump(mode="json") for v in versions]}

    @server.tool(
        description="0-call progressive L0 scent search across CAS nouns and bucket files. Call this "
        "first to inspect <=150-token summaries before loading full L1 records or L2 byte slices."
    )
    async def search_nouns(
        query: str = "",
        store_name: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        scents = await run(
            lambda: store.search_nouns(query, store=store_name, bucket=bucket, limit=limit)
        )
        return {"scents": [s.model_dump(mode="json") for s in scents]}

    @server.tool(description="List monotonic cursor-sequenced live delta feeds.")
    async def list_feeds() -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        items = await run(store.feeds)
        return {"feeds": [f.model_dump(mode="json") for f in items]}

    @server.tool(
        description="Poll monotonic delta entries in a live feed since cursor `after` (`seq > after`)."
    )
    async def poll_feed(feed: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        entries = await run(lambda: store.poll_feed(feed, after=after, limit=limit))
        next_cursor = entries[-1].seq if entries else max(0, after)
        return {
            "feed": feed,
            "entries": [e.model_dump(mode="json") for e in entries],
            "next_cursor": next_cursor,
        }

    @server.tool(description="Publish one monotonic delta entry to a live swarm feed.")
    async def publish_feed(
        feed: str,
        payload: dict[str, Any],
        kind: str = "delta",
        summary: str = "",
    ) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        entry = await run(lambda: store.publish_feed(feed, payload, kind=kind, summary=summary))
        return {"entry": entry.model_dump(mode="json")}

    @server.tool(
        description="0-call dual-resolution muscle-memory + fuzzy step recall + dead-end tombstone "
        "lookup. Call before solving to reuse verified 0-call recipes and avoid known dead ends."
    )
    async def match_reflex(query: str, limit: int = 5) -> dict[str, Any]:
        grant = await current_grant()
        await run(lambda: gate.store_scope(grant))
        matched: ReflexMatch = await run(lambda: store.match_reflex(query, limit=limit))
        return matched.model_dump(mode="json")

    @server.tool(
        description="Negotiate Surface Handshake v1.0: validate surface_id, clamp max_chars, "
        "and return isolated store namespace, allowed queues, capabilities, and handshake_id."
    )
    async def handshake(
        surface_id: str,
        protocol_version: str = "1.0",
        surface_version: str = "0.1.0",
        requested_max_chars: int = 4000,
        requested_queues: list[str] | None = None,
        requested_stores: list[str] | None = None,
    ) -> dict[str, Any]:
        grant = await current_grant()
        req = HandshakeRequest(
            protocol_version=protocol_version,
            surface_id=surface_id,
            surface_version=surface_version,
            requested_max_chars=requested_max_chars,
            requested_queues=requested_queues or [],
            requested_stores=requested_stores or [],
        )
        negotiated: HandshakeResponse = await run(
            lambda: store.handshake(
                req,
                grant_queues=grant.queues,
                store_scope=grant.can_store,
            )
        )
        return negotiated.model_dump(mode="json")
