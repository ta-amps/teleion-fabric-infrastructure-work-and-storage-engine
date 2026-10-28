"""Category projections agree before bounded scans, without granting visibility."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import BucketConfig, FakeClock, InvalidRequest, NotFound, Store
from tessera.access import ServiceSettings
from tessera.models import RecordWrite, canonical_category_id, tag_matches_category
from tessera.ontology.categories import normalize_tags as ontology_tags
from tessera.service import create_app


def test_category_aliases_conflicts_and_underscore_identity(tmp_path: Path) -> None:
    store = Store(tmp_path / "categories.db")
    for key, value, tags in [
        ("id", {"category_id": "noun.code_node"}, ["domain:legal", "industry:ngo"]),
        ("alias", {"category": "cat:NOUN.CODE_NODE"}, []),
        ("tag", {}, ["cat:noun.code_node"]),
        ("agree", {"category_id": "noun.code_node"}, ["category:noun.code_node"]),
    ]:
        store.put_record("examples", key, value, expected_version=0, tags=tags)
    assert canonical_category_id(" CAT:Noun.Code_Node ") == "noun.code_node"
    assert ontology_tags(["category:noun.code_node"]) == ("category:noun.code_node",)
    assert tag_matches_category(ontology_tags(["category:noun.code_node"]), "noun.code_node")
    assert not tag_matches_category(["category:noun.code-node"], "noun.code_node")
    assert {r.key for r in store.list_records("examples", category="noun.code_node")} == {
        "id",
        "alias",
        "tag",
        "agree",
    }
    assert len(store.list_records("examples", tag="cat:noun.code_node")) == 4
    assert [
        r.key for r in store.list_records("examples", category="noun.code_node", tag="domain:legal")
    ] == ["id"]
    assert "industry:ngo" in store.get_record("examples", "id").tags
    assert store.list_records("examples", category="noun.code-node") == []
    for value, tags in [
        ({"category_id": "noun.code_node", "category": "noun.feed_source"}, []),
        ({"category_id": "noun.code_node"}, ["cat:noun.feed_source"]),
        ({"category_id": "noun.code_node", "tags": ["category:noun.feed_source"]}, []),
        ({"category_id": "noun.code_node", "tags": {"category": "noun.feed_source"}}, []),
        ({"category_id": "noun.code_node", "metadata_tags": {"category": "noun.feed_source"}}, []),
    ]:
        with pytest.raises(InvalidRequest, match="conflicting category"):
            store.put_record("examples", "conflict", value, expected_version=0, tags=tags)
    with pytest.raises(InvalidRequest, match="conflicting category"):
        store.put_records(
            [
                RecordWrite(
                    store="examples", key="rollback", value={"category_id": "noun.code_node"}
                ),
                RecordWrite(
                    store="examples",
                    key="conflict",
                    value={"category_id": "noun.code_node"},
                    tags=["category:noun.feed_source"],
                ),
            ]
        )
    with pytest.raises(NotFound):
        store.get_record("examples", "rollback")
    store.define_bucket(BucketConfig(name="examples"))
    with pytest.raises(InvalidRequest, match="conflicting category"):
        store.put_object(
            "examples",
            "bad.txt",
            b"synthetic",
            expected_version=0,
            metadata={"category_id": "noun.code_node"},
            tags=["cat:noun.feed_source"],
        )
    store.close()


def test_category_prefilter_legacy_rows_and_http_rankings(tmp_path: Path) -> None:
    clock = FakeClock()
    store = Store(tmp_path / "legacy.db", clock=clock)
    common = {"trials": 1, "successes": 1, "synthetic": True}
    store.put_record(
        "examples",
        "z-target",
        {**common, "category_id": "noun.mcp_server"},
        expected_version=0,
        tags=["purpose:probe"],
    )
    store.put_record(
        "examples",
        "legacy-conflict",
        {**common, "category_id": "noun.feed_source"},
        expected_version=0,
    )
    # Reproduce stored pre-upgrade rows without changing normal write validation.
    with store._tx() as c:
        for table in ("records", "record_heads"):
            c.execute(f"UPDATE {table} SET tags='[]' WHERE store='examples' AND key='z-target'")  # noqa: S608
            c.execute(
                f"UPDATE {table} SET tags=? WHERE store='examples' AND key='legacy-conflict'",  # noqa: S608
                ('["category:noun.mcp_server"]',),
            )
        c.execute("DELETE FROM noun_tags WHERE container='examples' AND record_id='z-target'")
    clock.advance(1)
    store.put_records(
        [
            RecordWrite(
                store="examples",
                key=f"a-{i:04}",
                value={**common, "category_id": "noun.feed_source"},
            )
            for i in range(1001)
        ]
    )
    store.put_record(
        "examples",
        "private",
        {**common, "category_id": "noun.mcp_server"},
        expected_version=0,
        visibility_scope="private",
        owner_key_id="synthetic-other",
    )
    key, _ = store.issue_key("synthetic-reader", queues=["conformance-query"], scopes=["store"])
    app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + key}
        for path, field in [
            ("/v1/stores/examples/records?category=noun.mcp_server", "records"),
            ("/v1/search?store=examples&category=noun.mcp_server", "scents"),
            ("/v1/rankings/noun.mcp_server?store=examples", "ranked"),
        ]:
            reply = client.get(path, headers=headers)
            assert reply.status_code == 200, reply.text
            assert [r.get("key", r.get("id")) for r in reply.json()[field]] == ["z-target"]
        assert (
            client.get(
                "/v1/stores/examples/records?category=noun.mcp_server&q=doesnotmatch",
                headers=headers,
            ).json()["records"]
            == []
        )
    store.close()


def test_bucket_category_prefilter_after_unrelated_objects(tmp_path: Path) -> None:
    store = Store(tmp_path / "objects.db", clock=FakeClock())
    store.define_bucket(BucketConfig(name="examples"))
    store.put_object(
        "examples",
        "z-target.txt",
        b"synthetic",
        expected_version=0,
        metadata={"category_id": "noun.code_node"},
    )
    for i in range(1001):
        store.put_object(
            "examples",
            f"a-{i:04}.txt",
            b"synthetic",
            expected_version=0,
            metadata={"category_id": "noun.feed_source"},
        )
    assert [r.key for r in store.list_objects("examples", category="noun.code_node")] == [
        "z-target.txt"
    ]
    assert [r.key for r in store.search_nouns(bucket="examples", category="noun.code_node")] == [
        "z-target.txt"
    ]
    store.close()


def test_worker_category_queries_actual_sqlite() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--import",
            str(root / "tests/edge_access_loader.mjs"),
            str(root / "tests/edge_category_queries.mjs"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True
