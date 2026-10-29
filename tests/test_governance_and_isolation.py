"""Universal 6-field governance envelope, namespace isolation (/ns/{namespace} & X-Namespace),
Layer 1 surface ownership & quarantine lens, and Layer 2 combinable query filters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tessera import BucketConfig, QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.service import create_app


def _auth(key: str, *, surface: str | None = None, namespace: str | None = None) -> dict[str, str]:
    hdrs = {"authorization": f"Bearer {key}"}
    if surface is not None:
        hdrs["x-surface-id"] = surface
    if namespace is not None:
        hdrs["x-namespace"] = namespace
    return hdrs


def _mcp_call(
    client: TestClient,
    key: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    surface: str | None = None,
    path: str = "/mcp",
) -> dict[str, Any]:
    hdrs = {**_auth(key, surface=surface), "accept": "application/json, text/event-stream"}
    init = client.post(
        path,
        headers=hdrs,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gov-test", "version": "0"},
            },
        },
    )
    assert init.status_code == 200, init.text
    ver = init.json()["result"]["protocolVersion"]
    resp = client.post(
        path,
        headers={**hdrs, "mcp-protocol-version": ver},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    res: dict[str, Any] = resp.json()["result"]
    return res


def test_namespace_isolation_across_path_and_header(tmp_path: Path) -> None:
    db_path = tmp_path / "ns.db"
    store = Store(db_path)
    admin_key, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))

    with TestClient(app) as client:
        # Conflicting /ns/{a} and X-Namespace: {b} returns 400 invalid_request.
        conflict = client.get("/ns/alpha/v1/stores", headers=_auth(admin_key, namespace="beta"))
        assert conflict.status_code == 400 and conflict.json()["code"] == "invalid_request"

        # Define queue + bucket + record + object + blob + task + feed in alpha.
        assert (
            client.post(
                "/ns/alpha/v1/queues/define",
                headers=_auth(admin_key),
                json={"name": "solve"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/ns/alpha/v1/buckets/define",
                headers=_auth(admin_key),
                json={"name": "nouns", "description": "alpha nouns"},
            ).status_code
            == 200
        )
        put_rec = client.post(
            "/ns/alpha/v1/stores/shared.notes/records/R-1",
            headers=_auth(admin_key, surface="kothar"),
            json={
                "value": {"title": "Alpha blueprint", "l0_summary": "Isolated alpha record"},
                "tags": ["blueprint"],
            },
        )
        assert put_rec.status_code == 200
        assert put_rec.json()["namespace"] == "alpha"
        assert put_rec.json()["producer_surface"] == "surface:kothar"

        put_obj = client.post(
            "/ns/alpha/v1/buckets/nouns/objects/spec/alpha.json",
            headers={**_auth(admin_key), "content-type": "application/json"},
            content=b'{"title":"Alpha object"}',
        )
        assert put_obj.status_code == 201
        sha = put_obj.json()["sha256"]

        enq = client.post(
            "/ns/alpha/v1/tasks",
            headers=_auth(admin_key),
            json={"queue": "solve", "brief": {"goal": "alpha only"}},
        )
        assert enq.status_code == 201
        tid = enq.json()["task_id"]

        pub = client.post(
            "/ns/alpha/v1/feeds/deltas",
            headers=_auth(admin_key),
            json={"payload": {"step": 1}, "summary": "alpha delta"},
        )
        assert pub.status_code == 201 and pub.json()["seq"] == 1

        # Cross-namespace reads from beta or main MUST return 404 or 0 search hits.
        assert (
            client.get(
                "/ns/beta/v1/stores/shared.notes/records/R-1", headers=_auth(admin_key)
            ).status_code
            == 404
        )
        assert (
            client.get("/v1/stores/shared.notes/records/R-1", headers=_auth(admin_key)).status_code
            == 404
        )
        assert (
            client.get(
                "/ns/beta/v1/buckets/nouns/objects/spec/alpha.json", headers=_auth(admin_key)
            ).status_code
            == 404
        )
        assert client.get(f"/ns/beta/v1/blobs/{sha}", headers=_auth(admin_key)).status_code == 404
        assert client.get(f"/ns/beta/v1/tasks/{tid}", headers=_auth(admin_key)).status_code == 404
        beta_search = client.get("/ns/beta/v1/search?q=Alpha", headers=_auth(admin_key)).json()[
            "scents"
        ]
        assert beta_search == []

        # Reading via X-Namespace: alpha succeeds identically to /ns/alpha/...
        alpha_rec = client.get(
            "/v1/stores/shared.notes/records/R-1",
            headers=_auth(admin_key, namespace="alpha"),
        )
        assert alpha_rec.status_code == 200
        assert alpha_rec.json()["value"]["title"] == "Alpha blueprint"
        alpha_search = client.get(
            "/v1/search?q=Alpha", headers=_auth(admin_key, namespace="alpha")
        ).json()["scents"]
        assert len(alpha_search) == 2


def test_layer1_protected_surface_ownership_and_eq03_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "surface_auth.db"
    store = Store(db_path)
    store.define_queue(QueueConfig(name="eq01"))
    store.define_queue(QueueConfig(name="eq03"))
    admin_key, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
    shamash_key, _ = store.issue_key(
        "shamash-svc", queues=["aq*"], scopes=["store", "surface:shamash"]
    )
    eranos_no_eq03, _ = store.issue_key(
        "eranos-worker", queues=["eq01"], scopes=["store", "surface:eranos"]
    )
    eranos_eq03, _ = store.issue_key(
        "eranos-indexer", queues=["eq01", "eq03"], scopes=["store", "surface:eranos"]
    )
    generic_store_key, _ = store.issue_key("generic", queues=["eq01"], scopes=["store"])

    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))
    with TestClient(app) as client:
        # 1. Shamash key can write shamash.verdicts, cannot write herma.provisions or spoof X-Surface-Id: herma.
        ok_shamash = client.post(
            "/v1/stores/shamash.verdicts/records/V-1",
            headers=_auth(shamash_key),
            json={"value": {"verdict": "pass"}},
        )
        assert ok_shamash.status_code == 200
        assert ok_shamash.json()["producer_surface"] == "surface:shamash"

        denied_herma = client.post(
            "/v1/stores/herma.provisions/records/P-1",
            headers=_auth(shamash_key),
            json={"value": {"rule": "r1"}},
        )
        assert denied_herma.status_code == 403 and denied_herma.json()["code"] == "scope_denied"

        spoof = client.post(
            "/v1/stores/herma.provisions/records/P-1",
            headers=_auth(shamash_key, surface="herma"),
            json={"value": {"rule": "r1"}},
        )
        assert spoof.status_code == 403 and spoof.json()["code"] == "scope_denied"

        # MCP put_record enforces the exact same gate.
        mcp_denied = _mcp_call(
            client,
            shamash_key,
            "put_record",
            {"store_name": "kinema.scenes", "key": "S-1", "value": {"fps": 24}},
        )
        assert (
            mcp_denied.get("isError") is True and "scope_denied" in mcp_denied["content"][0]["text"]
        )

        # A generic store key cannot gain surface authority by presenting a header.
        assert (
            client.post(
                "/v1/stores/kothar.packs/records/K-1",
                headers=_auth(generic_store_key),
                json={"value": {"pack": 1}},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/stores/kothar.packs/records/K-1",
                headers=_auth(generic_store_key, surface="kothar"),
                json={"value": {"pack": 1}},
            ).status_code
            == 403
        )

        # 2. eranos.catalog and eranos.index require BOTH surface:eranos AND explicit 'eq03' queue grant.
        assert (
            client.post(
                "/v1/stores/eranos.staging/records/ST-1",
                headers=_auth(eranos_no_eq03),
                json={"value": {"kit": "draft"}},
            ).status_code
            == 200
        )
        for protected_cat in ("eranos.catalog", "eranos.index"):
            denied_cat = client.post(
                f"/v1/stores/{protected_cat}/records/C-1",
                headers=_auth(eranos_no_eq03),
                json={"value": {"kit": "published"}},
            )
            assert denied_cat.status_code == 403 and denied_cat.json()["code"] == "scope_denied"

            ok_cat = client.post(
                f"/v1/stores/{protected_cat}/records/C-1",
                headers=_auth(eranos_eq03),
                json={"value": {"kit": "published"}},
            )
            assert ok_cat.status_code == 403  # A queue grant alone is not a completed review proof.

        # Admin can always write any surface store.
        assert (
            client.post(
                "/v1/stores/eranos.catalog/records/C-ADMIN",
                headers=_auth(admin_key),
                json={"value": {"kit": "admin"}},
            ).status_code
            == 200
        )


def test_visibility_quarantine_and_layer2_combinable_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "gov_lens.db"
    store = Store(db_path)
    store.define_bucket(BucketConfig(name="kits", description="kits"))
    admin_key, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
    eranos_key, _ = store.issue_key("eranos", queues=["*"], scopes=["store", "surface:eranos"])
    shamash_key, _ = store.issue_key("shamash", queues=["*"], scopes=["store", "surface:shamash"])
    herma_key, _ = store.issue_key("herma", queues=["*"], scopes=["store", "surface:herma"])

    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))
    with TestClient(app) as client:
        # 1. Visibility scopes: private, surface_only, allowlist_surfaces, public_commons.
        client.post(
            "/v1/stores/commons/records/priv-1",
            headers=_auth(eranos_key),
            json={"value": {"title": "Secret kit"}, "visibility_scope": "private"},
        )
        client.post(
            "/v1/stores/commons/records/surf-1",
            headers=_auth(eranos_key),
            json={"value": {"title": "Eranos internal"}, "visibility_scope": "surface_only"},
        )
        client.post(
            "/v1/stores/commons/records/allow-1",
            headers=_auth(eranos_key),
            json={
                "value": {"title": "Shared with Shamash"},
                "visibility_scope": "allowlist_surfaces",
                "allowed_surfaces": ["surface:shamash"],
            },
        )
        client.post(
            "/v1/stores/commons/records/pub-1",
            headers=_auth(eranos_key),
            json={
                "value": {"title": "Water sanitation triage kit"},
                "visibility_scope": "public_commons",
                "tags": ["wash", "category:humanitarian"],
                "target_cohorts": ["all"],
            },
        )
        client.post(
            "/v1/stores/commons/records/restricted-1",
            headers=_auth(eranos_key),
            json={
                "value": {"title": "Restricted clinical protocol"},
                "visibility_scope": "public_commons",
                "tags": ["clinical", "category:medical"],
                "target_cohorts": ["cohort:clinical-v1"],
            },
        )

        # Herma sees only pub-1 in default list (restricted-1 requires ?cohort=cohort:clinical-v1).
        herma_list = [
            r["key"]
            for r in client.get("/v1/stores/commons/records", headers=_auth(herma_key)).json()[
                "records"
            ]
        ]
        assert herma_list == ["pub-1"]
        assert (
            client.get("/v1/stores/commons/records/allow-1", headers=_auth(herma_key)).status_code
            == 404
        )
        # Shamash is on allowlist_surfaces for allow-1.
        assert (
            client.get("/v1/stores/commons/records/allow-1", headers=_auth(shamash_key)).status_code
            == 200
        )
        # Eranos (owner & producer_surface) can read priv-1 and surf-1; Shamash gets 404 on both.
        assert (
            client.get("/v1/stores/commons/records/priv-1", headers=_auth(eranos_key)).status_code
            == 200
        )
        assert (
            client.get("/v1/stores/commons/records/priv-1", headers=_auth(shamash_key)).status_code
            == 404
        )
        assert (
            client.get("/v1/stores/commons/records/surf-1", headers=_auth(shamash_key)).status_code
            == 404
        )

        # 2. Quarantine hiding: status:quarantined NEVER appears in /v1/search or /v1/reflexes/match.
        client.post(
            "/v1/stores/tessera.reflexes/records/rx-good",
            headers=_auth(eranos_key),
            json={
                "value": {
                    "pattern": "triage wash intake",
                    "verified_hits": 4,
                    "steps": [{"op": "verify"}],
                },
                "tags": ["wash", "status:verified"],
            },
        )
        client.post(
            "/v1/stores/tessera.reflexes/records/rx-quarantined",
            headers=_auth(eranos_key),
            json={
                "value": {
                    "pattern": "triage wash intake",
                    "verified_hits": 10,
                    "steps": [{"op": "bad"}],
                },
                "tags": ["wash", "status:quarantined"],
            },
        )

        # Even admin with include_restricted=true NEVER gets status:quarantined in /v1/search or /v1/reflexes/match!
        s_hits = client.get(
            "/v1/search?q=triage+wash&store=tessera.reflexes&include_restricted=true",
            headers=_auth(admin_key),
        ).json()["scents"]
        assert [h["key"] for h in s_hits] == ["rx-good"]
        s_all = client.get(
            "/v1/search?q=triage+wash&include_restricted=true", headers=_auth(admin_key)
        ).json()["scents"]
        assert "rx-quarantined" not in [h["key"] for h in s_all]

        rx_hits = client.get(
            "/v1/reflexes/match?q=triage+wash+intake&include_restricted=true",
            headers=_auth(admin_key),
        ).json()["hits"]
        assert [h["reflex_id"] for h in rx_hits] == ["rx-good"]

        # Direct GET on quarantined noun: 404 for shamash, 200 for producer (eranos) and admin.
        assert (
            client.get(
                "/v1/stores/tessera.reflexes/records/rx-quarantined",
                headers=_auth(shamash_key),
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/v1/stores/tessera.reflexes/records/rx-quarantined",
                headers=_auth(eranos_key),
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/v1/stores/tessera.reflexes/records/rx-quarantined",
                headers=_auth(admin_key),
            ).status_code
            == 200
        )

        # 3. Combinable Layer 2 query filters (?q=&tag=&category=&cohort=&surface=).
        combined = client.get(
            "/v1/search?q=sanitation&tag=wash&category=humanitarian&surface=eranos",
            headers=_auth(herma_key),
        ).json()["scents"]
        assert [c["key"] for c in combined] == ["pub-1"]

        # Restricted cohort surfaces ONLY when explicitly requested via ?cohort=cohort:clinical-v1 (or admin + include_restricted).
        default_med = client.get("/v1/search?q=clinical", headers=_auth(herma_key)).json()["scents"]
        assert default_med == []

        explicit_cohort = client.get(
            "/v1/search?q=clinical&cohort=cohort:clinical-v1&category=medical",
            headers=_auth(herma_key),
        ).json()["scents"]
        assert [c["key"] for c in explicit_cohort] == ["restricted-1"]


def test_claimed_content_identity_must_match_uploaded_bytes(tmp_path: Path) -> None:
    """A governance header may assert sha256/size as an integrity check, never set them."""
    db_path = tmp_path / "identity.db"
    store = Store(db_path)
    admin_key, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))
    secret, dummy = b"SYNTHETIC private bytes", b"synthetic other bytes"
    secret_sha = hashlib.sha256(secret).hexdigest()
    dummy_sha = hashlib.sha256(dummy).hexdigest()

    def gov(value: dict[str, Any]) -> dict[str, str]:
        return {**_auth(admin_key), "x-tessera-governance": json.dumps(value)}

    with TestClient(app) as client:
        assert client.post("/v1/blobs", content=secret, headers=gov({})).status_code == 201
        for path in ("/v1/blobs", "/v1/buckets/nouns/objects/mine.txt"):
            if path.startswith("/v1/buckets"):
                client.post("/v1/buckets/define", headers=_auth(admin_key), json={"name": "nouns"})
            for claim in ({"sha256": secret_sha}, {"size": len(dummy) + 1}, {"size": "21"}):
                spoof = client.post(path, content=dummy, headers=gov(claim))
                assert spoof.status_code == 400, (path, claim, spoof.text)
                assert spoof.json()["code"] == "invalid_request"
            honest = client.post(
                path, content=dummy, headers=gov({"sha256": dummy_sha.upper(), "size": len(dummy)})
            )
            assert honest.status_code == 201, honest.text
            assert honest.json()["sha256"] == dummy_sha
        obj = client.get("/v1/buckets/nouns/objects/mine.txt", headers=_auth(admin_key))
        assert obj.status_code == 200 and obj.content == dummy
