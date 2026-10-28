"""HTTP client framing and real local MCP execution-report flow, without inference."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.client import Client
from tessera.receipts import canonical_json
from tessera.service import create_app


def test_client_preserves_report_object_namespace_and_idempotency() -> None:
    requests: list[httpx.Request] = []
    report = {"version": 1, "summaries": {"problem": "SYNTHETIC integer addition"}}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/tasks/submit":
            return httpx.Response(
                200,
                json={
                    "task_id": "fixture",
                    "fencing_token": 1,
                    "idempotency_key": "fixture:1",
                    "ok": True,
                    "receipt_hash": "0" * 64,
                    "state_after": "done",
                    "created_at": 1,
                },
            )
        return httpx.Response(200, json={"matches": [], "reuse_requires_validation": True})

    client = Client(
        "https://example.com", key="fixture-key", transport=httpx.MockTransport(respond)
    )
    try:
        client.submit(
            "fixture",
            1,
            {},
            idempotency_key="fixture:1",
            execution_report=report,
            namespace="conformance-report",
        )
        client.recall_execution(
            "fixture", 1, execution_report=report, limit=3, namespace="conformance-report"
        )
        client.get_execution("run_" + "1" * 64, namespace="conformance-report")
        client.list_executions("fixture", after=1, limit=2, namespace="conformance-report")
        client.submit("fixture", 1, {}, idempotency_key="fixture:1")
    finally:
        client.close()
    assert all(request.headers["x-namespace"] == "conformance-report" for request in requests[:4])
    assert requests[0].headers["idempotency-key"] == "fixture:1"
    assert json.loads(requests[0].content)["execution_report"] == report
    assert json.loads(requests[1].content) == {
        "task_id": "fixture",
        "fencing_token": 1,
        "execution_report": report,
        "limit": 3,
    }
    assert requests[2].method == "GET"
    assert requests[3].url.path == "/v1/tasks/fixture/executions"
    assert dict(requests[3].url.params) == {"after": "1", "limit": "2"}
    assert "execution_report" not in json.loads(requests[4].content)


def test_mcp_report_recall_and_readback_without_shim(tmp_path: Path) -> None:
    store = Store(tmp_path / "mcp-execution.db")
    queue = "conformance-execution"
    key, _ = store.issue_key("fixture-worker", queues=[queue], scopes=["store"])
    store.define_queue(QueueConfig(name=queue))
    report = {
        "version": 1,
        "harness": {"name": "synthetic-test-client"},
        "summaries": {"problem": "SYNTHETIC addition"},
    }
    try:
        with TestClient(
            create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
        ) as http:
            headers = {"Authorization": "Bearer " + key}

            def rpc(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                response = http.post(
                    "/mcp",
                    headers={**headers, "accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert not result.get("isError"), result
                return result["structuredContent"]

            response = http.post(
                "/v1/tasks",
                headers=headers,
                json={"queue": queue, "brief": {"synthetic": True}, "visibility_scope": "private"},
            )
            assert response.status_code == 201
            lease = rpc("claim", {"queue": queue, "worker": "fixture-worker"})["lease"]
            ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
            for invalid in (
                {"fencing_token": str(lease["fencing_token"])},
                {"limit": True},
                {"execution_report": "{}"},
            ):
                rejected = http.post(
                    "/mcp",
                    headers={**headers, "accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "recall_execution", "arguments": {**ref, **invalid}},
                    },
                )
                assert rejected.status_code in {200, 400}
                if rejected.status_code == 400:
                    assert rejected.json()["code"] == "invalid_request"
                else:
                    assert rejected.json()["result"]["isError"] is True, invalid
            recalled = rpc("recall_execution", {**ref, "execution_report": report})
            assert recalled["matches"] == [] and recalled["reuse_requires_validation"] is True
            receipt = rpc(
                "submit",
                {
                    **ref,
                    "output": {"value": 3},
                    "idempotency_key": "fixture:1",
                    "execution_report": report,
                },
            )["receipt"]
            run_id = (
                "run_"
                + hashlib.sha256(
                    canonical_json(
                        {"namespace": "main", **ref, "receipt_hash": receipt["receipt_hash"]}
                    ).encode()
                ).hexdigest()
            )
            observed = rpc("get_execution", {"run_id": run_id})
            listed = rpc("list_executions", {"task_id": lease["task_id"]})
            assert listed["executions"][0]["run_id"] == run_id
            assert listed["executions"][0]["receipt_hash"] == receipt["receipt_hash"]
            assert observed["report"] == report
            assert observed["output"] == {"value": 3}
            assert observed["trust"]["telemetry"] == "worker_reported"
            assert observed["trust"]["reuse_requires_validation"] is True
    finally:
        store.close()


def test_cloud_mcp_execution_argument_adapter() -> None:
    node = shutil.which("node")
    assert node is not None
    path = Path(__file__).with_name("edge_execution_mcp.mjs")
    result = subprocess.run(
        [node, str(path)], text=True, capture_output=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True
