"""Large historical results are loaded only for selected hits and bounded in recall."""

from pathlib import Path

from tessera import QueueConfig, Store, TaskSpec


def test_recall_defers_large_output_loading_and_has_a_combined_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "bounded.db")
    store.define_queue(QueueConfig(name="conformance-output"))
    _, grant = store.issue_key("example", queues=["conformance-output"], scopes=["store"])
    report = {"summaries": {"problem": "EXAMPLE identical task"}}
    with store.scope_context(key_id=grant.key_id):
        for i in range(4):
            task = store.enqueue(TaskSpec(queue="conformance-output", brief={"example_only": True}))
            lease = store.claim("conformance-output", "example", key_id=grant.key_id)
            assert lease
            store.submit(
                task,
                lease.fencing_token,
                {"example_only": "x" * 700000},
                idempotency_key=str(i),
                execution_report=report,
            )
        task = store.enqueue(TaskSpec(queue="conformance-output", brief={"example_only": True}))
        lease = store.claim("conformance-output", "example", key_id=grant.key_id)
        assert lease
        statements = []
        store._conn().set_trace_callback(statements.append)
        found = store.recall_execution(
            task, lease.fencing_token, grant=grant, execution_report=report, limit=2
        )
        store._conn().set_trace_callback(None)
        assert len(found["matches"]) == 2
        assert sum(m["execution"]["output"] is not None for m in found["matches"]) == 1
        omitted = next(m["execution"] for m in found["matches"] if m["execution"]["output"] is None)
        assert omitted["output_reason"] == "recall output byte budget reached; use get_execution"
        assert (
            len(store.get_execution(omitted["run_id"], grant=grant)["output"]["example_only"])
            == 700000
        )
        assert sum("SELECT * FROM results WHERE" in sql for sql in statements) == 2
    store.close()
