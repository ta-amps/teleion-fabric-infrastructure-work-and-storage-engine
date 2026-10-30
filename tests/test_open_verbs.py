"""Open verbs: a tracked anonymous session claims, heartbeats and submits only verbs whose producer
opened them, reads only its own work, never approves or publishes, and is capped per session and
per IP hash."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import Forbidden, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app

QUARANTINE = {
    "producer_surface": "surface:eranos",
    "visibility_scope": "surface_only",
}
OPEN_TAGS = ["status:quarantined", "open:anonymous"]


@pytest.fixture
def fabric(tmp_path: Path) -> Iterator[tuple[TestClient, Store, str]]:
    store = Store(str(tmp_path / "open.db"), clock=FakeClock())
    admin, _ = store.issue_key(
        "operator", queues=["*"], scopes=["admin", "store"], account_id="account:operator"
    )
    for name in ("eq01", "eq02", "eq03"):
        store.define_queue(QueueConfig(name=name, lease_seconds=1800, max_attempts=10))
    settings = ServiceSettings(
        anonymous_access_enabled=True,
        reaper_seconds=86400,
        public_url="http://testserver",
        claims_per_minute=1000,
    )
    with TestClient(create_app(store, settings)) as client:
        yield client, store, admin
    store.close()


def auth(secret: str, **extra: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + secret, **extra}


def session(client: TestClient, label: str = "volunteer-agent") -> dict[str, Any]:
    response = client.post("/v1/session/anonymous", json={"agent_label": label})
    assert response.status_code == 201, response.text
    return response.json()


def build_verb(store: Store, task_id: str, *, tags: list[str] = OPEN_TAGS, **extra: Any) -> str:
    return store.enqueue(
        TaskSpec(
            queue=extra.pop("queue", "eq02"),
            task_id=task_id,
            kind="draft",
            effect_class="write-internal",
            brief={"title": "SYNTHETIC kit build", "kit_id": "KIT-OPEN-01"},
            tags=tags,
            **QUARANTINE,
            **extra,
        )
    )


def work(client: TestClient, secret: str, task_id: str | None = None) -> dict[str, Any]:
    """Claim, heartbeat and submit one open verb in the shared namespace."""
    h = auth(secret)
    claimed = client.post(
        "/ns/main/v1/tasks/claim", json={"queue": "eq02", "worker": "volunteer"}, headers=h
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()
    if task_id is not None:
        assert lease["task_id"] == task_id
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    assert client.post("/ns/main/v1/tasks/heartbeat", json=ref, headers=h).status_code == 200
    submitted = client.post(
        "/ns/main/v1/tasks/submit",
        json={**ref, "output": {"node": "draft", "note": "SYNTHETIC result, see example.com"}},
        headers={**h, "Idempotency-Key": f"{lease['task_id']}:{lease['fencing_token']}"},
    )
    assert submitted.status_code == 200, submitted.text
    return {"lease": lease, "receipt": submitted.json()}


def test_anonymous_session_works_only_opened_verbs_and_receipts_name_it(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    build_verb(store, "build-open")
    build_verb(store, "build-keyed", tags=["status:quarantined"])
    me = session(client)
    h = auth(me["session_token"])

    listed = client.get("/ns/main/v1/verbs?queue=eq02", headers=h)
    assert listed.status_code == 200
    assert [v["task_id"] for v in listed.json()["verbs"]] == ["build-open"]

    done = work(client, me["session_token"], "build-open")
    lease, receipt = done["lease"], done["receipt"]
    assert lease["namespace"] == "main" and lease["labels"]["origin"] == "anonymous"
    assert receipt["state_after"] == "done" and receipt["labels"]["untrusted"] is True
    assert receipt["labels"]["agent_label"] == "volunteer-agent"

    # The session can follow its own work; the operator sees who did it in the receipt.
    assert client.get("/ns/main/v1/tasks/build-open", headers=h).json()["state"] == "done"
    own = client.get("/ns/main/v1/tasks/build-open/results", headers=h).json()["results"]
    assert own[0]["receipt_hash"] == receipt["receipt_hash"]
    stored = store.results("build-open")[0]
    assert stored.completed_by_key == me["principal_id"]
    assert stored.labels["origin"] == "anonymous"

    # The keyed-only verb stays out of reach.
    nothing = client.post(
        "/ns/main/v1/tasks/claim", json={"queue": "eq02", "worker": "volunteer"}, headers=h
    )
    assert nothing.status_code == 204
    assert store.task("build-keyed").state == "ready"
    audit = client.get("/v1/me/audit", headers=h).json()["events"]
    assert {"claim", "heartbeat", "submit"} <= {
        e["action"] for e in audit if e["namespace"] == "main"
    }


def test_one_open_claim_at_a_time(fabric: tuple[TestClient, Store, str]) -> None:
    client, store, _ = fabric
    build_verb(store, "build-a")
    build_verb(store, "build-b")
    h = auth(session(client)["session_token"])
    body = {"queue": "eq02", "worker": "volunteer"}
    assert client.post("/ns/main/v1/tasks/claim", json=body, headers=h).status_code == 200
    second = client.post("/ns/main/v1/tasks/claim", json=body, headers=h)
    assert second.status_code == 429 and "retry-after" in second.headers


def test_review_approval_publication_and_nouns_stay_closed(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    build_verb(store, "build-1")
    build_verb(store, "review-1", queue="eq03", depends_on=["build-1"])
    me = session(client)
    h = auth(me["session_token"])
    work(client, me["session_token"], "build-1")
    assert store.task("review-1").state == "ready"

    # A review verb is closed even when its producer tagged it open.
    for path, body in (
        ("/ns/main/v1/tasks/claim", {"queue": "eq03", "worker": "volunteer"}),
        ("/ns/main/v1/stores/eranos.catalog/records/KIT-OPEN-01", {"value": {"kit": "x"}}),
        ("/ns/main/v1/stores/batch", {"writes": []}),
        ("/ns/main/v1/tasks", {"queue": "eq02", "brief": {"title": "spam"}}),
        ("/ns/main/v1/tasks/cancel", {"task_id": "build-1"}),
        ("/ns/main/v1/tasks/park", {"task_id": "build-1", "fencing_token": 1, "reason": "x"}),
        ("/ns/main/v1/tasks/resume", {"task_id": "review-1", "input": {"approved": True}}),
        ("/ns/main/v1/blobs", {}),
    ):
        response = client.post(path, json=body, headers=h)
        assert response.status_code == 403, (path, response.status_code, response.text)
    for path in (
        "/ns/main/v1/stores/eranos.catalog/records/KIT-OPEN-01",
        "/ns/main/v1/stores",
        "/ns/main/v1/search?q=kit",
        "/ns/main/v1/counts",
        "/ns/main/v1/queues",
        "/ns/main/v1/tasks/review-1",
    ):
        response = client.get(path, headers=h)
        assert response.status_code in {403, 404}, (path, response.status_code)
    assert store.task("review-1").state == "ready"

    # Even through the operator API the store refuses an anonymous approver of keyed work.
    build_verb(store, "build-2")
    build_verb(store, "review-2", queue="eq03", depends_on=["build-2"])
    drafted = store.claim("eq02", "keyed-volunteer", key_id="k_drafter")
    assert drafted is not None and drafted.task_id == "build-2"
    store.submit(
        "build-2",
        drafted.fencing_token,
        {
            "node": "draft",
            "blob_sha256": "0" * 64,
            "draft": {"kit_id": "KIT-OPEN-01"},
            "verification": {"passed": True},
        },
        idempotency_key="build-2",
        completed_by_key="k_drafter",
    )
    store.cancel("review-1", "not part of this check")
    review = store.claim("eq03", "volunteer", key_id=me["principal_id"])
    assert review is not None and review.task_id == "review-2"
    approval = {
        "approved": True,
        "review_task_id": "review-2",
        "draft_task_id": "build-2",
        "drafter_key_id": "k_drafter",
        "reviewer_key_id": me["principal_id"],
        "draft_sha256": "0" * 64,
        "kit_id": "KIT-OPEN-01",
    }
    with pytest.raises(Forbidden, match="anonymous session never approves"):
        store.submit(
            "review-2",
            review.fencing_token,
            approval,
            idempotency_key="anon-approval",
            completed_by_key=me["principal_id"],
        )
    assert store.task("review-2").state == "leased"


def test_other_sessions_and_strangers_cannot_read_a_submission(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    build_verb(store, "build-1")
    first, second = session(client, "first"), session(client, "second")
    work(client, first["session_token"], "build-1")
    other = auth(second["session_token"])
    for path in ("/ns/main/v1/tasks/build-1", "/ns/main/v1/tasks/build-1/results"):
        assert client.get(path, headers=other).status_code == 404, path
        assert client.get(path).status_code == 401, path
    # Claim events would show other attempts: closed to every anonymous session, its own work too.
    for session_token in (first["session_token"], second["session_token"]):
        events = client.get("/ns/main/v1/claims/build-1/events", headers=auth(session_token))
        assert events.status_code == 403
    assert client.get("/ns/main/v1/claims/build-1/events").status_code == 401
    # Cross-namespace stays closed: another session's demo namespace and any other namespace.
    assert client.get(f"/ns/{first['namespace']}/v1/verbs", headers=other).status_code == 403
    assert client.get("/ns/conformance-other/v1/verbs", headers=other).status_code == 403


def test_open_claims_are_capped_per_session_and_per_ip_hash(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    for i in range(12):
        build_verb(store, f"build-{i:02d}")
    one, two, three = (session(client, name) for name in ("one", "two", "three"))
    for _ in range(5):
        work(client, one["session_token"])
    capped = client.post(
        "/ns/main/v1/tasks/claim",
        json={"queue": "eq02", "worker": "volunteer"},
        headers=auth(one["session_token"]),
    )
    assert capped.status_code == 429 and int(capped.headers["retry-after"]) > 0
    for _ in range(5):
        work(client, two["session_token"])
    # A fresh session from the same address meets the per-IP-hash cap.
    blocked = client.post(
        "/ns/main/v1/tasks/claim",
        json={"queue": "eq02", "worker": "volunteer"},
        headers=auth(three["session_token"]),
    )
    assert blocked.status_code == 429
    assert store.counts()["eq02"].ready == 2


def test_claimant_gets_the_brief_not_related_outputs(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    store.enqueue(
        TaskSpec(
            queue="eq01", task_id="check-1", kind="spec", brief={"title": "check"}, **QUARANTINE
        )
    )
    lease = store.claim("eq01", "checker", key_id="k_checker")
    assert lease is not None
    store.submit(
        lease.task_id,
        lease.fencing_token,
        {"private": "checker notes"},
        idempotency_key="check-1",
        completed_by_key="k_checker",
    )
    build_verb(store, "build-1", depends_on=["check-1"])
    h = auth(session(client)["session_token"])
    claimed = client.post(
        "/ns/main/v1/tasks/claim", json={"queue": "eq02", "worker": "volunteer"}, headers=h
    ).json()
    assert claimed["brief"]["kit_id"] == "KIT-OPEN-01"
    assert claimed["dependencies"] == [
        {
            "task_id": "check-1",
            "state": "done",
            "ok": True,
            "output": None,
            "receipt_hash": claimed["dependencies"][0]["receipt_hash"],
            "completed_by_key": None,
        }
    ]


def test_mcp_door_applies_the_same_rule(fabric: tuple[TestClient, Store, str]) -> None:
    client, store, _ = fabric
    build_verb(store, "build-1")
    build_verb(store, "review-1", queue="eq03", depends_on=["build-1"])
    secret = session(client)["session_token"]

    def call(name: str, arguments: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = client.post(
            "/ns/main/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={"Accept": "application/json, text/event-stream", **auth(secret)},
        )
        body = response.json()
        return response.status_code, body.get("result", {}).get("structuredContent") or body

    status, listed = call("list_verbs", {"queue": "eq02"})
    assert status == 200 and [v["task_id"] for v in listed["verbs"]] == ["build-1"]
    status, claimed = call("claim", {"queue": "eq02", "worker": "volunteer"})
    assert status == 200 and claimed["lease"]["task_id"] == "build-1"
    assert claimed["lease"]["labels"]["origin"] == "anonymous"
    for name, arguments in (
        ("get_record", {"store_name": "eranos.catalog", "key": "KIT-OPEN-01"}),
        ("claim", {"queue": "eq03", "worker": "volunteer"}),
    ):
        status, denied = call(name, arguments)
        assert status == 403 and denied["code"] == "scope_denied", (name, denied)
