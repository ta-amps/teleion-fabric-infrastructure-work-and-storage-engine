"""Full anonymous/keyed agent loops, durable limits and adversarial isolation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app


@pytest.fixture
def access(tmp_path: Path) -> Iterator[tuple[TestClient, Store, FakeClock, str]]:
    clock = FakeClock()
    store = Store(str(tmp_path / "access.db"), clock=clock)
    admin, _ = store.issue_key(
        "operator", queues=["*"], scopes=["admin", "store"], account_id="account:operator"
    )
    settings = ServiceSettings(
        anonymous_access_enabled=True,
        reaper_seconds=86400,
        public_url="http://testserver",
        claims_per_minute=1000,
    )
    with TestClient(create_app(store, settings)) as client:
        yield client, store, clock, admin
    store.close()


def auth(secret: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + secret}


def anonymous(client: TestClient, label: str = "test-agent") -> dict[str, Any]:
    response = client.post("/v1/session/anonymous", json={"agent_label": label})
    assert response.status_code == 201
    return response.json()


def rpc(client: TestClient, name: str, arguments: dict[str, Any], secret: str | None = None) -> Any:
    headers = {"Accept": "application/json, text/event-stream", **(auth(secret) if secret else {})}
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )


def test_anonymous_full_loop_and_durable_provenance(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, admin = access
    session = anonymous(client, "test<script>")
    secret = session["session_token"]
    assert secret.startswith("tan_") and session["labels"]["agent_label"] == "testscript"
    headers = auth(secret)
    found = client.get("/v1/search?q=practice", headers=headers)
    assert found.status_code == 200 and len(found.json()["scents"]) == 1
    response = client.post(
        "/v1/tasks/claim", json={"queue": "demo.ping", "worker": "agent"}, headers=headers
    )
    assert response.status_code == 200
    lease = response.json()
    assert lease["namespace"] == session["namespace"] and len(lease["claim_receipt"]) == 64
    assert lease["labels"]["origin"] == "anonymous"
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    assert client.post("/v1/tasks/heartbeat", json=ref, headers=headers).status_code == 200
    path = "/v1/stores/demo.artifacts/records/result"
    saved = client.post(
        path,
        json={
            "value": {"example_only": True},
            "expected_version": 0,
            "visibility_scope": "public_commons",
            "tags": ["demo:false", "untrusted:false"],
        },
        headers=headers,
    )
    assert saved.status_code == 200
    record = saved.json()
    assert record["namespace"] == session["namespace"] and record["visibility_scope"] == "private"
    assert {"origin:anonymous", "demo:true", "untrusted:true"} <= set(record["tags"])
    assert "demo:false" not in record["tags"]
    headers["Idempotency-Key"] = "done"
    first = client.post(
        "/v1/tasks/submit",
        json={**ref, "output": {"record": "result", "version": 1}},
        headers=headers,
    )
    assert first.status_code == 200 and first.json()["labels"]["demo"] is True
    # Replays are bound to the original actor and result on both HTTP backends.
    conflict = client.post("/v1/tasks/submit", json={**ref, "output": {}}, headers=headers)
    assert conflict.status_code == 409
    again = client.post(
        "/v1/tasks/submit",
        json={**ref, "output": {"record": "result", "version": 1}},
        headers=headers,
    )
    assert again.json()["replayed"] and again.json()["receipt_hash"] == first.json()["receipt_hash"]
    assert again.json()["labels"] == first.json()["labels"]
    results = client.get(f"/v1/tasks/{lease['task_id']}/results", headers=headers).json()["results"]
    assert results[0]["labels"] == first.json()["labels"]
    events = client.get("/v1/me/audit", headers=headers).json()["events"]
    assert {"mint_session", "searchNouns", "claim", "heartbeat", "putRecords", "submit"} <= {
        e["action"] for e in events
    }
    assert all(
        e["principal_kind"] == "anonymous" and e["principal_id"] == session["principal_id"]
        for e in events
    )
    assert any(e["receipt_id"] == first.json()["receipt_hash"] for e in events)
    claimed = next(e for e in events if e["action"] == "claim")
    assert (
        claimed["resource_id"] == lease["task_id"]
        and claimed["receipt_id"] == lease["claim_receipt"]
    )
    assert all(len(e["ip_hash"]) == 64 and len(e["ua_hash"]) == 64 for e in events)
    assert all(e["namespace"] == session["namespace"] for e in events)
    all_events = client.get("/v1/admin/audit", headers=auth(admin)).json()["events"]
    assert set(e["seq"] for e in events) <= set(e["seq"] for e in all_events)
    public = client.get("/v1/public/activity").json()["events"]
    assert all(
        set(e) == {"ts", "principal_kind", "action", "resource_type", "status", "labels"}
        for e in public
    )
    # Read the write-ahead log too: fresh rows live there until a checkpoint.
    store._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    files = (Path(store.path), Path(store.path + "-wal"))
    stored = b"".join(f.read_bytes() for f in files if f.exists()).decode("utf-8", "ignore")
    assert session["principal_id"] in stored, "the check must see the rows this session wrote"
    assert secret not in json.dumps(events) + stored


def test_sessions_cannot_cross_namespaces_or_admin(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, _, _, _ = access
    one, two = anonymous(client), anonymous(client)
    h = auth(one["session_token"])
    assert (
        client.get(
            f"/ns/{two['namespace']}/v1/stores/demo.instructions/records/welcome", headers=h
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/stores/demo.instructions/records/welcome", headers={**h, "X-Namespace": "main"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/tasks/claim", json={"queue": "production", "worker": "agent"}, headers=h
        ).status_code
        == 403
    )
    assert client.get("/v1/admin/audit", headers=h).status_code == 403
    assert (
        client.post(
            "/v1/admin/keys", json={"label": "x", "queues": ["*"], "scopes": ["admin"]}, headers=h
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/keys", json={"label": "x", "queues": ["*"], "scopes": ["admin"]}, headers=h
        ).status_code
        == 403
    )
    own = client.get("/v1/me/audit", headers=h).json()["events"]
    assert not any(e["principal_id"] == two["principal_id"] for e in own)
    spoof = client.post(
        "/v1/stores/demo.artifacts/records/x",
        json={"namespace": two["namespace"], "value": {}},
        headers=h,
    )
    assert spoof.status_code in {400, 403}
    guessed = client.get(f"/v1/tasks/{two['principal_id']}_demo_ping", headers=h)
    assert guessed.status_code == 404


def test_persistent_quota_and_example_policy(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, _ = access
    session = anonymous(client)
    h = auth(session["session_token"])
    path = "/v1/stores/demo.artifacts/records/example"
    for value in ("person@real.invalid", "123-45-6789", "4111 1111 1111 1111"):
        assert client.post(path, json={"value": value}, headers=h).status_code == 400
    assert client.post(path, json={"value": "adult@example.com"}, headers=h).status_code == 200
    grant = store.find_key(session["session_token"])
    assert grant is not None
    with store._tx() as c:
        c.execute("UPDATE access_limits SET requests=299 WHERE subject=?", (grant.key_id,))

    def read(_: int) -> int:
        return client.get("/v1/me", headers=h).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(read, range(4)))
    assert codes.count(200) == 1 and codes.count(429) == 3
    assert "retry-after" in client.get("/v1/me", headers=h).headers
    with store._tx() as c:
        c.execute(
            "UPDATE access_limits SET requests=0,bytes=1048576 WHERE subject=?", (grant.key_id,)
        )
    assert (
        client.post(path, json={"value": {}, "expected_version": 1}, headers=h).status_code == 429
    )


def test_parallel_claim_and_cas_fencing(access: tuple[TestClient, Store, FakeClock, str]) -> None:
    client, _, clock, _ = access
    session = anonymous(client)
    h = auth(session["session_token"])

    def claim(_: int) -> Any:
        return client.post(
            "/v1/tasks/claim", json={"queue": "demo.ping", "worker": "agent"}, headers=h
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(claim, range(20)))
    winners = [r for r in responses if r.status_code == 200]
    assert len(winners) == 1
    assert all(r.status_code in {200, 429} for r in responses)
    path = "/v1/stores/demo.artifacts/records/cas"
    assert (
        client.post(path, json={"value": {}, "expected_version": 0}, headers=h).status_code == 200
    )

    def put(i: int) -> int:
        return client.post(path, json={"value": i, "expected_version": 1}, headers=h).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(put, range(20)))
    assert codes.count(200) == 1 and codes.count(409) == 19
    lease = winners[0].json()
    clock.advance(301)
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    assert client.post("/v1/tasks/heartbeat", json=ref, headers=h).status_code == 409
    assert (
        client.post(
            "/v1/tasks/submit",
            json={**ref, "output": {}},
            headers={**h, "Idempotency-Key": "stale"},
        ).status_code
        == 409
    )


def test_keyed_lifecycle_and_non_demo_loop(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, admin = access
    store.define_queue(QueueConfig(name="production"))
    store.enqueue(TaskSpec(queue="production", brief={}))
    issued = client.post(
        "/v1/keys",
        json={
            "label": "agent",
            "account_id": "account:one",
            "queues": ["production"],
            "scopes": ["nouns:read", "verbs:claim", "verbs:submit", "cas:write", "namespace:main"],
        },
        headers=auth(admin),
    )
    assert issued.status_code == 201
    secret, key_id = issued.json()["key"], issued.json()["key_id"]
    h = auth(secret)
    lease = client.post(
        "/v1/tasks/claim", json={"queue": "production", "worker": "keyed"}, headers=h
    ).json()
    assert "task_id" in lease
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    assert client.post("/v1/tasks/heartbeat", json=ref, headers=h).status_code == 200
    assert (
        client.post(
            "/v1/stores/artifacts/records/x",
            json={"value": {}, "visibility_scope": "private"},
            headers={**h, "If-Match": "0"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/tasks/submit",
            json={**ref, "output": {}},
            headers={**h, "Idempotency-Key": "keyed"},
        ).status_code
        == 200
    )
    events = client.get("/v1/me/audit", headers=h).json()["events"]
    assert all(
        e["key_id"] == key_id
        and e["account_id"] == "account:one"
        and e["principal_kind"] == "keyed"
        for e in events
    )
    listed = client.get("/v1/keys", headers=h).json()["keys"]
    assert len(listed) == 1 and listed[0]["key_prefix"] == secret[:8] and "key" not in listed[0]
    assert client.post(f"/v1/keys/{key_id}/revoke", headers=h).status_code == 200
    assert client.get("/v1/me", headers=h).status_code == 401


def test_mcp_open_discovery_and_full_demo_loop(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, _, _, _ = access
    minted = rpc(client, "open_anonymous_session", {"agent_label": "mcp-agent"})
    assert minted.status_code == 200
    session = minted.json()["result"]["structuredContent"]
    secret = session["session_token"]
    search = rpc(client, "search_nouns", {"query": "practice"}, secret)
    assert not search.json()["result"].get("isError")
    lease = rpc(client, "claim", {"queue": "demo.ping", "worker": "mcp"}, secret).json()["result"][
        "structuredContent"
    ]["lease"]
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    assert not rpc(client, "heartbeat", ref, secret).json()["result"].get("isError")
    record = rpc(
        client,
        "put_record",
        {"store_name": "demo.artifacts", "key": "x", "value": {}, "expected_version": 0},
        secret,
    )
    assert record.json()["result"]["structuredContent"]["record"]["visibility_scope"] == "private"
    submitted = rpc(client, "submit", {**ref, "output": {}, "idempotency_key": "mcp-done"}, secret)
    assert (
        submitted.json()["result"]["structuredContent"]["receipt"]["labels"]["origin"]
        == "anonymous"
    )
    audit = rpc(client, "my_audit", {}, secret).json()["result"]["structuredContent"]["events"]
    assert any(e["action"] == "submit" and e["receipt_id"] for e in audit)
    listing = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    ).json()["result"]["tools"]
    card = client.get("/.well-known/agent-card.json").json()
    assert (
        {t["name"] for t in listing}
        == {t["name"] for t in card["tools"]}
        == {s["id"] for s in card["skills"]}
    )


def test_expiry_retention_and_cors(access: tuple[TestClient, Store, FakeClock, str]) -> None:
    client, store, clock, _ = access
    session = anonymous(client)
    for path in ("/mcp", "/v1/session/anonymous"):
        preflight = client.options(
            path,
            headers={
                "Origin": "https://agent.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "*"
        assert "Authorization" in preflight.headers["access-control-allow-headers"]
    missing = client.get("/v1/me")
    unknown = client.get("/v1/me", headers=auth("tsk_invalid"))
    assert missing.status_code == unknown.status_code == 401
    assert missing.json()["detail"] != unknown.json()["detail"]
    clock.advance(3601)
    assert client.get("/v1/me", headers=auth(session["session_token"])).status_code == 401
    clock.advance(86400)
    assert store.purge_expired_principals() == 1
    with store._read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM tasks WHERE namespace=?", (session["namespace"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM records WHERE namespace=?", (session["namespace"],)
            ).fetchone()[0]
            == 0
        )
    assert client.get("/v1/public/activity").json()["events"] == []


def test_fine_scopes_and_forms_cannot_bypass_policy(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, _ = access
    store.define_queue(QueueConfig(name="production"))
    task = store.enqueue(TaskSpec(queue="production", brief={}))
    reader, _ = store.issue_key("reader", queues=["production"], scopes=["nouns:read"])
    assert client.get("/v1/stores", headers=auth(reader)).status_code == 200
    assert (
        client.post(
            "/v1/stores/notes/records/x", json={"value": {}}, headers=auth(reader)
        ).status_code
        == 403
    )
    assert (
        client.post("/v1/buckets/define", json={"name": "bucket"}, headers=auth(reader)).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/tasks/claim",
            json={"queue": "production", "worker": "read-only"},
            headers=auth(reader),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/work/claim", data={"key": reader, "queue": "production", "worker": "read-only"}
        ).status_code
        == 403
    )
    assert client.get("/ns/foreign/v1/queues", headers=auth(reader)).status_code == 403
    general, _ = store.issue_key("general", queues=["production"], scopes=["store"])
    assert (
        client.post("/v1/tasks/cancel", json={"task_id": task}, headers=auth(general)).status_code
        == 403
    )
    claim = client.post(
        "/v1/tasks/claim", json={"queue": "production", "worker": "owner"}, headers=auth(general)
    )
    assert claim.status_code == 200
    assert (
        client.post("/v1/tasks/cancel", json={"task_id": task}, headers=auth(general)).status_code
        == 200
    )
    anon = anonymous(client, "surface:herma")
    anon_grant = store.find_key(anon["session_token"])
    assert anon_grant is not None and anon_grant.bound_surface() is None
    form_claim = client.post(
        "/work/claim",
        data={"key": anon["session_token"], "queue": "demo.ping", "worker": "browser"},
    )
    assert form_claim.status_code == 200
    audit = client.get("/v1/me/audit", headers=auth(anon["session_token"])).json()["events"]
    assert any(e["route_or_tool"] == "/work/claim" and e["receipt_id"] for e in audit)


def test_mint_limits_payload_bound_and_malformed_mcp(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, _, _, _ = access
    session = anonymous(client)
    for _ in range(9):
        anonymous(client)
    denied = client.post("/v1/session/anonymous", json={})
    assert denied.status_code == 429 and "retry-after" in denied.headers
    too_big = client.post(
        "/v1/stores/demo.artifacts/records/large",
        json={"value": "x" * 65536},
        headers=auth(session["session_token"]),
    )
    assert too_big.status_code == 413
    invalid = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": {}, "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert invalid.status_code == 400
    invalid = rpc(client, "put_records", {"writes": None}, session["session_token"])
    assert invalid.status_code == 400


def test_keyed_and_mixed_claim_races(access: tuple[TestClient, Store, FakeClock, str]) -> None:
    client, store, _, admin = access
    store.define_queue(QueueConfig(name="production"))
    store.enqueue(TaskSpec(queue="production", brief={}))
    secrets = [
        store.issue_key("worker", queues=["production"], scopes=["verbs:claim"])[0]
        for _ in range(2)
    ]

    def keyed_claim(i: int) -> int:
        return client.post(
            "/v1/tasks/claim",
            json={"queue": "production", "worker": str(i)},
            headers=auth(secrets[i % 2]),
        ).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(keyed_claim, range(20)))
    assert codes.count(200) == 1 and codes.count(204) == 19
    session = anonymous(client)
    foreign = anonymous(client)

    def mixed_claim(i: int) -> int:
        token = (
            session["session_token"]
            if i % 3 == 0
            else admin
            if i % 3 == 1
            else foreign["session_token"]
        )
        return client.post(
            f"/ns/{session['namespace']}/v1/tasks/claim",
            json={"queue": "demo.ping", "worker": str(i)},
            headers=auth(token),
        ).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(mixed_claim, range(20)))
    assert codes.count(200) == 1
    assert set(codes) <= {200, 204, 403, 429}


def test_admin_key_delegation_never_expands_authority(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, _ = access
    issuer, _ = store.issue_key(
        "issuer",
        queues=["production"],
        kinds=["small"],
        effect_classes=["read"],
        scopes=["admin:keys", "nouns:read"],
        days=2,
        account_id="account:issuer",
    )
    body = {
        "label": "child",
        "queues": ["production"],
        "kinds": ["small"],
        "effect_classes": ["read"],
        "scopes": ["nouns:read"],
        "days": 1,
    }
    assert client.post("/v1/keys", json=body, headers=auth(issuer)).status_code == 201
    for change in (
        {"scopes": ["admin"]},
        {"effect_classes": ["write-repo"]},
        {"kinds": ["*"]},
        {"days": 3},
        {"account_id": "account:other"},
        {"scopes": ["nouns:read", "surface:shamash"]},
    ):
        assert (
            client.post("/v1/keys", json={**body, **change}, headers=auth(issuer)).status_code
            == 403
        )
    renamed = client.post("/v1/keys", json={**body, "label": "shamash"}, headers=auth(issuer))
    assert renamed.status_code == 201
    renamed_grant = store.find_key(renamed.json()["key"])
    assert renamed_grant is not None
    assert renamed_grant.bound_surface() is None
    assert renamed_grant.scopes == body["scopes"]


def test_browser_cors_keeps_host_validation(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "origin.db"))
    settings = ServiceSettings(
        public_url="http://testserver", allowed_hosts=("testserver",), reaper_seconds=86400
    )
    with TestClient(create_app(store, settings)) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "browser-agent", "version": "1"},
            },
        }
        headers = {
            "Origin": "https://agent.example.com",
            "Accept": "application/json, text/event-stream",
        }
        assert client.post("/mcp", json=body, headers=headers).status_code == 200
        assert (
            client.post(
                "/mcp", json=body, headers={**headers, "Host": "untrusted.example.com"}
            ).status_code
            == 421
        )
        assert (
            client.post("/mcp", json=body, headers={**headers, "Origin": "null"}).status_code == 403
        )
        assert client.post("/v1/session/anonymous", json={}).status_code == 503
    store.close()


def test_delegation_cannot_drop_fine_mode_or_namespace(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    client, store, _, _ = access
    issuer, _ = store.issue_key(
        "issuer", queues=["production"], scopes=["admin:keys", "store", "nouns:read"], days=2
    )
    body = {"label": "child", "queues": ["production"], "scopes": ["store"], "days": 1}
    assert client.post("/v1/keys", json=body, headers=auth(issuer)).status_code == 403
    scoped, _ = store.issue_key(
        "scoped",
        queues=["production"],
        scopes=["admin:keys", "nouns:read", "namespace:alpha"],
        days=2,
    )
    body["scopes"] = ["nouns:read"]
    assert client.post("/ns/alpha/v1/keys", json=body, headers=auth(scoped)).status_code == 403
    body["scopes"] = ["nouns:read", "namespace:alpha"]
    assert client.post("/ns/alpha/v1/keys", json=body, headers=auth(scoped)).status_code == 201


def test_denied_and_no_op_requests_are_counted_not_written_per_request(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    """Same rule as the Worker: unauthenticated requests write nothing, denied and empty claims
    are counted per minute, and successful actions keep their own audit row."""
    client, store, _clock, admin = access
    store.define_queue(QueueConfig(name="conformance-counted"))
    worker, _ = store.issue_key(
        "worker", queues=["conformance-counted"], scopes=["verbs:claim", "nouns:read"]
    )

    def rows() -> int:
        with store._read() as c:
            return int(c.execute("SELECT COUNT(*) FROM access_events").fetchone()[0])

    assert client.get("/v1/counts", headers=auth(worker)).status_code == 200
    before = rows()
    assert client.get("/v1/counts").status_code == 401
    assert client.get("/v1/counts", headers=auth("example-invalid-bearer")).status_code == 401
    claim = {"queue": "conformance-counted", "worker": "w"}
    for _ in range(5):
        assert client.post("/v1/tasks/claim", json=claim, headers=auth(worker)).status_code == 204
    for _ in range(3):
        assert client.post("/v1/admin/reap", json={}, headers=auth(worker)).status_code == 403
    assert rows() == before
    audit = client.get("/v1/me/audit", headers=auth(worker)).json()
    counted = {(c["action"], c["outcome"]): c["count"] for c in audit["counters"]}
    assert counted[("claim", "empty")] == 5
    assert sum(n for (action, _), n in counted.items() if action != "claim") == 3
    assert audit["counters_note"]
    assert client.get("/v1/counts", headers=auth(worker)).status_code == 200
    assert rows() == before + 2  # the audit read above and this successful read
    assert client.get("/v1/admin/audit", headers=auth(admin)).json()["counters"]


def test_unhandled_errors_reach_the_audit_counters(tmp_path: Path) -> None:
    """H-61: a fault no handler names still counts, as 500:internal (the Worker's outcome for a
    storage fault). Before, only Tessera's own errors were audited, so an operator reading the
    audit never saw Python's 500s."""
    store = Store(str(tmp_path / "faults.db"), clock=FakeClock())
    worker, _ = store.issue_key("worker", queues=["*"], scopes=["nouns:read"])
    settings = ServiceSettings(reaper_seconds=86400, public_url="http://testserver")

    def broken() -> Any:
        raise RuntimeError("example fault")

    with TestClient(create_app(store, settings), raise_server_exceptions=False) as client:
        original = store.counts
        store.counts = broken  # type: ignore[method-assign]
        try:
            assert client.get("/v1/counts", headers=auth(worker)).status_code == 500
        finally:
            store.counts = original  # type: ignore[method-assign]
        audit = client.get("/v1/me/audit", headers=auth(worker)).json()
    assert any(c["outcome"] == "500:internal" for c in audit["counters"]), audit["counters"]
    store.close()


def test_ipv6_prefix_grouping_and_paced_global_sessions_match_the_worker(
    access: tuple[TestClient, Store, FakeClock, str],
) -> None:
    from tessera._principals import ip_group, mint_allowance

    _client, store, _clock, _admin = access
    assert ip_group("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert ip_group("2001:0db8:0001:0002:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert ip_group("[2001:db8:1:3::9]") == "2001:db8:1:3::/64"
    assert ip_group("::ffff:192.0.2.7") == "192.0.2.7"
    assert ip_group("not an address") == "not an address"
    same = store.request_identity("2001:db8:1:2::1", "ua")["ip_hash"]
    assert store.request_identity("2001:db8:1:2:aaaa::1", "ua")["ip_hash"] == same
    assert store.request_identity("2001:db8:1:3::1", "ua")["ip_hash"] != same
    assert mint_allowance(1000, 0, 0)[0] == 100
    assert mint_allowance(1000, 1800, 0)[0] == 600
    assert mint_allowance(1000, 3599, 0)[0] == 1000
    assert mint_allowance(1000, 60, 116) == (116, 2)
    assert mint_allowance(1000, 3000, 1000)[1] == 600
