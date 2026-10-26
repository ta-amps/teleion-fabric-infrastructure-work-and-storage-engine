"""Service-only requirements (SPEC §7): keys, POST-only writes, health over HTTP, the MCP door and
secret hygiene. They run only against a service you own:

  TESSERA_URL=http://localhost:8080
  TESSERA_TEST_KEYS='{"admin": "...", "q_a": "...", "q_b": "..."}'

`admin` has the admin scope; `q_a` may use only queue conformance-a; `q_b` only conformance-b.
Optionally TESSERA_LOG_PATH points at the service's log file for the T16 scan.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.fabric_service

URL = os.environ.get("TESSERA_URL", "")
KEYS: dict[str, str] = json.loads(os.environ.get("TESSERA_TEST_KEYS", "{}"))
needs_service = pytest.mark.skipif(
    not URL or not KEYS, reason="set TESSERA_URL and TESSERA_TEST_KEYS"
)
WRITE_PATHS = [
    "/v1/tasks",
    "/v1/tasks/claim",
    "/v1/tasks/heartbeat",
    "/v1/tasks/submit",
    "/v1/tasks/split",
    "/v1/tasks/cancel",
    "/v1/queues/define",
    "/v1/admin/reap",
    "/v1/jobs",
    "/v1/stores/batch",
    "/v1/blobs",
]


def auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


@pytest.fixture
def http() -> Any:
    with httpx.Client(base_url=URL, timeout=30) as client:
        for name in ("conformance-a", "conformance-b"):
            client.post("/v1/queues/define", json={"name": name}, headers=auth(KEYS["admin"]))
        yield client


@needs_service
def test_T01_keys_are_required(http: httpx.Client) -> None:
    body = {"queue": "conformance-a", "worker": "w-anon"}
    assert http.post("/v1/tasks/claim", json=body).status_code == 401
    other = {"queue": "conformance-b", "worker": "w-a"}
    assert http.post("/v1/tasks/claim", json=other, headers=auth(KEYS["q_a"])).status_code == 403


@needs_service
def test_T02_writes_are_post_only(http: httpx.Client) -> None:
    before = http.get("/v1/counts", headers=auth(KEYS["admin"])).json()["queues"]
    for path in WRITE_PATHS:
        assert http.get(path, headers=auth(KEYS["admin"])).status_code == 405, path
    after = http.get("/v1/counts", headers=auth(KEYS["admin"])).json()["queues"]
    assert before == after


@needs_service
def test_T10_health_over_http(http: httpx.Client) -> None:
    resp = http.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_ok"] is True and "reaper_last_run" in body and isinstance(body["queues"], list)


@needs_service
def test_T11_an_mcp_client_can_claim_and_submit(http: httpx.Client) -> None:
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    task = {"queue": "conformance-a", "brief": {"conformance": "T11"}}
    assert http.post("/v1/tasks", json=task, headers=auth(KEYS["admin"])).status_code == 201

    async def session_run() -> None:
        async with (
            streamablehttp_client(f"{URL}/mcp", headers=auth(KEYS["q_a"])) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"claim", "heartbeat", "submit", "split"} <= names
            claimed = await session.call_tool("claim", {"queue": "conformance-a", "worker": "mcp"})
            lease = json.loads(claimed.content[0].text)  # type: ignore[union-attr]
            submitted = await session.call_tool(
                "submit",
                {
                    "task_id": lease["task_id"],
                    "fencing_token": lease["fencing_token"],
                    "output": {"via": "mcp"},
                    "idempotency_key": uuid.uuid4().hex,
                },
            )
            assert not submitted.isError

    asyncio.run(session_run())


@needs_service
def test_T16_no_secret_leaks(http: httpx.Client) -> None:
    probes = [
        http.get("/v1/health"),
        http.get("/v1/counts", headers=auth(KEYS["admin"])),
        http.get("/v1/events", headers=auth(KEYS["admin"])),
        http.get("/board"),
        http.post("/v1/tasks/claim", json={"queue": "conformance-a", "worker": "w"}),
        http.post(
            "/v1/tasks/claim",
            json={"queue": "conformance-b", "worker": "w"},
            headers=auth(KEYS["q_a"]),
        ),
        http.get("/v1/tasks/claim", headers=auth(KEYS["q_a"])),
        http.get("/v1/tasks/does-not-exist", headers=auth(KEYS["admin"])),
    ]
    texts = [r.text + json.dumps(dict(r.headers)) for r in probes]
    log_path = os.environ.get("TESSERA_LOG_PATH")
    if log_path:
        texts.append(Path(log_path).read_text(encoding="utf-8", errors="replace"))
    for key in KEYS.values():
        assert all(key not in text for text in texts)
