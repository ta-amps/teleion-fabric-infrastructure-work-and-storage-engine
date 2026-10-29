"""Tests for Tessera Wave-2 Categorized Ontology, Empirical-Bayes Rankings, Fragility Decay,
3-Way CAS Reconciliation, Sybil-Fenced Voting, and HTTP route parity."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tessera.access import ServiceSettings
from tessera.ontology import (
    CALLER_PROFILES,
    NOUN_CATEGORIES,
    VERB_CATEGORIES,
    empirical_bayes_mean,
    plan_fragility_rechecks,
    reconcile_three_way,
    wilson_lower_bound,
)
from tessera.service import create_app
from tessera.store import Store


def test_ontology_registry_counts_and_bayesian_shrinkage() -> None:
    assert len(NOUN_CATEGORIES) == 20
    assert len(VERB_CATEGORIES) == 18
    assert len(CALLER_PROFILES) == 8
    # 1-of-1 lucky item cannot beat 48-of-50 battle-tested item under EB shrinkage + Wilson
    eb_lucky = empirical_bayes_mean(1.0, 1)
    eb_proven = empirical_bayes_mean(48.0 / 50.0, 50)
    assert eb_proven > eb_lucky
    assert wilson_lower_bound(48, 50) > wilson_lower_bound(1, 1)


def test_three_way_reconcile_and_fragility_planner() -> None:
    res = reconcile_three_way(
        {"title": "v1", "tags": ["a"], "count": 2},
        {"title": "v1", "tags": ["a", "b"], "count": 3, "owner": "w1"},
        {"title": "v1", "tags": ["a", "c"], "count": 5, "reviewer": "w2"},
    )
    assert res.status == "auto_merged"
    assert res.merged is not None
    assert res.merged["count"] == 6
    assert sorted(res.merged["tags"]) == ["a", "b", "c"]

    tasks = plan_fragility_rechecks(
        [
            {
                "item_id": "feed-advisory-1",
                "category_id": "noun.feed_source",
                "half_life_hours": 12.0,
                "fragility_score": 0.90,
                "last_verified_at": 1_000_000.0,
                "importance": 1.0,
            }
        ],
        now_ts=1_000_000.0 + 24 * 3600.0,
        max_batch=5,
    )
    assert len(tasks) == 1
    assert tasks[0].verb_category == "verb.recheck_fragility"


def test_http_ontology_rankings_fragility_and_voting_routes(tmp_path: Path) -> None:
    store = Store(tmp_path / "ont.db")
    secret, _ = store.issue_key("admin-test", queues=["*"], scopes=["admin", "store"])
    app = create_app(store, ServiceSettings(db_path=tmp_path / "ont.db"))
    headers = {"authorization": f"Bearer {secret}"}

    with TestClient(app) as client:
        cats = client.get("/v1/categories?profile=autonomous_coding_harness", headers=headers)
        assert cats.status_code == 200
        cbody = cats.json()
        assert len(cbody["noun_categories"]) == len(NOUN_CATEGORIES)
        assert len(cbody["verb_categories"]) == len(VERB_CATEGORIES)
        assert cbody["active_profile"]["profile_id"] == "autonomous_coding_harness"

        for idx, (trials, succ) in enumerate([(2, 2), (40, 39)]):
            client.post(
                f"/v1/stores/registry.mcp_servers/records/mcp-{idx}",
                headers=headers,
                json={
                    "value": {
                        "id": f"mcp-{idx}",
                        "category": "noun.mcp_server",
                        "title": f"Server {idx}",
                        "trials": trials,
                        "successes": succ,
                        "dimensions": {"schema_completeness": 0.95, "transport_reliability": 0.92},
                    },
                    "expected_version": None,
                },
            )
        ranked = client.get(
            "/v1/rankings/noun.mcp_server",
            params={"store": "registry.mcp_servers", "profile": "autonomous_coding_harness"},
            headers=headers,
        )
        assert ranked.status_code == 200
        rrows = ranked.json()["ranked"]
        assert rrows[0]["id"] == "mcp-1"
        assert "adapted_view" in rrows[0]

        client.post(
            "/v1/stores/registry.feeds/records/feed-0",
            headers=headers,
            json={
                "value": {
                    "id": "feed-0",
                    "category": "noun.feed_source",
                    "url": "https://example.org/rss.xml",
                    "half_life_hours": 12.0,
                    "fragility_score": 0.92,
                },
                "expected_version": None,
            },
        )
        frag = client.get(
            "/v1/fragility/plan",
            params={"store": "registry.feeds", "budget": 5, "simulate_hours_ahead": 20},
            headers=headers,
        )
        assert frag.status_code == 200
        assert len(frag.json()["plan"]) == 1

        rec = client.post(
            "/v1/reconcile",
            headers=headers,
            json={
                "ancestor": {"tags": ["x"], "verified_hits": 1},
                "left": {"tags": ["x", "y"], "verified_hits": 2},
                "right": {"tags": ["x", "z"], "verified_hits": 3},
                "monotonic_set_fields": ["tags"],
                "counter_fields": ["verified_hits"],
            },
        )
        assert rec.status_code == 200
        assert rec.json()["clean"] is True
        assert rec.json()["merged"]["verified_hits"] == 4

        vote = client.post(
            "/v1/reconcile/adjudicate",
            headers=headers,
            json={
                "conflict_id": "conf-1",
                "proposer_id": "worker-prop",
                "candidates": {"cand_a": {"v": 1}, "cand_b": {"v": 2}},
                "ballots": [
                    {
                        "voter_id": "worker-prop",
                        "key_prefix": "k_prop",
                        "choice": "cand_b",
                        "verified_passes": 10,
                        "verified_failures": 0,
                    },
                    {
                        "voter_id": "rev-1",
                        "key_prefix": "k_r1",
                        "choice": "cand_a",
                        "verified_passes": 20,
                        "verified_failures": 1,
                    },
                    {
                        "voter_id": "rev-2",
                        "key_prefix": "k_r2",
                        "choice": "cand_a",
                        "verified_passes": 18,
                        "verified_failures": 1,
                    },
                ],
            },
        )
        assert vote.status_code == 200
        assert vote.json()["winner"] == "cand_a"


def test_worker_catalog_matches_every_python_category_and_profile() -> None:
    """A full registry comparison catches missing categories, fields, axes and profile policies."""
    import importlib.util
    import json
    import shutil
    import subprocess

    import pytest

    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime required for Worker parity")
    script = """
