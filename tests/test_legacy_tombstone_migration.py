"""Existing diagnostic heads are restricted on upgrade, not only newly failed tasks."""

from pathlib import Path

import pytest

from tessera import QueueConfig, Store, TaskSpec
from tessera.errors import NotFound
from tessera.models import RecordWrite


def test_upgrade_restricts_legacy_task_heads_without_rewriting_payloads(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    store = Store(path)
    _, owner = store.issue_key("synthetic-owner", queues=["conformance-legacy"], scopes=["store"])
    _, peer = store.issue_key("synthetic-peer", queues=["conformance-legacy"], scopes=["store"])
    store.define_queue(QueueConfig(name="conformance-legacy", max_attempts=1))
    with store.scope_context(key_id=owner.key_id):
        task = store.enqueue(
            TaskSpec(
                queue="conformance-legacy", brief={"synthetic": True}, visibility_scope="private"
            )
        )
        lease = store.claim("conformance-legacy", "synthetic", key_id=owner.key_id)
        assert lease
        store.submit(
            task,
            lease.fencing_token,
            {},
            ok=False,
            error="SYNTHETIC private diagnostic",
            idempotency_key="fail",
        )
        before = store.get_record("negative.knowledge", task).value
        store.put_records(
            [
                RecordWrite(
                    store="negative.knowledge",
                    key="manual",
                    expected_version=0,
                    value={"synthetic": True},
                    visibility_scope="public_commons",
                )
            ]
        )
    store._conn().execute(
        "UPDATE record_heads SET visibility_scope='public_commons' WHERE store='negative.knowledge' AND key=?",
        (task,),
    )
    store._conn().execute("DELETE FROM meta WHERE key='task_tombstone_privacy_v1'")
    store.close()
    for _ in range(2):
        store = Store(path)
        with store.scope_context(key_id=peer.key_id):
            with pytest.raises(NotFound):
                store.get_record("negative.knowledge", task)
            assert store.get_record("negative.knowledge", "manual").value == {"synthetic": True}
        with store.scope_context(key_id=owner.key_id):
            assert store.get_record("negative.knowledge", task).value == before
        store.close()
