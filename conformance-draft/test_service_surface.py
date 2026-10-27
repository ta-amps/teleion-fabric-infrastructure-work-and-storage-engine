"""Service-only requirements (SPEC §7): keys, POST-only writes, health over HTTP, the MCP door and
secret hygiene. They run only against a service you own:

  TESSERA_URL=http://localhost:8080
  TESSERA_TEST_KEYS='{"admin": "...", "q_a": "...", "q_b": "..."}'

`admin` has the admin scope; `q_a` may use only queue conformance-a; `q_b` only conformance-b.
Optionally TESSERA_LOG_PATH points at the service's log file for the T16 scan.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.fabric_service

URL = os.environ.get("TESSERA_URL", "")
KEYS: dict[str, str] = json.loads(os.environ.get("TESSERA_TEST_KEYS", "{}"))
needs_service = pytest.mark.skipif(
    not URL or not KEYS, reason="set TESSERA_URL and TESSERA_TEST_KEYS"
)
WRITE_PATHS = [
    "/v1/tasks",
    "/v1/tasks/claim",
    "/v1/tasks/heartbeat",
    "/v1/tasks/submit",
    "/v1/tasks/split",
    "/v1/tasks/park",
    "/v1/tasks/resume",
    "/v1/tasks/cancel",
    "/v1/queues/define",
    "/v1/admin/reap",
    "/v1/jobs/resp_x",
    "/v1/stores/batch",
    "/v1/blobs",
    "/work/resume",
]


def auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


@pytest.fixture
def http() -> Any:
    with httpx.Client(base_url=URL, timeout=30) as client:
        for name in ("conformance-a", "conformance-b"):
            client.post("/v1/queues/define", json={"name": name}, headers=auth(KEYS["admin"]))
        yield client


@needs_service
def test_T01_keys_are_required(http: httpx.Client) -> None:
    body = {"queue": "conformance-a", "worker": "w-anon"}
    assert http.post("/v1/tasks/claim", json=body).status_code == 401
    other = {"queue": "conformance-b", "worker": "w-a"}
    assert http.post("/v1/tasks/claim", json=other, headers=auth(KEYS["q_a"])).status_code == 403


@needs_service
def test_T02_writes_are_post_only(http: httpx.Client) -> None:
    before = http.get("/v1/counts", headers=auth(KEYS["admin"])).json()["queues"]
    for path in WRITE_PATHS:
        assert http.get(path, headers=auth(KEYS["admin"])).status_code == 405, path
    after = http.get("/v1/counts", headers=auth(KEYS["admin"])).json()["queues"]
    assert before == after


@needs_service
def test_T10_health_over_http(http: httpx.Client) -> None:
    resp = http.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_ok"] is True and "reaper_last_run" in body and isinstance(body["queues"], list)


@needs_service
def test_T11_an_mcp_client_can_claim_and_submit(http: httpx.Client) -> None:
    """Raw MCP JSON-RPC over Streamable HTTP, so the test doesn't depend on any one SDK version."""
    task = {"queue": "conformance-a", "brief": {"conformance": "T11"}}
    assert http.post("/v1/tasks", json=task, headers=auth(KEYS["admin"])).status_code == 201
    headers = {**auth(KEYS["q_a"]), "accept": "application/json, text/event-stream"}

    def rpc(method: str, params: dict[str, Any], version: str | None = None) -> dict[str, Any]:
        extra = {"mcp-protocol-version": version} if version else {}
        resp = http.post(
            "/mcp",
            headers={**headers, **extra},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body["result"]

    init = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "0"},
        },
    )
    version = init["protocolVersion"]
    names = {t["name"] for t in rpc("tools/list", {}, version)["tools"]}
    assert {"claim", "heartbeat", "submit", "split"} <= names
    lease = rpc(
        "tools/call",
        {"name": "claim", "arguments": {"queue": "conformance-a", "worker": "mcp"}},
        version,
    )
    lease_body = lease["structuredContent"]["lease"]
    submitted = rpc(
        "tools/call",
        {
            "name": "submit",
            "arguments": {
                "task_id": lease_body["task_id"],
                "fencing_token": lease_body["fencing_token"],
                "output": {"via": "mcp"},
                "idempotency_key": uuid.uuid4().hex,
            },
        },
        version,
    )
    assert not submitted.get("isError")


@needs_service
def test_T16_no_secret_leaks(http: httpx.Client) -> None:
    probes = [
        http.get("/v1/health"),
        http.get("/v1/counts", headers=auth(KEYS["admin"])),
        http.get("/v1/events", headers=auth(KEYS["admin"])),
        http.get("/board"),
        http.post("/v1/tasks/claim", json={"queue": "conformance-a", "worker": "w"}),
        http.post(
            "/v1/tasks/claim",
            json={"queue": "conformance-b", "worker": "w"},
            headers=auth(KEYS["q_a"]),
        ),
        http.get("/v1/tasks/claim", headers=auth(KEYS["q_a"])),
        http.get("/v1/tasks/does-not-exist", headers=auth(KEYS["admin"])),
    ]
    texts = [r.text + json.dumps(dict(r.headers)) for r in probes]
    log_path = os.environ.get("TESSERA_LOG_PATH")
    if log_path:
        texts.append(Path(log_path).read_text(encoding="utf-8", errors="replace"))
    for key in KEYS.values():
        assert all(key not in text for text in texts)


