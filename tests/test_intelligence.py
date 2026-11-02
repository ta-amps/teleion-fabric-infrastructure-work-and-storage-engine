"""Tests for progressive L0 scent search, HTTP 206 byte ranges, delta feeds, dead-end tombstones,
and 0-call dual-resolution reflex recall across Store, HTTP Client, and Streamable HTTP MCP."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from tessera import BucketConfig, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.client import Client
from tessera.service import create_app


def test_l0_search_dead_ends_feeds_reflexes_and_http_206(tmp_path: Path) -> None:
    store = Store(tmp_path / "intel.db")
    try:
        # 1. Seed a large noun + bucket artifact and verify L0 progressive disclosure caps size
        large_notes = "EXAMPLE detailed evidence note. " * 200
        store.put_record(
            "shamash.cases",
            "C-IM-US-01",
            {
                "title": "Example Impersonation Wire Hold Case",
                "summary": "Safe-account phone impersonation demanding instant wire transfer; matched twin clears.",
                "domain": "shamash",
                "tags": ["impersonation", "wire-transfer"],
                "full_transcript": large_notes,
            },
            expected_version=None,
        )
        store.define_bucket(BucketConfig(name="kits", description="Verified NGO kits"))
        kit_bytes = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 10
        saved_obj = store.put_object(
            "kits",
            "helpline/triage.md",
            kit_bytes,
            content_type="text/markdown; charset=utf-8",
            metadata={
                "title": "NGO Helpline Triage SOP",
                "summary": "Multilingual intake checklist keeping safety planning human_only.",
                "surface": "eranos",
            },
            expected_version=None,
        )
        blob_sha = store.put_blob(b"HEADER:EXAMPLE_PAYLOAD:FOOTER")

        scents = store.search_nouns("impersonation wire")
        assert scents and scents[0].key == "C-IM-US-01"
        assert len(scents[0].l0_summary) <= 520
        assert scents[0].size_bytes > 5000

        bucket_scents = store.search_nouns("helpline triage", bucket="kits")
        assert len(bucket_scents) == 1
        assert bucket_scents[0].kind == "object" and bucket_scents[0].sha256 == saved_obj.sha256

        # 2. Verify automatic dead-end tombstone emission into `negative.knowledge` on dead-letter
        store.define_queue(
            QueueConfig(
                name="solve",
                max_attempts=2,
                backoff_base_seconds=0.0,
                backoff_cap_seconds=0.0,
            )
        )
        store.enqueue(
            TaskSpec(
                queue="solve",
                task_id="dead-wire-parse-01",
                kind="parse_bundle",
                brief={"title": "Unbounded regex parse on wire PDF", "goal": "Parse wire PDF"},
            )
        )
        for attempt in (1, 2):
            lease = store.claim("solve", "worker-1")
            assert lease is not None
            rec = store.submit(
                lease.task_id,
                lease.fencing_token,
                {},
                idempotency_key=f"dead:{attempt}",
                ok=False,
                error="catastrophic backtracking on malformed header",
            )
        assert rec.state_after == "dead"
        # Embedded tasks without an owner keep diagnostics operator-only.
        with store.scope_context(is_admin=True):
            tombstone = store.get_record("negative.knowledge", "dead-wire-parse-01")
        assert tombstone.version == 1
        assert "catastrophic backtracking" in tombstone.value["l0_summary"]

        # 3. Seed a verified 0-call reflex and verify dual-resolution match + dead-end warning
        store.put_record(
            "tessera.reflexes",
            "rx-wire-parse",
            {
                "reflex_id": "rx-wire-parse",
                "pattern": "parse wire bundle and verify multi currency caps",
                "tier": "deterministic_0_call",
                "verified_hits": 4,
                "l0_summary": "Use linear-time deterministic parser + statute cap table (0 model calls).",
                "steps": [
                    {"node": "extract.currency_amounts"},
                    {"node": "checks.advance_to_payout"},
                ],
                "tags": ["wire", "parse", "deterministic", "status:verified"],
            },
            expected_version=None,
        )
        with store.scope_context(is_admin=True):
            exact = store.match_reflex("verify multi-currency caps and parse wire bundle")
        assert exact.recommended_tier == "0_call_reflex"
        assert exact.hits[0].exact_hit is True
        assert any(d.key == "dead-wire-parse-01" for d in exact.dead_ends)

        fuzzy = store.match_reflex("wire bundle currensy cap parser")
        assert fuzzy.hits and fuzzy.hits[0].reflex_id == "rx-wire-parse"
        assert fuzzy.recommended_tier in {"0_call_reflex", "hybrid_1_call"}

        # 4. Verify HTTP 206 Partial Content byte ranges, Client methods, and MCP tools
        worker_key, _ = store.issue_key("intel-worker", queues=["solve"], scopes=["store"])
        app = create_app(store, ServiceSettings(public_url="http://testserver"))
        with TestClient(app) as tc:
            auth = {"authorization": f"Bearer {worker_key}"}

            # HTTP 206 on bucket object
            r206 = tc.get(
                "/v1/buckets/kits/objects/helpline/triage.md",
                headers={**auth, "range": "bytes=10-19"},
            )
            assert r206.status_code == 206
            assert r206.content == b"ABCDEFGHIJ"
            assert r206.headers["content-range"] == f"bytes 10-19/{len(kit_bytes)}"
            assert r206.headers["accept-ranges"] == "bytes"

            # HTTP 416 on out-of-bounds range
            r416 = tc.get(
                "/v1/buckets/kits/objects/helpline/triage.md",
                headers={**auth, "range": "bytes=99999-100000"},
            )
            assert r416.status_code == 416

            # HTTP 206 on raw content-addressed blob
            b206 = tc.get(
                f"/v1/blobs/{blob_sha}",
                headers={**auth, "range": "bytes=7-21"},
            )
            assert b206.status_code == 206
            assert b206.content == b"EXAMPLE_PAYLOAD"

            def _dispatch(req: httpx.Request) -> httpx.Response:
                r = tc.request(
                    req.method,
                    f"{req.url.path}?{req.url.query.decode()}" if req.url.query else req.url.path,
                    content=req.content,
                    headers=dict(req.headers),
                )
                return httpx.Response(r.status_code, headers=dict(r.headers), content=r.content)

            client = Client(
                "http://testserver", key=worker_key, transport=httpx.MockTransport(_dispatch)
            )
            try:
                assert (
                    client.get_object_slice("kits", "helpline/triage.md", start=0, end=9)
                    == b"0123456789"
                )
                assert client.get_blob_slice(blob_sha, start=0, end=5) == b"HEADER"
                e1 = client.publish_feed(
                    "shim.updates",
                    {"prov_id": "PROV-01", "version": 2},
                    kind="shim_promoted",
                    summary="Updated context shim rule v2",
                )
                e2 = client.publish_feed(
                    "shim.updates",
                    {"prov_id": "PROV-02", "version": 1},
                    kind="shim_promoted",
                    summary="Added direct-response rule v1",
                )
                assert e2.seq > e1.seq
                delta = client.poll_feed("shim.updates", after=e1.seq)
                assert [d.seq for d in delta] == [e2.seq]
                feeds = client.feeds()
                assert len(feeds) == 1 and feeds[0].entries == 2
                c_scents = client.search_nouns("impersonation", store="shamash.cases")
                assert len(c_scents) == 1 and c_scents[0].key == "C-IM-US-01"
                c_rx = client.match_reflex("parse wire bundle and verify multi currency caps")
                assert c_rx.recommended_tier == "0_call_reflex"
            finally:
                client.close()

            # Verify new MCP intelligence & L2 byte-slice tools
            mcp_headers = {
                **auth,
                "accept": "application/json, text/event-stream",
            }

            def rpc(method: str, params: dict[str, Any], ver: str | None = None) -> dict[str, Any]:
                hdrs = {**mcp_headers, **({"mcp-protocol-version": ver} if ver else {})}
                resp = tc.post(
                    "/mcp",
                    headers=hdrs,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                assert resp.status_code == 200, resp.text
                res: dict[str, Any] = resp.json()["result"]
                return res

            ver = rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "muse-chat-test", "version": "1"},
                },
            )["protocolVersion"]
            names = {t["name"] for t in rpc("tools/list", {}, ver)["tools"]}
            assert {
                "search_nouns",
                "match_reflex",
                "list_feeds",
                "poll_feed",
                "publish_feed",
            } <= names

            mcp_search = rpc(
                "tools/call",
                {"name": "search_nouns", "arguments": {"query": "impersonation"}},
                ver,
            )["structuredContent"]
            assert mcp_search["scents"][0]["key"] == "C-IM-US-01"

            mcp_slice = rpc(
                "tools/call",
                {
                    "name": "get_object",
                    "arguments": {
                        "bucket": "kits",
                        "key": "helpline/triage.md",
                        "offset": 10,
                        "max_bytes": 6,
                    },
                },
                ver,
            )["structuredContent"]
            assert mcp_slice["content"] == "ABCDEF"
            assert mcp_slice["slice"]["returned_bytes"] == 6

            mcp_rx = rpc(
                "tools/call",
                {
                    "name": "match_reflex",
                    "arguments": {"query": "parse wire bundle and verify multi currency caps"},
                },
                ver,
            )["structuredContent"]
            assert mcp_rx["recommended_tier"] == "0_call_reflex"
    finally:
        store.close()
