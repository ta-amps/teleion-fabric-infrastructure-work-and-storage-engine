"""Tests for Surface Handshake v1.0 (`POST /v1/handshake` + MCP `handshake`) and `SurfaceBridge`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tessera import Forbidden, HandshakeRequest, InvalidRequest, QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.bridge import SurfaceBridge
from tessera.client import Client
from tessera.service import create_app


def test_store_handshake_clamps_limits_and_narrows_queues(tmp_path: Path) -> None:
    store = Store(tmp_path / "hs.db")
    try:
        store.define_queue(QueueConfig(name="solve", heartbeat_seconds=45))
        store.define_queue(QueueConfig(name="verify", heartbeat_seconds=30))
        store.define_queue(QueueConfig(name="eq02", heartbeat_seconds=60))
        store.put_record("canonical", "k1", {"title": "Shared item"}, expected_version=None)

        resp_high = store.handshake(
            HandshakeRequest(
                protocol_version="1.0",
                surface_id="kothar",
                requested_max_chars=999_999,
                requested_queues=["solve", "verify", "eq02"],
            ),
            grant_queues=["solve", "verify"],
            store_scope=True,
        )
        assert resp_high.surface_id == "kothar"
        assert resp_high.isolated_store_prefix == "kothar."
        assert resp_high.clamped_max_chars == 16_000
        assert resp_high.heartbeat_seconds == 30
        assert resp_high.allowed_queues == ["solve", "verify"]
        assert "canonical" in resp_high.shared_read_stores
        assert resp_high.transactional_db_exempt_from_middleware is True
        assert len(resp_high.handshake_id) == 64

        resp_low = store.handshake(
            HandshakeRequest(
                protocol_version="1.0",
                surface_id="kinema",
                requested_max_chars=10,
            ),
            grant_queues=["*"],
            store_scope=False,
        )
        assert resp_low.clamped_max_chars == 120
        assert resp_low.store_scope is False
        assert set(resp_low.allowed_queues) == {"solve", "verify", "eq02"}

        with pytest.raises(InvalidRequest, match="unsupported handshake protocol_version"):
            store.handshake(HandshakeRequest(protocol_version="2.0", surface_id="kothar"))
    finally:
        store.close()


def test_http_handshake_header_validation_and_surface_bridge_bidirectional_flow(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "svc.db")
    try:
        store.define_queue(QueueConfig(name="solve"))
        store.define_queue(QueueConfig(name="eq02"))
        kothar_secret, _ = store.issue_key(
            "kothar-surface", queues=["solve"], scopes=["store", "surface:kothar"]
        )
        eranos_secret, _ = store.issue_key(
            "eranos-surface", queues=["eq02"], scopes=["store", "surface:eranos"]
        )
        app = create_app(store, ServiceSettings())
        transport = httpx.WSGITransport if hasattr(httpx, "WSGITransport") else None
        _ = transport  # ASGI transport used via TestClient
        from fastapi.testclient import TestClient

        with TestClient(app) as tc:
            # GET on /v1/handshake must return 405 Method Not Allowed
            r_get = tc.get("/v1/handshake", headers={"Authorization": f"Bearer {kothar_secret}"})
            assert r_get.status_code == 405

            # Mismatched X-Surface-Id header vs body must return 400
            r_mismatch = tc.post(
                "/v1/handshake",
                headers={
                    "Authorization": f"Bearer {kothar_secret}",
                    "X-Surface-Id": "shamash",
                },
                json={"protocol_version": "1.0", "surface_id": "kothar"},
            )
            assert r_mismatch.status_code == 400

            # Header-only surface_id works and echoes x-surface-id & x-handshake-id headers
            r_hdr = tc.post(
                "/v1/handshake",
                headers={
                    "Authorization": f"Bearer {kothar_secret}",
                    "X-Surface-Id": "kothar",
                },
                json={"protocol_version": "1.0", "requested_max_chars": 50_000},
            )
            assert r_hdr.status_code == 200
            assert r_hdr.headers["x-surface-id"] == "kothar"
            assert len(r_hdr.headers["x-handshake-id"]) == 64
            assert r_hdr.json()["clamped_max_chars"] == 16_000
            assert r_hdr.json()["allowed_queues"] == ["solve"]

            r_client = tc.post(
                "/v1/handshake",
                headers={"Authorization": f"Bearer {eranos_secret}"},
                json={
                    "protocol_version": "1.0",
                    "surface_id": "eranos",
                    "requested_max_chars": 4000,
                    "requested_queues": ["eq02", "solve"],
                },
            )
            assert r_client.status_code == 200
            assert r_client.json()["allowed_queues"] == ["eq02"]
            assert Client("http://127.0.0.1:7700", surface_id="eranos").surface_id == "eranos"

        # SurfaceBridge bidirectional flow across two isolated surfaces (kothar <-> eranos)
        kothar_bridge = SurfaceBridge(
            "kothar",
            store,
            requested_max_chars=6000,
            requested_queues=["solve"],
        )
        eranos_bridge = SurfaceBridge(
            "eranos",
            store,
            requested_max_chars=3000,
            requested_queues=["eq02"],
        )
        hs_k = kothar_bridge.negotiate()
        hs_e = eranos_bridge.negotiate()
        assert hs_k.isolated_store_prefix == "kothar."
        assert hs_e.isolated_store_prefix == "eranos."

        # Cross-surface write isolation: kothar cannot write into eranos.catalog
        with pytest.raises(Forbidden, match="cannot write cross-surface store"):
            kothar_bridge.put_surface_noun("eranos.catalog", "KIT-01", {"bad": True})

        # Kothar writes into its isolated store and broadcasts onto noun.promotions
        rec, feed_ev = kothar_bridge.put_surface_noun(
            "compiled",
            "dag-01",
            {"recipe_id": "dag-01", "merkle_root": "abc123"},
            summary="Kothar promoted dag-01",
        )
        assert rec.store == "kothar.compiled"
        assert rec.version == 1
        assert feed_ev is not None
        assert feed_ev.kind == "kothar.promoted"

        # Eranos polls bidirectional updates and reads the shared/cross-surface noun cleanly
        updates, cursor = eranos_bridge.poll_updates()
        assert len(updates) == 1
        assert updates[0].payload["store"] == "kothar.compiled"
        assert cursor == feed_ev.seq
        shared = eranos_bridge.get_shared_noun("kothar.compiled", "dag-01")
        assert shared.value["merkle_root"] == "abc123"

        # Subsequent poll with advanced cursor returns 0 duplicate entries
        more, cursor2 = eranos_bridge.poll_updates()
        assert more == []
        assert cursor2 == cursor

        # Queue scoping enforced on SurfaceBridge.enqueue_verb
        with pytest.raises(Forbidden, match="outside negotiated queues"):
            kothar_bridge.enqueue_verb("eq02", {"goal": "unauthorized queue"})
        tid = kothar_bridge.enqueue_verb("solve", {"goal": "compile recipe"})
        lease = kothar_bridge.claim_verb("solve", "worker-1")
        assert lease is not None and lease.task_id == tid
        assert lease.brief["surface_id"] == "kothar"
        receipt = kothar_bridge.submit_verb(
            lease.task_id,
            lease.fencing_token,
            {"done": True},
            idempotency_key="idem-hs-1",
        )
        assert receipt.state_after == "done"

        # Middleware handshake + dry-run helper via mock transport
        seen_headers: dict[str, str] = {}

        def mock_herma(req: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(req.headers))
            if req.url.path == "/v1/handshake":
                return httpx.Response(
                    200,
                    json={
                        "protocol_version": "1.0",
                        "backend": "herma",
                        "surface_id": "kothar",
                        "clamped_max_chars": 4000,
                    },
                )
            if req.url.path == "/v1/dry-run":
                body: dict[str, Any] = json.loads(req.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={
                        "responded_directly": False,
                        "augmented_payload": body["payload"],
                        "shimmed_context_refs": ["kothar.nodes/ingest.csv_rows"],
                    },
                )
            return httpx.Response(404)

        mw_bridge = SurfaceBridge(
            "kothar",
            store,
            middleware_url="https://herma.example.test",
            middleware_token="hmk_test",
            middleware_transport=httpx.MockTransport(mock_herma),
        )
        mw_hs = mw_bridge.negotiate_middleware()
        assert mw_hs["backend"] == "herma"
        assert seen_headers.get("x-surface-id") == "kothar"
        shimmed = mw_bridge.shim_via_middleware(
            {"model": "muse-spark-open", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert shimmed["shimmed_context_refs"] == ["kothar.nodes/ingest.csv_rows"]
    finally:
        store.close()
