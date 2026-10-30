"""The access cases in tests/fixtures/access_parity.json, run against the Python service.

tests/workerd_access_parity.mjs runs the same file against the Worker, so each case holds for both
implementations or fails in the one that differs. Synthetic data only."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app

PARITY = json.loads((Path(__file__).parent / "fixtures" / "access_parity.json").read_text())
TEMPLATE = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)\}")


def lookup(value: Any, path: str) -> Any:
    """`a.b.0.c`; `*` maps over a list and `#` is a list's length."""
    parts = path.split(".")
    for index, part in enumerate(parts):
        if part == "*":
            rest = ".".join(parts[index + 1 :])
            return [lookup(item, rest) if rest else item for item in value]
        if part == "#":
            return len(value)
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def fill(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value)
        if whole:
            return lookup(saved, whole[1])
        return TEMPLATE.sub(lambda m: str(lookup(saved, m[1])), value)
    if isinstance(value, list):
        return [fill(item, saved) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, saved) for key, item in value.items()}
    return value


def check(where: str, body: Any, expect: dict[str, Any]) -> None:
    for path, wanted in expect.get("equals", {}).items():
        assert lookup(body, path) == wanted, (where, path, lookup(body, path))
    for path, wanted in expect.get("contains", {}).items():
        for item in wanted if isinstance(wanted, list) else [wanted]:
            assert item in lookup(body, path), (where, path, item)
    for path, unwanted in expect.get("lacks", {}).items():
        for item in unwanted if isinstance(unwanted, list) else [unwanted]:
            assert item not in lookup(body, path), (where, path, item)
    for path in expect.get("null", []):
        assert lookup(body, path) is None, (where, path)


@pytest.fixture
def fabric(tmp_path: Path) -> Iterator[tuple[TestClient, Store, dict[str, Any]]]:
    store = Store(str(tmp_path / "parity.db"), clock=FakeClock())
    secret, admin = store.issue_key("operator", queues=["*"], scopes=["admin", "store"])
    for queue in PARITY["queues"]:
        store.define_queue(QueueConfig(**queue))
    settings = ServiceSettings(
        anonymous_access_enabled=True,
        requester_signup_enabled=True,
        reaper_seconds=86400,
        public_url="http://testserver",
        claims_per_minute=1000,
    )
    with TestClient(create_app(store, settings)) as client:
        yield client, store, {"admin": {"token": secret, "key_id": admin.key_id}}
    store.close()


def run_step(
    client: TestClient, store: Store, saved: dict[str, Any], step: dict[str, Any], where: str
) -> None:
    if "session" in step:
        made = client.post("/v1/session/anonymous", json={"agent_label": step["session"]})
        assert made.status_code == 201, (where, made.text)
        data = made.json()
        saved[step["session"]] = {
            **data,
            "token": data["session_token"],
            "key_id": data["principal_id"],
        }
        return
    if "signup" in step:
        profile = {
            "display_name": "Synthetic Food Bank",
            "organization_type": "Food pantry",
            **step.get("profile", {}),
        }
        made = client.post("/v1/session/requester", json={"profile": profile})
        assert made.status_code == step.get("status", 201), (where, made.text)
        if made.status_code == 201:
            saved[step["signup"]] = {**made.json(), "token": made.json()["key"]}
            check(where, made.json(), fill(step.get("expect", {}), saved))
        return
    if "key" in step:
        grant = step["grant"]
        secret, issued = store.issue_key(
            step["key"],
            queues=grant["queues"],
            kinds=grant.get("kinds", ["*"]),
            effect_classes=grant.get("effect_classes", ["read", "write-internal"]),
            scopes=grant.get("scopes", []),
        )
        saved[step["key"]] = {"token": secret, "key_id": issued.key_id}
        return
    who = step["as"]
    headers = {**fill(step.get("headers", {}), saved)}
    if who != "none":
        headers["Authorization"] = "Bearer " + saved[who]["token"]
    for index in range(step.get("repeat", 1)):
        saved["rep"] = {"i": index}
        path = fill(step["path"], saved)
        if step["method"] == "GET":
            response = client.get(path, headers=headers)
        elif "raw" in step:  # exactly these bytes, with this Content-Type or none
            if step.get("content_type"):
                headers["Content-Type"] = step["content_type"]
            response = client.post(path, content=step["raw"].encode(), headers=headers)
        else:
            response = client.post(path, json=fill(step.get("body", {}), saved), headers=headers)
        assert response.status_code == step["status"], (where, path, response.text[:400])
    try:
        body = response.json() if response.content else None
    except ValueError:  # an MCP door may answer as an event stream
        body = None
    if "save" in step:
        saved[step["save"]] = body
    check(where, body, fill(step.get("expect", {}), saved))


@pytest.mark.parametrize("case", PARITY["cases"], ids=[case["id"] for case in PARITY["cases"]])
def test_access_case(
    case: dict[str, Any], fabric: tuple[TestClient, Store, dict[str, Any]]
) -> None:
    client, store, saved = fabric
    for index, step in enumerate(case["steps"]):
        run_step(client, store, saved, step, f"{case['id']} step {index}")
