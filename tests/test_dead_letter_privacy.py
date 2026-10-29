"""Failure diagnostics remain private even when the source verb was shared."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.errors import NotFound
from tessera.models import VisibilityScope
from tessera.service import create_app


@pytest.mark.parametrize(
    ("visibility", "quarantined"),
    [
        ("private", False),
        ("public_commons", False),
        ("surface_only", False),
        ("surface_only", True),
        ("allowlist_surfaces", True),
    ],
)
def test_http_dead_letters_are_owner_private(
    tmp_path: Path, visibility: VisibilityScope, quarantined: bool
) -> None:
    store = Store(tmp_path / "failures.db")
    ns, queue, task_id = "conformance-failures", "conformance-private", "synthetic-failure"
    with store.scope_context(namespace=ns):
        store.define_queue(QueueConfig(name=queue, max_attempts=1))
        # Previously emitted public versions must also become unreadable to others.
        store.put_record(
            "negative.knowledge",
            task_id,
            {"synthetic": True},
            expected_version=0,
            producer_surface="surface:memory_team",
        )
    scopes = ["nouns:read", "verbs:claim", "verbs:submit", "surface:memory_team", f"namespace:{ns}"]
    owner, owner_grant = store.issue_key("synthetic-owner", queues=[queue], scopes=scopes)
    other, _ = store.issue_key("synthetic-peer", queues=[queue], scopes=scopes)
    noun_only, _ = store.issue_key(
        "synthetic-noun-reader",
        queues=["conformance-unrelated"],
        scopes=["nouns:read", "surface:memory_team", f"namespace:{ns}"],
    )
    cross_surface, _ = store.issue_key(
        "synthetic-allowlisted",
        queues=[queue],
        scopes=["nouns:read", "surface:other_team", f"namespace:{ns}"],
    )
    tags = ["origin:synthetic", "status:verified"] + (["status:quarantined"] if quarantined else [])
    app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
    with TestClient(app) as client:
        prefix = f"/ns/{ns}"
        auth = {"Authorization": "Bearer " + owner}
        response = client.post(
            prefix + "/v1/tasks",
            headers=auth,
            json={
                "queue": queue,
                "task_id": task_id,
                "brief": {"title": "SYNTHETIC confidential failure"},
                "visibility_scope": visibility,
                "producer_surface": "surface:memory_team",
                "allowed_surfaces": ["surface:other_team"],
                "target_cohorts": ["cohort:memory"],
                "tags": tags,
            },
        )
        assert response.status_code == 201, response.text
        lease = client.post(
            prefix + "/v1/tasks/claim", headers=auth, json={"queue": queue, "worker": "synthetic"}
        ).json()
        response = client.post(
            prefix + "/v1/tasks/submit",
            headers={**auth, "Idempotency-Key": "synthetic"},
            json={
                "task_id": task_id,
                "fencing_token": lease["fencing_token"],
                "ok": False,
                "error": "SYNTHETIC private diagnostic",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state_after"] == "dead"
        path = prefix + "/v1/stores/negative.knowledge/records/" + task_id
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        noun = response.json()
        assert noun["namespace"] == ns
        assert noun["visibility_scope"] == "private"
        assert noun["owner_key_id"] == owner_grant.key_id
        assert noun["producer_surface"] == "surface:memory_team"
        assert noun["allowed_surfaces"] == []
        assert noun["target_cohorts"] == ["cohort:memory"]
        assert "origin:synthetic" in noun["tags"]
        assert "status:verified" not in noun["tags"]
        assert ("status:quarantined" in noun["tags"]) == quarantined
        assert noun["value"]["reason"] == "SYNTHETIC private diagnostic"
        assert len(client.get(path + "/history", headers=auth).json()["versions"]) == 2
        for token in (other, noun_only, cross_surface):
            denied_auth = {"Authorization": "Bearer " + token}
            for suffix in ("", "?version=1", "?version=2", "/history"):
                assert client.get(path + suffix, headers=denied_auth).status_code == 404
            listed = client.get(
                prefix + "/v1/stores/negative.knowledge/records?cohort=cohort:memory",
                headers=denied_auth,
            )
            assert listed.status_code == 200 and listed.json()["records"] == []
            searched = client.get(
                prefix + "/v1/search?store=negative.knowledge&cohort=cohort:memory",
                headers=denied_auth,
            )
            assert searched.status_code == 200 and searched.json()["scents"] == []
        assert (
            client.get("/v1/stores/negative.knowledge/records/" + task_id, headers=auth).status_code
            == 403
        )


def test_unowned_embedded_failure_is_admin_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "embedded.db")
    try:
        store.define_queue(QueueConfig(name="conformance-embedded", max_attempts=1))
        task_id = store.enqueue(TaskSpec(queue="conformance-embedded", brief={"synthetic": True}))
        lease = store.claim("conformance-embedded", "synthetic")
        assert lease is not None
        store.submit(task_id, lease.fencing_token, None, idempotency_key="synthetic", ok=False)
        with pytest.raises(NotFound):
            store.get_record("negative.knowledge", task_id)
        with (
            store.scope_context(key_id="synthetic-reader", surface="tessera"),
            pytest.raises(NotFound),
        ):
            store.get_record("negative.knowledge", task_id)
        with store.scope_context(is_admin=True):
            record = store.get_record("negative.knowledge", task_id)
            assert record.visibility_scope == "private"
            assert record.owner_key_id == "system:operator-only"
    finally:
        store.close()


def test_worker_tombstone_privacy_derivation() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, str(Path(__file__).with_name("edge_dead_letter_privacy.mjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["controls_passed"] is True