@needs_service
def test_T22_resume_requires_the_tasks_effect_class(http: httpx.Client) -> None:
    admin_hdr = auth(KEYS["admin"])
    q_name = f"conformance-t22-{uuid.uuid4().hex[:8]}"
    assert (
        http.post("/v1/queues/define", json={"name": q_name}, headers=admin_hdr).status_code == 200
    )

    issued_ids: list[str] = []

    def issue(label: str, effects: list[str]) -> str:
        resp = http.post(
            "/v1/admin/keys",
            headers=admin_hdr,
            json={"label": label, "queues": [q_name], "effect_classes": effects},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        issued_ids.append(str(data["key_id"]))
        return str(data["key"])

    def mcp_resume(secret: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        mcp_hdr = {**auth(secret), "accept": "application/json, text/event-stream"}
        init_resp = http.post(
            "/mcp",
            headers=mcp_hdr,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t22", "version": "0"},
                },
            },
        )
        assert init_resp.status_code == 200, init_resp.text
        ver = init_resp.json()["result"]["protocolVersion"]
        call_resp = http.post(
            "/mcp",
            headers={**mcp_hdr, "mcp-protocol-version": ver},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "resume",
                    "arguments": {"task_id": task_id, "input": payload},
                },
            },
        )
        assert call_resp.status_code == 200, call_resp.text
        res: dict[str, Any] = call_resp.json()["result"]
        return res

    try:
        parker_key = issue("t22-parker", ["read", "write-internal", "write-repo"])
        read_only_key = issue("t22-read", ["read"])
        worker_key = issue("t22-worker", ["read", "write-internal", "write-repo"])

        # 1. A read-only key cannot resume a write-internal task across HTTP, MCP, or /work.
        enq1 = http.post(
            "/v1/tasks",
            headers=admin_hdr,
            json={
                "queue": q_name,
                "brief": {"case": "write-internal"},
                "effect_class": "write-internal",
            },
        )
        assert enq1.status_code == 201
        t1 = enq1.json()["task_id"]
        lease1 = http.post(
            "/v1/tasks/claim",
            headers=auth(parker_key),
            json={"queue": q_name, "worker": "parker-1"},
        ).json()
        assert (
            http.post(
                "/v1/tasks/park",
                headers=auth(parker_key),
                json={
                    "task_id": t1,
                    "fencing_token": lease1["fencing_token"],
                    "reason": "need review",
                },
            ).status_code
            == 200
        )

        r_http1 = http.post(
            "/v1/tasks/resume",
            headers=auth(read_only_key),
            json={"task_id": t1, "input": {"ok": True}},
        )
        assert r_http1.status_code == 403 and r_http1.json()["code"] == "scope_denied"

        r_mcp1 = mcp_resume(read_only_key, t1, {"ok": True})
        assert r_mcp1.get("isError") is True and "scope_denied" in r_mcp1["content"][0]["text"]

        r_work1 = http.post(
            "/work/resume",
            data={"key": read_only_key, "task_id": t1, "input": '{"ok": true}'},
        )
        assert r_work1.status_code == 403 and "scope_denied" in r_work1.text
        assert read_only_key not in r_work1.text

        ok1 = http.post(
            "/v1/tasks/resume",
            headers=auth(worker_key),
            json={"task_id": t1, "input": {"ok": True}},
        )
        assert ok1.status_code == 200 and ok1.json()["state"] == "ready"

        # Drain t1 so queue is empty for case 2.
        drain1 = http.post(
            "/v1/tasks/claim",
            headers=auth(worker_key),
            json={"queue": q_name, "worker": "drain-1"},
        ).json()
        assert (
            http.post(
                "/v1/tasks/submit",
                headers={**auth(worker_key), "idempotency-key": f"{t1}:{drain1['fencing_token']}"},
                json={
                    "task_id": t1,
                    "fencing_token": drain1["fencing_token"],
                    "output": {"done": True},
                },
            ).status_code
            == 200
        )

        # 2. A non-admin key cannot resume a write-repo task across HTTP, MCP, or /work; admin can.
        enq2 = http.post(
            "/v1/tasks",
            headers=admin_hdr,
            json={"queue": q_name, "brief": {"case": "write-repo"}, "effect_class": "write-repo"},
        )
        assert enq2.status_code == 201
        t2 = enq2.json()["task_id"]
        lease2 = http.post(
            "/v1/tasks/claim",
            headers=auth(parker_key),
            json={"queue": q_name, "worker": "parker-2"},
        ).json()
        assert (
            http.post(
                "/v1/tasks/park",
                headers=auth(parker_key),
                json={
                    "task_id": t2,
                    "fencing_token": lease2["fencing_token"],
                    "reason": "need admin",
                },
            ).status_code
            == 200
        )

        r_http2 = http.post(
            "/v1/tasks/resume",
            headers=auth(worker_key),
            json={"task_id": t2, "input": {"approved": True}},
        )
        assert r_http2.status_code == 403 and r_http2.json()["code"] == "scope_denied"

        r_mcp2 = mcp_resume(worker_key, t2, {"approved": True})
        assert r_mcp2.get("isError") is True and "scope_denied" in r_mcp2["content"][0]["text"]

        r_work2 = http.post(
            "/work/resume",
            data={"key": worker_key, "task_id": t2, "input": '{"approved": true}'},
        )
        assert r_work2.status_code == 403 and "scope_denied" in r_work2.text
        assert worker_key not in r_work2.text

        ok_work_admin = http.post(
            "/work/resume",
            data={"key": KEYS["admin"], "task_id": t2, "input": '{"approved": true}'},
        )
        assert ok_work_admin.status_code == 200 and "ready" in ok_work_admin.text
        assert KEYS["admin"] not in ok_work_admin.text
    finally:
        for kid in issued_ids:
            http.post(f"/v1/admin/keys/{kid}/revoke", headers=admin_hdr)
