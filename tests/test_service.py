"""The HTTP service: keys, scopes, POST-only writes, holders, rate limits, the edge lock, the agent door,
the web forms and the MCP door, all on a real app over an in-process client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store
from tessera.service import WRITE_PATHS, ServiceSettings, create_app

Keys = dict[str, str]


def auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[TestClient, Store, Keys]]:
    store = Store(str(tmp_path / "t.db"))
    keys = {
        "admin": store.issue_key("admin", queues=["*"], scopes=["admin", "store"])[0],
        "a": store.issue_key("worker-a", queues=["q-a"])[0],
        "a2": store.issue_key("worker-a2", queues=["q-a"])[0],
        "b": store.issue_key("worker-b", queues=["q-b"])[0],
    }
    for name in ("q-a", "q-b"):
        store.define_queue(QueueConfig(name=name))
    settings = ServiceSettings(
        db_path=str(tmp_path / "t.db"),
        claims_per_minute=5,
        reaper_seconds=3600,
        public_url="http://testserver",
    )
    with TestClient(create_app(store, settings)) as client:
        yield client, store, keys
    store.close()


def enqueue(client: TestClient, keys: Keys, queue: str = "q-a", **extra: Any) -> str:
    resp = client.post(
        "/v1/tasks", json={"queue": queue, "brief": {"n": 1}, **extra}, headers=auth(keys["admin"])
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["task_id"])


def test_health_is_public(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, _ = service
    assert client.get("/v1/health").json()["db_ok"] is True
    assert client.get("/healthz").status_code == 200


def test_keys_are_required_and_scoped(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    resp = client.post("/v1/tasks/claim", json={"queue": "q-a", "worker": "w"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "unauthorized"
    resp = client.post(
        "/v1/tasks/claim", json={"queue": "q-b", "worker": "w"}, headers=auth(keys["a"])
    )
    assert resp.status_code == 403


def test_writes_are_post_only(service: tuple[TestClient, Store, Keys]) -> None:
    client, store, keys = service
    before = store.counts()
    for path in WRITE_PATHS:
        concrete = path.replace("{job_id}", "resp_x")
        assert client.get(concrete, headers=auth(keys["admin"])).status_code == 405, concrete
    assert store.counts() == before


def test_claim_heartbeat_submit(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    task = enqueue(client, keys)
    lease = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    )
    assert lease.status_code == 200 and lease.json()["task_id"] == task
    token = lease.json()["fencing_token"]
    ref = {"task_id": task, "fencing_token": token}
    assert client.post("/v1/tasks/heartbeat", json=ref, headers=auth(keys["a"])).status_code == 200
    body = {**ref, "output": {"answer": 42}}
    assert client.post("/v1/tasks/submit", json=body, headers=auth(keys["a"])).status_code == 400
    headers = {**auth(keys["a"]), "idempotency-key": f"{task}:{token}"}
    first = client.post("/v1/tasks/submit", json=body, headers=headers)
    again = client.post("/v1/tasks/submit", json=body, headers=headers)
    assert first.status_code == 200 and first.json()["state_after"] == "done"
    assert again.json()["replayed"] is True
    assert again.json()["receipt_hash"] == first.json()["receipt_hash"]


def test_only_the_claiming_key_may_change_a_task(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    task = enqueue(client, keys)
    token = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    ).json()["fencing_token"]
    body = {"task_id": task, "fencing_token": token, "output": {}}
    resp = client.post(
        "/v1/tasks/submit", json=body, headers={**auth(keys["a2"]), "idempotency-key": "k"}
    )
    assert resp.status_code == 403


def test_an_empty_queue_answers_204(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    resp = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    )
    assert resp.status_code == 204 and "retry-after" in resp.headers


def test_claims_are_rate_limited(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    codes = [
        client.post(
            "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
        ).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [204] * 5 and codes[5] == 429


def test_public_effect_tasks_never_reach_agents(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    enqueue(client, keys, effect_class="public")
    resp = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    )
    assert resp.status_code == 204
    resp = client.post(
        "/v1/tasks",
        json={"queue": "q-a", "brief": {}, "effect_class": "public"},
        headers=auth(keys["a"]),
    )
    assert resp.status_code == 403


def test_storage_needs_the_store_scope(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    path = "/v1/stores/notes/records/n-1"
    assert client.post(path, json={"value": 1}, headers=auth(keys["a"])).status_code == 403
    assert client.post(path, json={"value": 1}, headers=auth(keys["admin"])).json()["version"] == 1
    conflict = client.post(path, json={"value": 2}, headers=auth(keys["admin"]))
    assert conflict.status_code == 409 and conflict.json()["current_version"] == 1
    blob = client.post("/v1/blobs", content=b"SYNTHETIC", headers=auth(keys["admin"]))
    assert (
        client.get(f"/v1/blobs/{blob.json()['sha256']}", headers=auth(keys["admin"])).content
        == b"SYNTHETIC"
    )


def test_the_edge_lock(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "e.db"))
    admin, _ = store.issue_key("admin", queues=["*"], scopes=["admin"])
    settings = ServiceSettings(
        db_path=str(tmp_path / "e.db"),
        edge_secret="edge-test-value",
        public_url="http://testserver",
    )
    with TestClient(create_app(store, settings)) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/counts", headers=auth(admin)).status_code == 403
        locked = client.get("/v1/counts", headers={**auth(admin), "x-edge-auth": "edge-test-value"})
        assert locked.status_code == 200
    store.close()


def test_the_agent_door(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, _ = service
    assert "/mcp" in client.get("/connect").text
    llms = client.get("/llms.txt").text
    assert llms.startswith("# Tessera") and "## Rules" in llms
    assert client.get("/.well-known/agent-card.json").json()["name"] == "Tessera"
    assert "Disallow: /v1/" in client.get("/robots.txt").text
    assert "q-a" in client.get("/board").text


def test_web_forms_never_echo_the_key(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    task = enqueue(client, keys)
    page = client.post("/work/claim", data={"key": keys["a"], "queue": "q-a", "worker": "browser"})
    assert page.status_code == 200 and task in page.text and keys["a"] not in page.text
    token = int(page.text.split('name="fencing_token" value="')[1].split('"')[0])
    done = client.post(
        "/work/submit",
        data={
            "key": keys["a"],
            "task_id": task,
            "fencing_token": token,
            "idempotency_key": f"{task}:{token}",
            "output": json.dumps({"result": "ok"}),
        },
    )
    assert "Submitted" in done.text and keys["a"] not in done.text
    assert (
        client.post("/work/claim", data={"key": "wrong", "queue": "q-a", "worker": "x"}).status_code
        == 401
    )


def _rpc(
    client: TestClient,
    key: str | None,
    method: str,
    params: dict[str, Any],
    version: str | None = None,
) -> Any:
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if key:
        headers.update(auth(key))
    if version:
        headers["mcp-protocol-version"] = version
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def test_the_mcp_door(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "conformance", "version": "0"},
    }
    assert _rpc(client, None, "initialize", init).status_code == 401
    resp = _rpc(client, keys["a"], "initialize", init)
    assert resp.status_code == 200, resp.text
    version = resp.json()["result"]["protocolVersion"]
    names = {
        t["name"]
        for t in _rpc(client, keys["a"], "tools/list", {}, version).json()["result"]["tools"]
    }
    assert {"claim", "heartbeat", "submit", "split", "queues"} <= names
    task = enqueue(client, keys)
    called = _rpc(
        client,
        keys["a"],
        "tools/call",
        {"name": "claim", "arguments": {"queue": "q-a", "worker": "mcp"}},
        version,
    ).json()
    lease = called["result"]["structuredContent"]["lease"]
    assert lease["task_id"] == task
    submitted = _rpc(
        client,
        keys["a"],
        "tools/call",
        {
            "name": "submit",
            "arguments": {
                "task_id": task,
                "fencing_token": lease["fencing_token"],
                "output": {"via": "mcp"},
                "idempotency_key": f"{task}:{lease['fencing_token']}",
            },
        },
        version,
    ).json()
    assert submitted["result"]["structuredContent"]["receipt"]["state_after"] == "done"


def test_no_key_appears_in_any_response(service: tuple[TestClient, Store, Keys]) -> None:
    client, _, keys = service
    responses = [
        client.get("/v1/health"),
        client.get("/board"),
        client.get("/connect"),
        client.get("/v1/counts", headers=auth(keys["admin"])),
        client.get("/v1/events", headers=auth(keys["admin"])),
        client.post(
            "/v1/tasks/claim", json={"queue": "q-b", "worker": "w"}, headers=auth(keys["a"])
        ),
        client.get("/v1/tasks/claim", headers=auth(keys["a"])),
    ]
    for resp in responses:
        text = resp.text + json.dumps(dict(resp.headers))
        assert not any(k in text for k in keys.values())


def test_dependencies_stay_inside_the_key_scope(service: tuple[TestClient, Store, Keys]) -> None:
    """A dependent's lease carries its dependencies' outputs, so a key may only depend on tasks in
    queues it can use."""
    client, store, keys = service
    other = enqueue(client, keys, queue="q-b")
    resp = client.post(
        "/v1/tasks",
        json={"queue": "q-a", "brief": {}, "depends_on": [other]},
        headers=auth(keys["a"]),
    )
    assert resp.status_code == 403
    own = enqueue(client, keys, queue="q-a")
    resp = client.post(
        "/v1/tasks",
        json={"queue": "q-a", "brief": {}, "depends_on": [own]},
        headers=auth(keys["a"]),
    )
    assert resp.status_code == 201
    assert store.task(resp.json()["task_id"]).state == "waiting"


def test_park_and_resume_follow_the_holder_and_queue_rules(
    service: tuple[TestClient, Store, Keys],
) -> None:
    client, store, keys = service
    task = enqueue(client, keys)
    lease = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    ).json()
    park = {"task_id": task, "fencing_token": lease["fencing_token"], "reason": "need documents"}
    assert client.post("/v1/tasks/park", json=park, headers=auth(keys["a2"])).status_code == 403
    resp = client.post("/v1/tasks/park", json=park, headers=auth(keys["a"]))
    assert resp.status_code == 200 and resp.json()["wait_reason"] == "input"
    resume = {"task_id": task, "input": {"documents": ["payslip"]}}
    assert client.post("/v1/tasks/resume", json=resume, headers=auth(keys["b"])).status_code == 403
    assert client.post("/v1/tasks/resume", json=resume, headers=auth(keys["a2"])).status_code == 200
    again = client.post("/v1/tasks/resume", json=resume, headers=auth(keys["a2"]))
    assert again.status_code == 409 and again.json()["code"] == "invalid_state"
    assert store.task(task).state == "ready"


def test_the_key_that_parked_a_task_may_not_resume_it(
    service: tuple[TestClient, Store, Keys],
) -> None:
    """Outside input comes from someone else: an agent that parks to wait for a person can't
    answer itself. Admin keys may; every resume records the key that made it."""
    client, store, keys = service
    task = enqueue(client, keys)
    lease = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    ).json()
    park = {"task_id": task, "fencing_token": lease["fencing_token"], "reason": "need approval"}
    assert client.post("/v1/tasks/park", json=park, headers=auth(keys["a"])).status_code == 200
    resume = {"task_id": task, "input": {"approved": True}}
    resp = client.post("/v1/tasks/resume", json=resume, headers=auth(keys["a"]))
    assert resp.status_code == 403
    assert client.post("/v1/tasks/resume", json=resume, headers=auth(keys["a2"])).status_code == 200
    resumed = [e for e in store.events() if e.type == "resumed" and e.task_id == task]
    assert len(resumed) == 1 and resumed[0].detail.get("by_key")
    assert store.task(task).parked_by is None


def test_split_children_need_an_effect_class_the_key_holds(
    service: tuple[TestClient, Store, Keys],
) -> None:
    client, _, keys = service
    task = enqueue(client, keys)
    lease = client.post(
        "/v1/tasks/claim", json={"queue": "q-a", "worker": "w"}, headers=auth(keys["a"])
    ).json()
    body = {
        "task_id": task,
        "fencing_token": lease["fencing_token"],
        "children": [{"brief": {}, "effect_class": "write-repo"}],
    }
    assert client.post("/v1/tasks/split", json=body, headers=auth(keys["a"])).status_code in {
        400,
        403,
    }
