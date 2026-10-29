"""Bounded task authoring and private local claim fixtures, without model execution."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tessera import QueueConfig, Store, TaskSpec
from tessera._execution_capture import digest


def module() -> Any:
    path = Path(__file__).resolve().parents[1] / "tools" / "execution_distillation.py"
    spec = importlib.util.spec_from_file_location("execution_distillation", path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def request_data() -> dict[str, Any]:
    return {
        "namespace": "conformance-distill",
        "queue": "conformance-distill",
        "output_store": "distillation.candidates",
        "source_runs": [
            {"run_id": "run_" + "1" * 64, "report_sha256": "2" * 64, "receipt_hash": "3" * 64}
        ],
    }


def test_dry_run_needs_no_credentials_network_or_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = module()
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_data()))

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("dry-run must not construct an HTTP client")

    monkeypatch.setattr(tool, "Client", forbidden)
    monkeypatch.delenv("TESSERA_KEY", raising=False)
    assert tool.main(["--request", str(request)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["task"]["visibility_scope"] == "private"
    assert result["task"]["kind"] == "verb.solve_draft"
    assert result["task"]["brief"]["execution"]["server_fallback"] is False


def test_invalid_request_does_not_echo_unexpected_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = module()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({**request_data(), "unexpected": "sensitive-marker"}))
    with pytest.raises(SystemExit) as failure:
        tool.main(["--request", str(request)])
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "sensitive-marker" not in captured.err
    assert "bounded distillation schema" in captured.err


@pytest.mark.parametrize(
    "change",
    [
        {"max_candidates": True},
        {"max_candidates": 5},
        {"max_runtime_seconds": 901},
        {"source_runs": []},
        {"source_runs": "not an array"},
        {"visibility_scope": "public_commons"},
        {"provider_key": "unexpected"},
    ],
)
def test_request_rejects_unsafe_shapes_and_unbounded_work(change: dict[str, Any]) -> None:
    tool = module()
    with pytest.raises(ValidationError):
        tool.DistillationRequest.model_validate({**request_data(), **change})


def test_source_order_deduplicates_but_conflicting_pins_do_not_merge() -> None:
    tool = module()
    first = request_data()["source_runs"][0]
    second = {**first, "run_id": "run_" + "4" * 64}
    a = tool.DistillationRequest.model_validate(
        {**request_data(), "source_runs": [first, second, first]}
    )
    b = tool.DistillationRequest.model_validate({**request_data(), "source_runs": [second, first]})
    assert tool.build_spec(a) == tool.build_spec(b)
    conflict = tool.DistillationRequest.model_validate(
        {**request_data(), "source_runs": [first, {**first, "report_sha256": "5" * 64}]}
    )
    with pytest.raises(ValueError, match="conflicting immutable"):
        tool.build_spec(conflict)


def test_drifted_source_prevents_enqueue() -> None:
    tool = module()

    class ChangedSource:
        def get_execution(self, run_id: str, **_kwargs: Any) -> dict[str, Any]:
            return {"run_id": run_id, "report_sha256": "0" * 64}

        def enqueue(self, *_args: Any, **_kwargs: Any) -> str:
            pytest.fail("changed source must fail before enqueue")

    with pytest.raises(ValueError, match="pinned identity"):
        tool.enqueue_verified(
            ChangedSource(), tool.DistillationRequest.model_validate(request_data())
        )


def test_recorded_run_can_seed_one_private_distillation_verb(tmp_path: Path) -> None:
    """Real local Store capture/read/enqueue/claim; the worker does not generate code here."""
    tool = module()
    request = request_data()
    namespace, queue = request["namespace"], request["queue"]
    store = Store(tmp_path / "distillation.db")
    _, grant = store.issue_key(
        "fixture-worker", queues=[queue], scopes=["store", "namespace:" + namespace]
    )
    report = {"version": 1, "summaries": {"problem": "SYNTHETIC typed integer addition"}}
    try:
        with store.scope_context(namespace=namespace, key_id=grant.key_id):
            store.define_queue(QueueConfig(name=queue))
            store.enqueue(
                TaskSpec(queue=queue, brief={"synthetic": True}, visibility_scope="private")
            )
            lease = store.claim(queue, "fixture-worker", key_id=grant.key_id)
            assert lease is not None
            receipt = store.submit(
                lease.task_id,
                lease.fencing_token,
                {"value": 3},
                idempotency_key="synthetic-completion",
                execution_report=report,
            )
            run_id = "run_" + digest(
                {
                    "namespace": namespace,
                    "task_id": lease.task_id,
                    "fencing_token": lease.fencing_token,
                    "receipt_hash": receipt.receipt_hash,
                }
            )
            source = store.get_execution(run_id, grant=grant)

            class ScopedStore:
                def get_execution(self, run_id: str, **_kwargs: Any) -> dict[str, Any]:
                    return store.get_execution(run_id, grant=grant)

                def enqueue(self, spec: TaskSpec, **kwargs: Any) -> str:
                    assert kwargs.pop("idempotency_key") == spec.idempotency_key
                    return store.enqueue(spec, **kwargs)

            request["source_runs"] = [
                {name: source[name] for name in ("run_id", "report_sha256", "receipt_hash")}
            ]
            parsed = tool.DistillationRequest.model_validate(request)
            first = tool.enqueue_verified(ScopedStore(), parsed)
            assert tool.enqueue_verified(ScopedStore(), parsed) == first
            work = store.claim(queue, "fixture-worker", key_id=grant.key_id)
            assert work is not None and work.task_id == first
            assert work.kind == "verb.solve_draft"
            assert work.brief["source_runs"] == request["source_runs"]
            assert work.brief["output_schema"]["$defs"]["Candidate"]["properties"]["category_id"][
                "enum"
            ] == ["noun.code_node", "noun.reflex_procedure"]
    finally:
        store.close()
