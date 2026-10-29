"""Tests for queryable noun stores, persistent named file buckets, external blob_dir, and MCP tools."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tessera import BucketConfig, InvalidRequest, NotFound, Store, VersionConflict
from tessera.access import ServiceSettings
from tessera.client import Client
from tessera.service import create_app


def test_stores_and_buckets_roundtrip_and_cas(tmp_path: Path) -> None:
    blob_dir = tmp_path / "blobs"
    store = Store(tmp_path / "tessera.db", blob_dir=blob_dir)
    try:
        store.put_record("canonical", "noun.alpha", {"v": 1}, expected_version=None)
        store.put_record("canonical", "noun.beta", {"v": 2}, expected_version=None)
        store.put_record("seeds", "seed.one", {"v": 0}, expected_version=None)

        summaries = store.stores()
        assert [(s.store, s.records) for s in summaries] == [("canonical", 2), ("seeds", 1)]
        prefixed = store.list_records("canonical", prefix="noun.b")
        assert [r.key for r in prefixed] == ["noun.beta"]

        info = store.define_bucket(BucketConfig(name="kits", description="Verified NGO kits"))
        assert info.name == "kits" and info.objects == 0

        v1_bytes = b"# SYNTHETIC Kit v1\n"
        m1 = store.put_object(
            "kits",
            "ngo-intake/SKILL.md",
            v1_bytes,
            content_type="text/markdown",
            metadata={"author": "worker-a"},
            expected_version=None,
        )
        assert m1.version == 1
        assert m1.sha256 == hashlib.sha256(v1_bytes).hexdigest()
        # External blob_dir stored the bytes on disk by sha256 prefix
        assert (blob_dir / m1.sha256[:2] / m1.sha256).read_bytes() == v1_bytes

        with pytest.raises(VersionConflict) as exc:
            store.put_object("kits", "ngo-intake/SKILL.md", b"conflict", expected_version=None)
        assert exc.value.current_version == 1

        v2_bytes = b"# SYNTHETIC Kit v2 (reviewed)\n"
        m2 = store.put_object(
            "kits",
            "ngo-intake/SKILL.md",
            v2_bytes,
            content_type="text/markdown",
            metadata={"reviewer": "worker-b"},
            expected_version=1,
        )
        assert m2.version == 2

        head_meta, head_data = store.get_object("kits", "ngo-intake/SKILL.md")
        assert head_meta.version == 2 and head_data == v2_bytes

        old_meta, old_data = store.get_object("kits", "ngo-intake/SKILL.md", version=1)
        assert old_meta.version == 1 and old_data == v1_bytes

        listed = store.list_objects("kits", prefix="ngo-intake/")
        assert len(listed) == 1 and listed[0].version == 2

        hist = store.object_history("kits", "ngo-intake/SKILL.md")
        assert [h.version for h in hist] == [1, 2]

        buckets = store.buckets()
        assert len(buckets) == 1 and buckets[0].objects == 1 and buckets[0].bytes == len(v2_bytes)

        with pytest.raises(InvalidRequest):
            store.put_object("kits", "../escape", b"bad", expected_version=None)
        with pytest.raises(NotFound):
            store.put_object("missing-bucket", "a.txt", b"x", expected_version=None)
    finally:
        store.close()


def test_http_client_mcp_and_admin_keys_for_buckets(tmp_path: Path) -> None:
    store = Store(tmp_path / "service.db")
    try:
        admin_secret, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
        app = create_app(store, ServiceSettings(public_url="http://testserver"))
        with TestClient(app) as tc:
            # Admin can issue a store-scoped worker key remotely over HTTP
            issued = tc.post(
                "/v1/admin/keys",
                headers={"authorization": f"Bearer {admin_secret}"},
                json={"label": "fleet-worker-01", "queues": ["expand"], "scopes": ["store"]},
            )
            assert issued.status_code == 201, issued.text
            worker_secret = issued.json()["key"]
            assert (
                worker_secret
                not in tc.get(
                    "/v1/admin/keys", headers={"authorization": f"Bearer {admin_secret}"}
                ).text
            )

            # Writes are POST-only
            assert (
                tc.get(
                    "/v1/buckets/define", headers={"authorization": f"Bearer {worker_secret}"}
                ).status_code
                == 405
            )

            def _dispatch(req: httpx.Request) -> httpx.Response:
                r = tc.request(
                    req.method,
                    f"{req.url.path}?{req.url.query.decode()}" if req.url.query else req.url.path,
                    content=req.content,
                    headers=dict(req.headers),
                )
                return httpx.Response(r.status_code, headers=dict(r.headers), content=r.content)

            client = Client(
                "http://testserver", key=worker_secret, transport=httpx.MockTransport(_dispatch)
            )
            try:
                client.define_bucket(BucketConfig(name="nouns", description="Atomic noun files"))
                saved = client.put_object(
                    "nouns",
                    "packs/social-harm.json",
                    b'{"synthetic":true}',
                    content_type="application/json",
                    metadata={"stage": "canonical"},
                    expected_version=None,
                )
                assert saved.version == 1
                meta, raw = client.get_object("nouns", "packs/social-harm.json")
                assert meta.sha256 == saved.sha256 and raw == b'{"synthetic":true}'
                assert len(client.list_objects("nouns", prefix="packs/")) == 1
                assert [
                    v.version for v in client.object_history("nouns", "packs/social-harm.json")
                ] == [1]
            finally:
                client.close()

            # Verify MCP bucket & query tools over Streamable HTTP
            mcp_headers = {
                "authorization": f"Bearer {worker_secret}",
                "accept": "application/json, text/event-stream",
            }

            def rpc(method: str, params: dict[str, Any], ver: str | None = None) -> dict[str, Any]:
                hdrs = {**mcp_headers, **({"mcp-protocol-version": ver} if ver else {})}
                r = tc.post(
                    "/mcp",
                    headers=hdrs,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                assert r.status_code == 200, r.text
                res: dict[str, Any] = r.json()["result"]
                return res

            ver = rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "fleet-test", "version": "1"},
                },
            )["protocolVersion"]
            tool_names = {t["name"] for t in rpc("tools/list", {}, ver)["tools"]}
            assert {
                "list_stores",
                "list_records",
                "define_bucket",
                "buckets",
                "put_object",
                "get_object",
                "list_objects",
                "object_history",
            } <= tool_names

            put_res = rpc(
                "tools/call",
                {
                    "name": "put_object",
                    "arguments": {
                        "bucket": "nouns",
                        "key": "packs/mcp-note.md",
                        "content": "SYNTHETIC bilateral note",
                        "expected_version": None,
                    },
                },
                ver,
            )
            assert not put_res.get("isError")
            get_res = rpc(
                "tools/call",
                {
                    "name": "get_object",
                    "arguments": {"bucket": "nouns", "key": "packs/mcp-note.md"},
                },
                ver,
            )
            assert get_res["structuredContent"]["content"] == "SYNTHETIC bilateral note"
    finally:
        store.close()


def test_client_keeps_storage_outage_retry_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider storage outage is raised at once with the full Retry-After and reset time;
    short hints are still slept and retried inside the call."""
    import tessera.client as client_module
    from tessera.errors import StorageQuotaExhausted, Unavailable

    slept: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", slept.append)
    replies: list[httpx.Response] = []
    seen: list[str] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return replies.pop(0)

    problem = {"content-type": "application/problem+json"}
    quota = {"code": "storage_quota_exhausted", "detail": "allowance spent"}
    quota |= {"status": 503, "reset_at": "2026-10-30T00:00:00.000Z"}
    client = Client("http://testserver", key="synthetic", transport=httpx.MockTransport(dispatch))
    replies.append(httpx.Response(503, json=quota, headers={**problem, "retry-after": "20793"}))
    with pytest.raises(StorageQuotaExhausted) as raised:
        client.counts()
    assert raised.value.retry_after == 20793
    assert raised.value.reset_at == "2026-10-30T00:00:00.000Z"
    assert (len(seen), slept) == (1, [])

    replies.append(
        httpx.Response(503, json={"code": "unavailable"}, headers={"retry-after": "120"})
    )
    with pytest.raises(Unavailable) as long_wait:
        client.counts()
    assert long_wait.value.retry_after == 120 and slept == []

    replies.extend(
        [
            httpx.Response(429, json={"code": "rate_limited"}, headers={"retry-after": "2"}),
            httpx.Response(200, json={"as_of": 0, "queues": {}}),
        ]
    )
    assert client.counts() == {}
    assert slept == [2.0]
    client.close()
