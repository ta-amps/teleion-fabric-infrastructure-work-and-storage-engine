"""Execution recall uses accepted results and rechecks source permissions and expiry."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import FakeClock, QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.service import create_app

NS = "conformance-memory"
QUEUE = "conformance-work"
REPORT: dict[str, Any] = {
    "version": 1,
    "harness": {"name": "synthetic-harness", "version": "1"},
    "model": {"name": "synthetic-model"},
    "contract": {
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "context_hash": "a" * 64,
        "environment_hash": "b" * 64,
        "toolchain_hash": "c" * 64,
    },
    "summaries": {
        "problem": "SYNTHETIC add two numbers",
        "input": "one and two",
        "solution": "three",
    },
    "usage": {"source": "worker_reported", "input_tokens": None, "reason": "not measured"},
    "blocks": [{"kind": "context", "text": "SYNTHETIC arithmetic fixture"}],
}


@dataclass
class Actor:
    token: str = field(repr=False)
    key_id: str
    namespace: str = NS


class MemoryHarness:
    def __init__(self, store: Store, clock: FakeClock, client: TestClient) -> None:
        self.store, self.clock, self.client = store, clock, client

    def actor(
        self, *, queues: list[str] | None = None, admin: bool = False, namespace: str = NS
    ) -> Actor:
        scopes = (
            ["admin"]
            if admin
            else [
                "nouns:read",
                "cas:write",
                "verbs:claim",
                "verbs:submit",
                "surface:memory_team",
                f"namespace:{namespace}",
            ]
        )
        token, grant = self.store.issue_key(
            "synthetic-memory", queues=queues or [QUEUE], scopes=scopes, days=30
        )
        return Actor(token, grant.key_id, namespace)

    def request(
        self,
        actor: Actor,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self.client.request(
            method,
            f"/ns/{actor.namespace}" + path,
            json=body,
            headers={"Authorization": "Bearer " + actor.token, **(headers or {})},
        )

    def claim(
        self,
        actor: Actor,
        task_id: str,
        *,
        visibility: str = "surface_only",
        brief: dict[str, Any] | None = None,
        queue: str = QUEUE,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            actor,
            "POST",
            "/v1/tasks",
            {
                "queue": queue,
                "task_id": task_id,
                "brief": brief or {"synthetic": True, "input": [1, 2]},
                "visibility_scope": visibility,
                "depends_on": depends_on or [],
            },
        )
        assert response.status_code == 201, response.text
        response = self.request(
            actor, "POST", "/v1/tasks/claim", {"queue": queue, "worker": "synthetic-worker"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["task_id"] == task_id
        return response.json()

    def submit(
        self,
        actor: Actor,
        lease: dict[str, Any],
        *,
        ok: bool = True,
        report: dict[str, Any] | None = None,
        idem: str | None = None,
    ) -> Any:
        return self.request(
            actor,
            "POST",
            "/v1/tasks/submit",
            {
                "task_id": lease["task_id"],
                "fencing_token": lease["fencing_token"],
                "output": {"synthetic": True, "sum": 3},
                "ok": ok,
                "error": None if ok else "SYNTHETIC failed check",
                "execution_report": copy.deepcopy(REPORT if report is None else report),
            },
            {"Idempotency-Key": idem or lease["task_id"]},
        )

    def recall(
        self, actor: Actor, lease: dict[str, Any], report: dict[str, Any] | None = None
    ) -> Any:
        return self.request(
            actor,
            "POST",
            "/v1/executions/recall",
            {
                "task_id": lease["task_id"],
                "fencing_token": lease["fencing_token"],
                "execution_report": copy.deepcopy(REPORT if report is None else report),
                "limit": 10,
            },
        )

    def run_id(self, task_id: str, namespace: str = NS) -> str:
        with self.store._read() as connection:
            row = connection.execute(
                "SELECT run_id FROM execution_runs WHERE namespace=? AND task_id=?",
                (namespace, task_id),
            ).fetchone()
        assert row is not None
        return str(row[0])

    def rows(self, table: str) -> int:
        queries = {
            "execution_runs": "SELECT COUNT(*) FROM execution_runs",
            "execution_bands": "SELECT COUNT(*) FROM execution_bands",
            "results": "SELECT COUNT(*) FROM results",
        }
        with self.store._read() as connection:
            return int(connection.execute(queries[table]).fetchone()[0])


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[MemoryHarness]:
    clock = FakeClock()
    store = Store(tmp_path / "memory.db", clock=clock)
    with store.scope_context(namespace=NS):
        for queue in (QUEUE, "conformance-dependency"):
            store.define_queue(
                QueueConfig(name=queue, lease_seconds=60, heartbeat_seconds=10, max_attempts=1)
            )
    app = create_app(
        store,
        ServiceSettings(db_path=store.path, reaper_seconds=3600, anonymous_access_enabled=True),
    )
    with TestClient(app) as client:
        yield MemoryHarness(store, clock, client)


def test_http_success_recall_and_immutable_report_replay(memory: MemoryHarness) -> None:
    actor = memory.actor()
    first = memory.claim(actor, "first")
    accepted = memory.submit(actor, first)
    assert accepted.status_code == 200
    changed = copy.deepcopy(REPORT)
    changed["model"] = {"name": "changed-untrusted-report"}
    replay = memory.submit(actor, first, report=changed)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["receipt_hash"] == accepted.json()["receipt_hash"]
    assert memory.rows("execution_runs") == memory.rows("results") == 1
    discovered = memory.request(actor, "GET", "/v1/tasks/first/executions")
    assert discovered.status_code == 200
    assert discovered.json()["executions"][0]["receipt_hash"] == accepted.json()["receipt_hash"]
    second = memory.claim(actor, "second")
    response = memory.recall(actor, second)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["coverage"]["complete"] is False
    assert data["reuse_requires_validation"] is True
    assert data["matches"][0]["match_kind"] == "exact_input"
    recalled = data["matches"][0]["execution"]
    assert recalled["report"] == REPORT
    assert recalled["output"] == {"synthetic": True, "sum": 3}
    assert recalled["receipt_hash"] == accepted.json()["receipt_hash"]
    assert recalled["evidence"]["key_id"] == actor.key_id
    assert recalled["trust"]["telemetry"] == "worker_reported"
    assert recalled["report"]["usage"]["input_tokens"] is None
    assert "source_governance" not in recalled
    assert memory.request(actor, "GET", "/v1/executions/" + recalled["run_id"]).json() == recalled


def test_stale_lease_neither_captures_nor_recalls(memory: MemoryHarness) -> None:
    actor = memory.actor()
    lease = memory.claim(actor, "stale")
    memory.clock.advance(61)
    assert memory.recall(actor, lease).status_code == 409
    assert memory.submit(actor, lease).status_code == 409
    assert memory.rows("execution_runs") == memory.rows("results") == 0


def test_run_requires_source_queue_and_source_identity(memory: MemoryHarness) -> None:
    owner = memory.actor()
    source = memory.claim(owner, "shared")
    assert memory.submit(owner, source).status_code == 200
    run_id = memory.run_id("shared")
    peer = memory.actor()
    assert memory.request(peer, "GET", "/v1/executions/" + run_id).status_code == 200
    assert memory.submit(peer, source).status_code == 409
    changed_output = memory.request(
        owner,
        "POST",
        "/v1/tasks/submit",
        {
            "task_id": source["task_id"],
            "fencing_token": source["fencing_token"],
            "output": {"synthetic": "different output"},
            "ok": True,
        },
        {"Idempotency-Key": source["task_id"]},
    )
    assert changed_output.status_code == 409
    assert memory.rows("execution_runs") == 1
    noun_only = memory.actor(queues=["conformance-unrelated"])
    assert memory.request(noun_only, "GET", "/v1/executions/" + run_id).status_code == 404
    assert memory.request(noun_only, "GET", "/v1/tasks/shared/executions").status_code == 404
    elsewhere = memory.actor(namespace="conformance-elsewhere")
    assert memory.request(elsewhere, "GET", "/v1/executions/" + run_id).status_code == 404
    query = memory.claim(owner, "query")
    assert memory.recall(peer, query).status_code == 403


@pytest.mark.parametrize(
    ("original", "current"), [("private", "public_commons"), ("public_commons", "private")]
)
def test_original_and_current_task_acl_intersect(
    memory: MemoryHarness, original: str, current: str
) -> None:
    owner, peer = memory.actor(), memory.actor()
    source = memory.claim(owner, "acl-source", visibility=original)
    assert memory.submit(owner, source).status_code == 200
    run_id = memory.run_id("acl-source")
    # An operator ACL update in this isolated fixture must neither retroactively
    # publish private inputs nor leave a stale public memory view.
    with memory.store._tx() as connection:
        connection.execute(
            "UPDATE tasks SET visibility_scope=? WHERE namespace=? AND id=?",
            (current, NS, "acl-source"),
        )
    assert memory.request(peer, "GET", "/v1/executions/" + run_id).status_code == 404
    assert memory.request(owner, "GET", "/v1/executions/" + run_id).status_code == 200
    query = memory.claim(peer, "acl-query")
    assert memory.recall(peer, query).json()["matches"] == []


@pytest.mark.parametrize(
    ("original", "current"), [("private", "public_commons"), ("surface_only", "private")]
)
def test_artifact_version_and_current_head_acl_intersect(
    memory: MemoryHarness, original: str, current: str
) -> None:
    owner, peer = memory.actor(), memory.actor()
    path = "/v1/stores/memory.artifacts/records/synthetic"
    first = memory.request(
        owner,
        "POST",
        path,
        {"expected_version": 0, "value": {"synthetic": 1}, "visibility_scope": original},
    )
    assert first.status_code == 200
    report = {
        **copy.deepcopy(REPORT),
        "artifact_refs": [{"store": "memory.artifacts", "key": "synthetic", "version": 1}],
    }
    source = memory.claim(owner, "artifact-source", visibility="public_commons")
    assert memory.submit(owner, source, report=report).status_code == 200
    run_id = memory.run_id("artifact-source")
    assert (
        memory.request(
            owner,
            "POST",
            path,
            {"expected_version": 1, "value": {"synthetic": 2}, "visibility_scope": current},
        ).status_code
        == 200
    )
    assert memory.request(peer, "GET", "/v1/executions/" + run_id).status_code == 404
    assert memory.request(owner, "GET", "/v1/executions/" + run_id).status_code == 200


def test_unavailable_or_cross_namespace_ref_rolls_back_submit(memory: MemoryHarness) -> None:
    owner = memory.actor()
    source = memory.claim(owner, "missing-ref")
    ref = {"store": "memory.artifacts", "key": "absent", "version": 1}
    for invalid in (ref, {**ref, "namespace": "conformance-elsewhere"}):
        report = {**copy.deepcopy(REPORT), "artifact_refs": [invalid]}
        assert memory.submit(owner, source, report=report).status_code == 400
        assert memory.rows("results") == memory.rows("execution_runs") == 0
        assert memory.request(owner, "GET", "/v1/tasks/missing-ref").json()["state"] == "leased"


def test_failed_result_is_a_warning_not_a_success(memory: MemoryHarness) -> None:
    actor = memory.actor()
    failed = memory.claim(actor, "failed")
    assert memory.submit(actor, failed, ok=False).status_code == 200
    query = memory.claim(actor, "retry-example")
    result = memory.recall(actor, query)
    assert result.status_code == 200, result.text
    match = result.json()["matches"][0]
    assert match["match_kind"] == "failure_warning"
    assert match["underlying_match_kind"] == "exact_input"
    assert match["execution"]["ok"] is False
    assert match["execution"]["error"] == "SYNTHETIC failed check"


def test_query_fingerprints_do_not_bypass_related_task_grants(memory: MemoryHarness) -> None:
    author = memory.actor(queues=[QUEUE, "conformance-dependency"])
    dependency = memory.claim(author, "dependency", queue="conformance-dependency")
    assert memory.submit(author, dependency).status_code == 200
    response = memory.request(
        author,
        "POST",
        "/v1/tasks",
        {
            "task_id": "dependent",
            "queue": QUEUE,
            "brief": {"synthetic": True},
            "visibility_scope": "surface_only",
            "depends_on": ["dependency"],
        },
    )
    assert response.status_code == 201
    narrow = memory.actor()
    lease = memory.request(
        narrow, "POST", "/v1/tasks/claim", {"queue": QUEUE, "worker": "synthetic"}
    ).json()
    denied = memory.recall(narrow, lease)
    assert denied.status_code in (403, 404)
    assert "query_fingerprints" not in denied.json()


def test_anonymous_memory_expires_and_all_indexes_are_purged(memory: MemoryHarness) -> None:
    minted = memory.client.post("/v1/session/anonymous", json={"agent_label": "synthetic-memory"})
    assert minted.status_code == 201
    session = minted.json()
    anon = Actor(session["session_token"], session["principal_id"], session["namespace"])
    lease = memory.request(
        anon, "POST", "/v1/tasks/claim", {"queue": "demo.ping", "worker": "synthetic"}
    ).json()
    assert memory.submit(anon, lease, report={}).status_code == 200
    run_id = memory.run_id(lease["task_id"], anon.namespace)
    run = memory.request(anon, "GET", "/v1/executions/" + run_id)
    assert run.status_code == 200
    assert run.json()["expires_at"] == session["retention_expires_at"]
    assert run.json()["labels"]["origin"] == "anonymous"
    second = memory.client.post(
        "/v1/session/anonymous", json={"agent_label": "synthetic-other"}
    ).json()
    other = Actor(second["session_token"], second["principal_id"], second["namespace"])
    assert memory.request(other, "GET", "/v1/executions/" + run_id).status_code == 404
    operator = memory.actor(admin=True, namespace=anon.namespace)
    memory.clock.advance(86401)
    # Expiry is enforced before a periodic purge has removed the stored row.
    assert memory.rows("execution_runs") == 1
    assert memory.request(operator, "GET", "/v1/executions/" + run_id).status_code == 404
    memory.store.purge_expired_principals()
    assert memory.rows("execution_runs") == memory.rows("execution_bands") == 0


def test_resume_input_changes_exact_fingerprint(memory: MemoryHarness) -> None:
    owner, answerer = memory.actor(), memory.actor()
    first = memory.claim(owner, "before-input")
    assert memory.submit(owner, first).status_code == 200
    run_id = memory.run_id("before-input")
    old = memory.request(owner, "GET", "/v1/executions/" + run_id).json()
    query = memory.claim(owner, "after-input")
    parked = memory.request(
        owner,
        "POST",
        "/v1/tasks/park",
        {
            "task_id": query["task_id"],
            "fencing_token": query["fencing_token"],
            "reason": "SYNTHETIC missing input",
        },
    )
    assert parked.status_code == 200
    resumed = memory.request(
        answerer,
        "POST",
        "/v1/tasks/resume",
        {
            "task_id": query["task_id"],
            "input": {"synthetic": True, "additional_number": 4},
        },
    )
    assert resumed.status_code == 200
    query = memory.request(
        owner, "POST", "/v1/tasks/claim", {"queue": QUEUE, "worker": "synthetic"}
    ).json()
    recalled = memory.recall(owner, query)
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["query_fingerprints"]["input_hash"] != old["fingerprints"]["input_hash"]
    assert all(row["match_kind"] != "exact_input" for row in recalled.json()["matches"])


@pytest.mark.parametrize("shape", ["deep", "large"])
def test_unsupported_snapshot_shape_preserves_legacy_submit(
    memory: MemoryHarness, shape: str
) -> None:
    owner = memory.actor()
    brief: dict[str, Any] = (
        {"synthetic": True, "text": "x" * 140000} if shape == "large" else {"synthetic": True}
    )
    if shape == "deep":
        for _ in range(20):
            brief = {"nested": brief}
    lease = memory.claim(owner, "unmatched", brief=brief)
    response = memory.submit(owner, lease)
    assert response.status_code == 200, response.text
    result = memory.request(owner, "GET", "/v1/executions/" + memory.run_id("unmatched"))
    assert result.status_code == 200
    assert result.json()["fingerprints"]["input_hash"] is None
    assert result.json()["fingerprints"]["exact_hash"] is None


def test_invalid_report_does_not_finish_or_capture(memory: MemoryHarness) -> None:
    owner = memory.actor()
    lease = memory.claim(owner, "invalid-report")
    report = {"version": 1, "usage": {"source": "worker_reported", "input_tokens": False}}
    assert memory.submit(owner, lease, report=report).status_code == 400
    assert memory.rows("execution_runs") == memory.rows("results") == 0
    assert memory.request(owner, "GET", "/v1/tasks/invalid-report").json()["state"] == "leased"


def test_repeated_simhash_chunks_do_not_break_atomic_submit(memory: MemoryHarness) -> None:
    owner = memory.actor()
    lease = memory.claim(owner, "repeated-bands")
    # This deterministic text produces the same 16-bit chunk at positions 0 and 7.
    response = memory.submit(owner, lease, report={"summaries": {"problem": "01062"}})
    assert response.status_code == 200, response.text
    assert memory.rows("execution_runs") == 1
    assert memory.rows("execution_bands") == 8
