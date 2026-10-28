#!/usr/bin/env python3
"""Local executable work-routing example, using HTTP handlers and temporary state.

Run: uv run python tools/team_private_work.py
No remote services, provider keys, model calls, shell execution or public writes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from tessera import ChildSpec, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.ontology import NOUN_CATEGORIES, VERB_CATEGORIES
from tessera.receipts import canonical_json
from tessera.service import create_app

NAMESPACE = "conformance-routing"
SURFACE = "surface:synthetic_team"
QUEUE = "conformance-work"
SERVER_QUEUE = "conformance-server"
KIND = "verb.solve_draft"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    synthetic: Literal[True]
    values: list[StrictInt] = Field(min_length=1, max_length=4)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: StrictInt


CONTRACT = {
    "schema_version": 1,
    "category_id": "noun.skill_pack",
    "operation": "sum_squares",
    "input_schema": Input.model_json_schema(),
    "output_schema": Output.model_json_schema(),
    "execution": {"worker": "external", "server_fallback": False},
    "shim": {"mode": "none", "references": []},
}


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def run_demo() -> dict[str, Any]:
    """Exercise the ordinary API using separate requester, worker and outsider keys."""
    assert KIND in VERB_CATEGORIES
    assert CONTRACT["category_id"] in NOUN_CATEGORIES
    with tempfile.TemporaryDirectory(prefix="tessera-routing-") as folder:
        store = Store(Path(folder) / "fabric.db")
        for queue in (QUEUE, SERVER_QUEUE):
            store.define_queue(QueueConfig(name=queue), namespace=NAMESPACE)
        common = ["namespace:" + NAMESPACE, "nouns:read"]
        requester, request_grant = store.issue_key(
            "synthetic-member",
            queues=[QUEUE],
            kinds=[KIND],
            effect_classes=["write-internal"],
            scopes=[*common, SURFACE, "cas:write", "verbs:submit"],
            account_id="synthetic-team",
        )
        worker, work_grant = store.issue_key(
            "synthetic-member",
            queues=[QUEUE],
            kinds=[KIND],
            effect_classes=["write-internal"],
            scopes=[*common, SURFACE, "cas:write", "verbs:claim", "verbs:submit"],
            account_id="synthetic-team",
        )
        outsider, _ = store.issue_key(
            SURFACE,
            queues=[QUEUE],
            kinds=[KIND],
            effect_classes=["write-internal"],
            scopes=[*common, "verbs:claim", "cas:write"],
            account_id="synthetic-team",
        )
        controls: dict[str, bool] = {}
        receipts: list[str] = []
        app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
        try:
            with TestClient(app) as client:
                prefix = "/ns/" + NAMESPACE

                def call(path: str, token: str, body: Any = None, *, expected: int = 200) -> Any:
                    headers = {"Authorization": "Bearer " + token}
                    if path == "/v1/tasks/submit" and body is not None:
                        headers["Idempotency-Key"] = body["task_id"] + ":result"
                    response = (
                        client.get(prefix + path, headers=headers)
                        if body is None
                        else client.post(prefix + path, headers=headers, json=body)
                    )
                    assert response.status_code == expected, (path, response.status_code)
                    return response.json() if response.content else None

                def put(
                    name: str,
                    key: str,
                    value: Any,
                    token: str,
                    version: int = 0,
                    visibility: str = "surface_only",
                ) -> dict[str, Any]:
                    record = call(
                        f"/v1/stores/{name}/records/{key}",
                        token,
                        {
                            "expected_version": version,
                            "value": value,
                            "visibility_scope": visibility,
                            "tags": [
                                "synthetic:true",
                                "category:" + value.get("category_id", "noun.seed_hypothesis"),
                            ],
                        },
                    )
                    return {
                        "store": name,
                        "key": key,
                        "version": record["version"],
                        "sha256": digest(value),
                    }

                def read(ref: dict[str, Any], token: str) -> Any:
                    record = call(
                        f"/v1/stores/{ref['store']}/records/{ref['key']}?version={ref['version']}",
                        token,
                    )
                    assert digest(record["value"]) == ref["sha256"]
                    return record["value"]

                def claim(token: str = worker, *, expected: int = 200) -> Any:
                    return call(
                        "/v1/tasks/claim",
                        token,
                        {"queue": QUEUE, "worker": "synthetic-member", "kinds": [KIND]},
                        expected=expected,
                    )

                def fence(lease: dict[str, Any]) -> dict[str, Any]:
                    return {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}

                def submit(lease: dict[str, Any], output: Any) -> None:
                    receipt = call(
                        "/v1/tasks/submit", worker, {**fence(lease), "ok": True, "output": output}
                    )
                    assert receipt["state_after"] == "done"
                    receipts.append(receipt["receipt_hash"])

                contract_ref = put("work.contracts", "sum-squares", CONTRACT, requester)
                input_ref = put("work.inputs", "numbers", {"synthetic": True}, requester)
                brief = {
                    "contract": "tessera.work.example.v1",
                    "category_id": KIND,
                    "contract_ref": contract_ref,
                    "input_ref": input_ref,
                }
                spec = TaskSpec(
                    namespace=NAMESPACE,
                    producer_surface=SURFACE,
                    visibility_scope="surface_only",
                    queue=QUEUE,
                    kind=KIND,
                    brief=brief,
                    idempotency_key="sum-squares",
                    tags=["synthetic:true", "category:" + KIND],
                )
                task_id = call("/v1/tasks", requester, spec.model_dump(mode="json"), expected=201)[
                    "task_id"
                ]
                call("/v1/stores/work.contracts/records/sum-squares", outsider, expected=404)
                claim(outsider, expected=204)
                controls["label_and_account_do_not_grant_surface"] = True
                call(
                    "/v1/tasks/claim",
                    worker,
                    {"queue": SERVER_QUEUE, "worker": "synthetic"},
                    expected=403,
                )
                controls["external_key_cannot_claim_server_queue"] = True
                assert (
                    client.get(
                        "/ns/other/v1/stores", headers={"Authorization": "Bearer " + worker}
                    ).status_code
                    == 403
                )
                controls["namespace_grant_required"] = True

                lease = claim()
                assert lease["task_id"] == task_id
                assert read(lease["brief"]["contract_ref"], worker) == CONTRACT
                call("/v1/tasks/heartbeat", worker, fence(lease))
                assert "values" not in read(lease["brief"]["input_ref"], worker)
                parked = call(
                    "/v1/tasks/park",
                    worker,
                    {
                        **fence(lease),
                        "reason": "SYNTHETIC: provide the numbers as a versioned input noun",
                    },
                )
                input_ref = put(
                    "work.inputs",
                    "numbers",
                    {"synthetic": True, "values": [2, 3]},
                    requester,
                    version=1,
                )
                call(
                    "/v1/tasks/resume",
                    worker,
                    {
                        "task_id": task_id,
                        "park": parked["parks"],
                        "input": {"input_ref": input_ref},
                    },
                    expected=403,
                )
                call(
                    "/v1/tasks/resume",
                    requester,
                    {
                        "task_id": task_id,
                        "park": parked["parks"],
                        "input": {"input_ref": input_ref},
                    },
                )
                controls["distinct_principal_answers_park"] = True
                lease = claim()
                input_ref = lease["inputs"][-1]["input_ref"]
                values = Input.model_validate(read(input_ref, worker)).values
                children = [
                    ChildSpec(brief={**brief, "input_ref": input_ref, "index": index})
                    for index in range(len(values))
                ]
                child_ids = call(
                    "/v1/tasks/split",
                    worker,
                    {
                        **fence(lease),
                        "children": [child.model_dump(mode="json") for child in children],
                    },
                )["children"]
                for _ in child_ids:
                    child = claim()
                    assert child["task_id"] in child_ids
                    view = call("/v1/tasks/" + child["task_id"], worker)
                    assert view["visibility_scope"] == "surface_only"
                    assert view["producer_surface"] == SURFACE
                    data = Input.model_validate(read(child["brief"]["input_ref"], worker))
                    result = Output(result=data.values[child["brief"]["index"]] ** 2)
                    artifact = put(
                        "work.artifacts",
                        child["task_id"],
                        {"category_id": "noun.eval_run", **result.model_dump()},
                        worker,
                    )
                    submit(child, {"artifact_ref": artifact})
                controls["split_inherits_team_governance"] = True
                parent = claim()
                assert parent["task_id"] == task_id
                assert all(
                    row["state"] == "done" and row["ok"] and row["receipt_hash"]
                    for row in parent["children"]
                )
                result = Output(
                    result=sum(
                        read(row["output"]["artifact_ref"], worker)["result"]
                        for row in parent["children"]
                    )
                )
                expected = sum(value * value for value in values)
                assert result.result == expected
                artifact = put(
                    "work.artifacts",
                    task_id,
                    {"category_id": "noun.eval_run", **result.model_dump()},
                    worker,
                )
                evidence = put(
                    "work.evidence",
                    task_id,
                    {
                        "category_id": "noun.eval_run",
                        "synthetic": True,
                        "method": "deterministic_sum_squares",
                        "passed": result.result == expected,
                        "contract_ref": contract_ref,
                        "input_ref": input_ref,
                        "artifact_ref": artifact,
                    },
                    worker,
                )
                submit(parent, {"artifact_ref": artifact, "evidence_ref": evidence})
                assert read(artifact, requester)["result"] == expected

                private_spec = spec.model_copy(
                    update={
                        "task_id": None,
                        "idempotency_key": "owner-only",
                        "visibility_scope": "private",
                    }
                )
                private_id = call(
                    "/v1/tasks", requester, private_spec.model_dump(mode="json"), expected=201
                )["task_id"]
                claim(expected=204)
                call("/v1/tasks/" + private_id, worker, expected=404)
                put(
                    "work.inputs",
                    "owner-only",
                    {"synthetic": True},
                    requester,
                    visibility="private",
                )
                call("/v1/stores/work.inputs/records/owner-only", worker, expected=404)
                controls["private_remains_owner_key_only"] = True
                call(
                    "/v1/tasks/cancel",
                    requester,
                    {"task_id": private_id, "reason": "SYNTHETIC control complete"},
                )
                report = {
                    "scope": "local_http_handlers_synthetic_example",
                    "namespace": NAMESPACE,
                    "requester_key_id": request_grant.key_id,
                    "worker_key_id": work_grant.key_id,
                    "distinct_keys": request_grant.key_id != work_grant.key_id,
                    "result": result.result,
                    "controls": controls,
                    "receipt_hashes": receipts,
                    "model_calls": 0,
                    "server_fallback_enabled": False,
                    "contract_validation": "explicit_example_worker_not_global_CAS_policy",
                }
                report["fingerprint"] = digest(report)
                return report
        finally:
            store.close()


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
