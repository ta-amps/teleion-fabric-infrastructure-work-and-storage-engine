"""Explicit grants, team-private routing and owner-only negative controls."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store, TaskSpec
from tessera.access import Gate, ServiceSettings
from tessera.service import create_app


@pytest.mark.parametrize(
    "label", ["surface:shamash", "shamash", "shamash-worker", "shamash_worker", "shamash.worker"]
)
def test_labels_never_grant_surface_permissions(tmp_path: Path, label: str) -> None:
    store = Store(tmp_path / "labels.db")
    try:
        unbound, grant = store.issue_key(
            label, queues=["fixture"], scopes=["nouns:read", "cas:write"], account_id="same-account"
        )
        bound, _ = store.issue_key(
            "unrelated display name",
            queues=["fixture"],
            scopes=["nouns:read", "cas:write", "surface:shamash"],
            account_id="same-account",
        )
        assert grant.bound_surface() is None
        assert Gate.effective_surface(grant, "shamash") is None
        with TestClient(create_app(store, ServiceSettings(db_path=store.path))) as client:
            for key, expected in [(unbound, 403), (bound, 200)]:
                response = client.post(
                    "/v1/stores/shamash.fixture/records/synthetic",
                    headers={"Authorization": "Bearer " + key, "X-Surface-Id": "shamash"},
                    json={"value": {"synthetic": True}, "expected_version": 0},
                )
                assert response.status_code == expected
    finally:
        store.close()


@pytest.mark.parametrize(
    ("changed_grant", "expected"),
    [
        ({"queues": ["other-queue"]}, 403),
        ({"kinds": ["verb.verify_gate"]}, 204),
        ({"effect_classes": ["read"]}, 204),
        ({"scopes": ["namespace:other", "surface:fixture", "verbs:claim", "nouns:read"]}, 403),
    ],
)
def test_queue_kind_effect_and_namespace_are_independent_gates(
    tmp_path: Path, changed_grant: dict[str, Any], expected: int
) -> None:
    store = Store(tmp_path / "grants.db")
    namespace = "conformance-routing"
    queue = "conformance-work"
    try:
        store.define_queue(QueueConfig(name=queue), namespace=namespace)
        with store.scope_context(namespace=namespace, surface="fixture", key_id="requester"):
            task_id = store.enqueue(
                TaskSpec(
                    queue=queue,
                    kind="verb.solve_draft",
                    producer_surface="surface:fixture",
                    visibility_scope="surface_only",
                    brief={"synthetic": True},
                )
            )
        grant: dict[str, Any] = {
            "queues": [queue],
            "kinds": ["verb.solve_draft"],
            "effect_classes": ["write-internal"],
            "scopes": ["namespace:" + namespace, "surface:fixture", "verbs:claim", "nouns:read"],
        }
        token, _ = store.issue_key(
            "requester", account_id="requester", **{**grant, **changed_grant}
        )
        with TestClient(create_app(store, ServiceSettings(db_path=store.path))) as client:
            response = client.post(
                "/ns/" + namespace + "/v1/tasks/claim",
                headers={"Authorization": "Bearer " + token},
                json={"queue": queue, "worker": "requester", "kinds": ["verb.solve_draft"]},
            )
            assert response.status_code == expected
        with store.scope_context(namespace=namespace):
            assert store.task(task_id).state == "ready"
    finally:
        store.close()


def test_executable_team_private_flow() -> None:
    source = Path(__file__).resolve().parents[1] / "tools" / "team_private_work.py"
    spec = importlib.util.spec_from_file_location("team_private_work", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report = module.run_demo()
    assert report["distinct_keys"] is True
    assert report["result"] == 13
    assert len(report["receipt_hashes"]) == 3
    assert all(report["controls"].values())
    assert report["model_calls"] == 0 and report["server_fallback_enabled"] is False
    assert len(report["fingerprint"]) == 64
