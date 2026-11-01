"""Bounded authoring, pinned inputs and a real local task loop; no provider calls."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tessera import QueueConfig, RecordWrite, Store, TaskSpec


def module() -> Any:
    path = Path(__file__).resolve().parents[1] / "tools/research_seed.py"
    spec = importlib.util.spec_from_file_location("research_seed", path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def request_data() -> dict[str, Any]:
    return {
        "namespace": "conformance-research",
        "queue": "conformance-research",
        "output_store": "research.candidates",
        "stage": "discover_sources",
        "profile": {
            "store": "research.profiles",
            "key": "EXAMPLE-profile",
            "version": 1,
            "value_sha256": "0" * 64,
        },
    }


def test_dry_run_is_offline_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = module()
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_data()))

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("preview must not create a client or call a provider")

    monkeypatch.setattr(tool, "Client", forbidden)
    monkeypatch.delenv("TESSERA_KEY", raising=False)
    assert tool.main(["--request", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["task"]["visibility_scope"] == "private"
    assert result["task"]["brief"]["execution"]["server_fallback"] is False
    assert result["task"]["brief"]["execution"]["maximum_automatically_enqueued_children"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"max_sources": True},
        {"max_candidates": 5},
        {"max_fetches": 21},
        {"max_runtime_seconds": 901},
        {"stage": "activate_schedule"},
        {"visibility_scope": "public_commons"},
        {"provider_key": "unexpected"},
        {"stage": "build_collector"},
        {"profile": "encoded object"},
    ],
)
def test_unsafe_or_unbounded_request_fails(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        module().ResearchRequest.model_validate({**request_data(), **change})


def test_identity_normalizes_inputs_but_rejects_conflicting_pins() -> None:
    tool = module()
    data = request_data()
    first = {**data["profile"], "key": "EXAMPLE-source-a"}
    second = {**first, "key": "EXAMPLE-source-b"}
    a = tool.ResearchRequest.model_validate({**data, "inputs": [first, second, first]})
    b = tool.ResearchRequest.model_validate({**data, "inputs": [second, first]})
    assert tool.build_spec(a) == tool.build_spec(b)
    changed = tool.ResearchRequest.model_validate(
        {**data, "profile": {**data["profile"], "version": 2}}
    )
    assert (
        tool.build_spec(changed).task_id
        != tool.build_spec(tool.ResearchRequest.model_validate(data)).task_id
    )
    conflict = tool.ResearchRequest.model_validate(
        {**data, "inputs": [first, {**first, "version": 2}]}
    )
    with pytest.raises(ValueError, match="conflicting noun"):
        tool.build_spec(conflict)


def test_cli_rejects_duplicate_nonfinite_oversized_and_private_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = module()
    path = tmp_path / "request.json"
    for raw in [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        " " * 32769,
        json.dumps({**request_data(), "unknown": "sensitive-marker"}),
    ]:
        path.write_text(raw)
        with pytest.raises(SystemExit) as failure:
            tool.main(["--request", str(path)])
        assert failure.value.code == 2
        captured = capsys.readouterr()
        assert not captured.out and "sensitive-marker" not in captured.err


def test_output_cannot_activate_widen_destination_or_fake_success() -> None:
    tool = module()
    request = tool.ResearchRequest.model_validate(request_data())
    artifact = {**request_data()["profile"], "store": request.output_store}
    result = {
        "outcome": "candidates",
        "reason": "EXAMPLE source",
        "candidates": [
            {
                "category_id": "noun.feed_source",
                "artifact": artifact,
                "evidence": [request_data()["profile"]],
                "limitations": ["Example data only"],
            }
        ],
    }
    tool.check_output(request, result)
    for change in [
        {"activation": "active"},
        {"outcome": "no_change"},
        {"candidates": []},
        {"candidates": result["candidates"] * 4},
    ]:
        with pytest.raises(ValueError, match=r"ResearchOutput|candidate allowance"):
            tool.check_output(request, {**result, **change})
    for field, value in [
        ("category_id", "noun.code_node"),
        ("artifact", {**artifact, "store": "canonical"}),
    ]:
        invalid = copy.deepcopy(result)
        invalid["candidates"][0][field] = value
        with pytest.raises(ValueError, match="category or destination"):
            tool.check_output(request, invalid)


def test_real_local_seed_loop_and_input_drift(tmp_path: Path) -> None:
    tool = module()
    data = request_data()
    namespace, queue = data["namespace"], data["queue"]
    store = Store(tmp_path / "research.db")
    _, grant = store.issue_key(
        "example-worker", queues=[queue], scopes=["store", "namespace:" + namespace]
    )
    try:
        with store.scope_context(namespace=namespace, key_id=grant.key_id):
            store.define_queue(QueueConfig(name=queue))
            value = {
                "example_only": True,
                "topics": ["memory"],
                "hardware": None,
                "hardware_reason": "Unknown",
            }
            record = store.put_records(
                [
                    RecordWrite(
                        store=data["profile"]["store"],
                        key=data["profile"]["key"],
                        value=value,
                        expected_version=0,
                        visibility_scope="private",
                    )
                ]
            )[0]
            data["profile"].update(
                version=record.version, value_sha256=tool.value_digest(record.value)
            )

            class ScopedStore:
                def get_record(self, *args: Any, **kwargs: Any) -> Any:
                    return store.get_record(*args, **kwargs)

                def enqueue(self, spec: TaskSpec, **kwargs: Any) -> str:
                    assert kwargs.pop("idempotency_key") == spec.idempotency_key
                    return store.enqueue(spec, **kwargs)

            request = tool.ResearchRequest.model_validate(data)
            task = tool.enqueue_verified(ScopedStore(), request)
            assert tool.enqueue_verified(ScopedStore(), request) == task
            wrong = tool.ResearchRequest.model_validate(
                {**data, "profile": {**data["profile"], "value_sha256": "f" * 64}}
            )
            with pytest.raises(ValueError, match="pinned identity"):
                tool.enqueue_verified(ScopedStore(), wrong)
            lease = store.claim(queue, "example-worker", key_id=grant.key_id)
            assert lease and lease.task_id == task and lease.kind == "verb.probe_source"
            result = {
                "outcome": "no_change",
                "candidates": [],
                "reason": "Test fixture performs no research.",
            }
            tool.check_output(request, result)
            receipt = store.submit(
                lease.task_id, lease.fencing_token, result, idempotency_key="example-result"
            )
            assert receipt.receipt_hash
            assert store.task(task).state == "done"
            assert store.claim(queue, "example-worker", key_id=grant.key_id) is None
    finally:
        store.close()


def test_all_four_checked_in_examples_build_supported_tasks() -> None:
    tool = module()
    folder = Path(__file__).resolve().parents[1] / "examples/research"
    stages = set()
    for path in folder.glob("*.json"):
        request = tool.ResearchRequest.model_validate(tool.load_json(path))
        task = tool.build_spec(request)
        stages.add(request.stage)
        assert task.visibility_scope == "private" and task.effect_class == "write-internal"
    assert stages == set(tool.STAGES)