import {NOUN_CATEGORIES,VERB_CATEGORIES,CALLER_PROFILES,adaptNounView,validateCategoryPayload} from './deploy/cloudflare/ontology_engine.js';
const validations={};
for(const [key,spec] of Object.entries({...NOUN_CATEGORIES,...VERB_CATEGORIES})) {
  const dimensions=Object.fromEntries(Object.entries(spec.dimensions).map(([name,d])=>[name,d.dtype==='categorical'?d.allowed_values.at(-1):d.dtype==='int'?Math.ceil(d.min_val):d.min_val]));
  validations[key]=validateCategoryPayload(key,dimensions);
}
const alias=adaptNounView({noun_id:'synthetic',category_id:'noun.code_node',title:'Synthetic'},'swarm_worker');
console.log(JSON.stringify({nouns:NOUN_CATEGORIES,verbs:VERB_CATEGORIES,profiles:CALLER_PROFILES,validations,alias_profile:alias.profile_id}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    assert data["nouns"] == {
        key: value.model_dump(mode="json") for key, value in NOUN_CATEGORIES.items()
    }
    assert data["verbs"] == {
        key: value.model_dump(mode="json") for key, value in VERB_CATEGORIES.items()
    }
    assert data["profiles"] == {
        key: value.model_dump(mode="json") for key, value in CALLER_PROFILES.items()
    }
    assert data["alias_profile"] == "autonomous_coding_harness"
    assert all(result["valid"] for result in data["validations"].values())
    spec = importlib.util.spec_from_file_location(
        "build_ontology_catalog", root / "scripts/build_ontology_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (root / "deploy/cloudflare/ontology_catalog.js").read_text() == module.generated_source()
