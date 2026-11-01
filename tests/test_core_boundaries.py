"""Privacy over version history, strict outcomes, deadline boundaries and category plans."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import BucketConfig, ChildSpec, FakeClock, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.errors import NotFound, StaleLease
from tessera.service import create_app


@pytest.mark.parametrize("kind", ["record", "object"])
def test_history_intersects_version_and_head_acl(tmp_path: Path, kind: str) -> None:
    store = Store(tmp_path / "history.db")
    store.define_bucket(BucketConfig(name="fixture"))

    def put(version: int, visibility: Any, tags: list[str]) -> None:
        if kind == "record":
            store.put_record(
                "fixture",
                "key",
                {"example_only": True, "version": version + 1},
                expected_version=version,
                visibility_scope=visibility,
                tags=tags,
            )
        else:
            store.put_object(
                "fixture",
                "key",
                f"EXAMPLE {version}".encode(),
                expected_version=version,
                visibility_scope=visibility,
                tags=tags,
            )

    def get(version: int) -> Any:
        return (
            store.get_record("fixture", "key", version)
            if kind == "record"
            else store.get_object("fixture", "key", version)
        )

    def history() -> Any:
        return (
            store.record_history("fixture", "key")
            if kind == "record"
            else store.object_history("fixture", "key")
        )

    try:
        with store.scope_context(key_id="owner", surface="eranos"):
            put(0, "private", ["status:quarantined"])
            assert get(1)
        with store.scope_context(key_id="other", surface="eranos"), pytest.raises(NotFound):
            get(1)
        with store.scope_context(key_id="owner", surface="eranos"):
            put(1, "public_commons", [])
        with store.scope_context(key_id="other", surface="eranos"):
            with pytest.raises(NotFound):
                get(1)
            assert [row.version for row in history()] == [2]
        with store.scope_context(key_id="owner", surface="eranos"):
            put(2, "private", [])
        with store.scope_context(key_id="other", surface="eranos"):
            with pytest.raises(NotFound):
                get(2)
            with pytest.raises(NotFound):
                history()
        with store.scope_context(key_id="owner", surface="eranos"):
            assert [row.version for row in history()] == [1, 2, 3]
    finally:
        store.close()


def test_quarantined_blob_still_requires_private_owner(tmp_path: Path) -> None:
    store = Store(tmp_path / "blob.db")
    try:
        with store.scope_context(key_id="owner", surface="eranos"):
            digest = store.put_blob(
                b"EXAMPLE private blob", visibility_scope="private", tags=["status:quarantined"]
            )
            assert store.get_blob(digest) == b"EXAMPLE private blob"
        with store.scope_context(key_id="other", surface="eranos"), pytest.raises(NotFound):
            store.get_blob(digest)
    finally:
        store.close()


@pytest.mark.parametrize("method", ["heartbeat", "submit", "split", "park", "register_job"])
def test_lease_time_is_checked_after_transaction_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    clock = FakeClock()
    store = Store(tmp_path / "deadline.db", clock=clock)
    store.define_queue(QueueConfig(name="conformance-deadline"))
    store.enqueue(TaskSpec(queue="conformance-deadline", brief={"example_only": True}))
    lease = store.claim("conformance-deadline", "example")
    assert lease is not None
    original_tx = store._tx

    @contextmanager
    def delayed_tx() -> Any:
        with original_tx() as connection:
            clock.advance(lease.expires_at - clock.now() + 1)
            yield connection

    monkeypatch.setattr(store, "_tx", delayed_tx)
    action = {
        "heartbeat": lambda: store.heartbeat(lease.task_id, lease.fencing_token),
        "submit": lambda: store.submit(
            lease.task_id, lease.fencing_token, {}, idempotency_key="late"
        ),
        "split": lambda: store.split(lease.task_id, lease.fencing_token, [ChildSpec(brief={})]),
        "park": lambda: store.park(lease.task_id, lease.fencing_token, reason="late"),
        "register_job": lambda: store.register_job(
            lease.task_id, lease.fencing_token, "example-job", "example-model"
        ),
    }[method]
    try:
        with pytest.raises(StaleLease):
            action()
    finally:
        store.close()


def test_claim_deadlines_start_after_transaction_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    store = Store(tmp_path / "claim-deadline.db", clock=clock)
    store.define_queue(QueueConfig(name="conformance-deadline"))
    store.enqueue(TaskSpec(queue="conformance-deadline", brief={"example_only": True}))
    original_tx = store._tx

    @contextmanager
    def delayed_tx() -> Any:
        with original_tx() as connection:
            clock.advance(600)
            yield connection

    monkeypatch.setattr(store, "_tx", delayed_tx)
    try:
        lease = store.claim("conformance-deadline", "example")
        assert lease is not None
        assert lease.expires_at > clock.now()
        assert lease.hard_expires_at > clock.now()
    finally:
        store.close()


def test_http_and_mcp_strict_boolean_outcome(tmp_path: Path) -> None:
    store = Store(tmp_path / "boolean.db")
    store.define_queue(QueueConfig(name="conformance-boolean"))
    token, _ = store.issue_key("example", queues=["conformance-boolean"], scopes=["store"])
    app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        client.post(
            "/v1/tasks",
            headers=headers,
            json={"queue": "conformance-boolean", "brief": {"example_only": True}},
        ).raise_for_status()
        claimed = client.post(
            "/v1/tasks/claim",
            headers=headers,
            json={"queue": "conformance-boolean", "worker": "example"},
        ).json()
        args = {
            "task_id": claimed["task_id"],
            "fencing_token": claimed["fencing_token"],
            "output": {},
        }
        client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "example", "version": "1"},
                },
            },
        ).raise_for_status()
        for invalid in ["false", "true", 0, 1, None, [], {}]:
            response = client.post(
                "/v1/tasks/submit",
                headers={**headers, "Idempotency-Key": "invalid"},
                json={**args, "ok": invalid},
            )
            assert 400 <= response.status_code < 500
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "submit",
                        "arguments": {**args, "ok": invalid, "idempotency_key": "invalid"},
                    },
                },
            )
            assert response.json()["result"]["isError"] is True
            assert store.task(claimed["task_id"]).state == "leased"
        valid = client.post(
            "/v1/tasks/submit",
            headers={**headers, "Idempotency-Key": "real-failure"},
            json={**args, "ok": False},
        )
        assert valid.status_code == 200
        assert valid.json()["ok"] is False
        assert valid.json()["state_after"] != "done"


def test_category_filter_and_fragility_digest_http(tmp_path: Path) -> None:
    store = Store(tmp_path / "categories.db")
    token, _ = store.issue_key("example", queues=["*"], scopes=["store"])
    for key, category in [("a-other", "noun.code_node"), ("b-match", "noun.mcp_server")]:
        store.put_record(
            "fixture",
            key,
            {"category_id": category, "trials": 10, "successes": 10},
            expected_version=0,
        )
    digest = "a" * 64
    store.put_record(
        "feeds", "example", {"content_sha256": digest, "last_verified_at": 1}, expected_version=0
    )
    app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + token}
        response = client.get("/v1/rankings/noun.mcp_server?store=fixture&limit=1", headers=headers)
        assert response.status_code == 200
        assert [(r["id"], r["category_id"]) for r in response.json()["ranked"]] == [
            ("b-match", "noun.mcp_server")
        ]
        response = client.get(
            "/v1/fragility/plan?store=feeds&simulate_hours_ahead=0", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["plan"][0]["expected_sha256"] == digest
