"""Requester keys: a self-serve sign-up mints a hashed key that writes nouns only in its own
`org.<id>` namespace, files request verbs into the shared request queue and follows only the
request verbs it filed. Sign-ups, writes and filed requests are capped; personal data is refused."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera import ChildSpec, QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app

PROFILE = {
    "display_name": "Synthetic Food Bank",
    "organization_type": "Food pantry",
    "causes": ["food security"],
}


@pytest.fixture
def fabric(tmp_path: Path) -> Iterator[tuple[TestClient, Store, str]]:
    store = Store(str(tmp_path / "requesters.db"), clock=FakeClock())
    admin, _ = store.issue_key("operator", queues=["*"], scopes=["admin", "store"])
    for name in ("eq01", "eq02", "eq03"):
        store.define_queue(QueueConfig(name=name))
    settings = ServiceSettings(
        anonymous_access_enabled=True,
        requester_signup_enabled=True,
        reaper_seconds=86400,
        public_url="http://testserver",
    )
    with TestClient(create_app(store, settings)) as client:
        yield client, store, admin
    store.close()


def sign_up(client: TestClient, **profile: Any) -> dict[str, Any]:
    response = client.post("/v1/session/requester", json={"profile": {**PROFILE, **profile}})
    assert response.status_code == 201, response.text
    return dict(response.json())


def auth(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


def request_verb(task_id: str, brief: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "queue": "eq01",
        "kind": "request",
        "effect_class": "write-internal",
        "task_id": task_id,
        "brief": brief or {"job": "SYNTHETIC summary of public tenant laws"},
    }


def test_sign_up_mints_a_narrow_hashed_key_and_a_profile_noun(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    org = sign_up(client)
    assert org["principal_kind"] == "requester" and org["key"].startswith("tsk_")
    assert org["namespace"] == "org." + org["principal_id"]
    assert org["queues"] == ["eq01"] and org["kinds"] == ["request"]
    assert org["effect_classes"] == ["read", "write-internal"]
    assert set(org["scopes"]) == {
        "nouns:read",
        "cas:write",
        "verbs:submit",
        f"namespace:{org['namespace']}",
    }
    assert org["labels"]["origin"] == "requester" and org["quotas"]["request_verbs_per_hour"] == 5
    assert org["key"] not in Path(store.path).read_bytes().decode("utf-8", "ignore")
    h = auth(org["key"])
    assert client.get("/v1/me", headers=h).json()["namespace"] == org["namespace"]
    profile = client.get("/v1/stores/org.profile/records/profile", headers=h).json()
    assert profile["value"]["display_name"] == "Synthetic Food Bank"
    assert profile["visibility_scope"] == "private" and "origin:requester" in profile["tags"]
    contract = client.get("/connect.json").json()["requester"]
    assert contract["enabled"] is True and contract["quotas"]["signups_per_ip_hour"] == 3


def test_requester_files_and_follows_only_its_own_request_verbs(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    first, second = sign_up(client), sign_up(client, display_name="Synthetic Clinic")
    h, other = auth(first["key"]), auth(second["key"])
    filed = client.post("/ns/main/v1/tasks", json=request_verb("rq-1"), headers=h)
    assert filed.status_code == 201, filed.text
    verb = store.task("rq-1")
    assert verb.visibility_scope == "allowlist_surfaces"
    assert verb.allowed_surfaces == ["surface:eranos"] and verb.owner_key_id == first["key_id"]
    assert {"origin:requester", "untrusted:true", "kind:request"} <= set(verb.tags)
    assert client.get("/ns/main/v1/tasks/rq-1", headers=h).json()["state"] == "ready"
    assert client.get("/ns/main/v1/tasks/rq-1", headers=other).status_code == 404
    assert client.get("/ns/main/v1/tasks/rq-1").status_code == 401
    own = client.post(
        "/v1/stores/org.requests/records/r1",
        json={"value": {"job": "SYNTHETIC"}, "expected_version": 0},
        headers=h,
    )
    assert own.status_code == 200 and own.json()["visibility_scope"] == "private"


def test_requester_keys_reach_nothing_else(fabric: tuple[TestClient, Store, str]) -> None:
    client, _, _ = fabric
    first, second = sign_up(client), sign_up(client, display_name="Synthetic Clinic")
    h = auth(first["key"])
    store_writes = (
        ("/v1/stores/eranos.requests/records/r1", {"value": {}}),
        ("/ns/main/v1/stores/eranos.catalog/records/KIT-X-01", {"value": {}}),
    )
    for path, body in store_writes:
        assert client.post(path, json=body, headers=h).status_code == 403, path
    for body in (
        {**request_verb("x1"), "queue": "eq02"},
        {**request_verb("x2"), "kind": "build"},
        {**request_verb("x3"), "depends_on": ["rq-1"]},
    ):
        assert client.post("/ns/main/v1/tasks", json=body, headers=h).status_code == 403, body
    assert (
        client.post(
            "/ns/main/v1/tasks/claim", json={"queue": "eq01", "worker": "w"}, headers=h
        ).status_code
        == 403
    )
    for path in (
        "/ns/main/v1/stores/eranos.catalog/records/KIT-X-01",
        "/ns/main/v1/search?q=kit",
        "/ns/main/v1/counts",
        f"/ns/{second['namespace']}/v1/stores/org.profile/records/profile",
        "/ns/conformance-other/v1/stores",
    ):
        assert client.get(path, headers=h).status_code == 403, path
    assert client.get("/v1/admin/audit", headers=h).status_code == 403
    assert (
        client.post(
            "/v1/keys", json={"label": "x", "queues": ["*"], "scopes": ["admin"]}, headers=h
        ).status_code
        == 403
    )


def test_personal_data_and_quotas_hold(fabric: tuple[TestClient, Store, str]) -> None:
    client, _, _ = fabric
    org = sign_up(client)
    h = auth(org["key"])
    for value in ("write to person@real.invalid", "123-45-6789", "4111 1111 1111 1111"):
        brief = {"job": value}
        assert (
            client.post("/ns/main/v1/tasks", json=request_verb("pii", brief), headers=h).status_code
            == 400
        )
        record = {"value": {"note": value}, "expected_version": 0}
        assert (
            client.post("/v1/stores/org.requests/records/pii", json=record, headers=h).status_code
            == 400
        )
    refused = client.post(
        "/v1/session/requester",
        json={"profile": {**PROFILE, "organization_type": "Clinic 4111 1111 1111 1111"}},
    )
    assert refused.status_code == 400
    for i in range(5):
        filed = client.post("/ns/main/v1/tasks", json=request_verb(f"rq-{i}"), headers=h)
        assert filed.status_code == 201, filed.text
    capped = client.post("/ns/main/v1/tasks", json=request_verb("rq-6"), headers=h)
    assert capped.status_code == 429 and int(capped.headers["retry-after"]) > 0
    # Three sign-ups per IP hash each hour: the fixture's one plus two more, then refused.
    sign_up(client, display_name="Second Org")
    sign_up(client, display_name="Third Org")
    blocked = client.post(
        "/v1/session/requester", json={"profile": {**PROFILE, "display_name": "Fourth Org"}}
    )
    assert blocked.status_code == 429


def test_sign_up_is_off_unless_enabled(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "off.db"), clock=FakeClock())
    settings = ServiceSettings(reaper_seconds=86400, public_url="http://testserver")
    with TestClient(create_app(store, settings)) as client:
        assert client.post("/v1/session/requester", json={"profile": PROFILE}).status_code == 503
        assert client.get("/connect.json").json()["requester"]["enabled"] is False
    store.close()


def test_requester_follows_its_request_but_not_the_split_children(
    fabric: tuple[TestClient, Store, str],
) -> None:
    client, store, _ = fabric
    org = sign_up(client)
    h = auth(org["key"])
    assert (
        client.post("/ns/main/v1/tasks", json=request_verb("rq-split"), headers=h).status_code
        == 201
    )
    lease = store.claim("eq01", "classifier", kinds=["request"], key_id="k_classifier")
    assert lease is not None and lease.task_id == "rq-split"
    children = store.split(
        "rq-split",
        lease.fencing_token,
        [
            ChildSpec(queue="eq02", kind="research", brief={"part": 1}, tags=["open:anonymous"]),
            ChildSpec(queue="eq03", kind="report_review", brief={"part": 1}, after=[0]),
        ],
    )
    assert client.get("/ns/main/v1/tasks/rq-split", headers=h).json()["state"] == "waiting"
    for child in children:
        assert store.task(child).owner_key_id == org["key_id"]
        assert client.get(f"/ns/main/v1/tasks/{child}", headers=h).status_code == 404
        assert client.get(f"/ns/main/v1/tasks/{child}/results", headers=h).status_code == 404
