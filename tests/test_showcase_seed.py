"""Creative seeds remain bounded and idempotent through actual Store operations."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tessera import QueueConfig, Store


def module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "showcase_seed", Path(__file__).resolve().parents[1] / "tools/showcase_seed.py"
    )
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def test_seed_pack_is_bounded_and_no_server_inference() -> None:
    tool = module()
    pack = tool.load_pack()
    assert len(tool.nouns(pack)) == 21
    assert pack["bounds"]["automatic_children"] == 0
    assert pack["bounds"]["server_inference"] is False
    assert pack["evaluation"]["unknown"] is None
    assert set(pack["procedures"]) == tool.STAGES


def test_prepare_replays_without_overwrite_or_duplicate_tasks(tmp_path: Path) -> None:
    tool = module()
    store = Store(tmp_path / "seed.db")
    _, grant = store.issue_key(
        "synthetic-showcase",
        queues=["conformance-showcase"],
        scopes=["store", "namespace:conformance-showcase"],
    )
    with store.scope_context(
        namespace="conformance-showcase", key_id=grant.key_id, surface="surface:tessera"
    ):
        store.define_queue(
            QueueConfig(name="conformance-showcase"), namespace="conformance-showcase"
        )
        pack = tool.load_pack()
        a = tool.prepare(store, pack, "conformance-showcase", "conformance-showcase")
        b = tool.prepare(store, pack, "conformance-showcase", "conformance-showcase")
        assert a == b and len(set(a["task_ids"])) == 22
        assert all(ref["version"] == 1 for ref in a["noun_refs"].values())
        first = store.task(a["task_ids"][0]).brief
        second = store.task(a["task_ids"][1]).brief
        assert first["shared_brief_hash"] == second["shared_brief_hash"]
        assert first["attempt_index"] != second["attempt_index"]
        changed = {**pack, "bounds": {**pack["bounds"], "max_candidates": 2}}
        with pytest.raises(ValueError, match="identity conflict"):
            tool.prepare(store, changed, "conformance-showcase", "conformance-showcase")
    store.close()


def test_shape_cannot_claim_verified_idea() -> None:
    tool = module()
    idea = {
        "title": "SYNTHETIC test",
        "beneficiary": "test reader",
        "mechanism": "compare fixtures",
        "difference": "different input",
        "assumptions": ["input exists"],
        "proposed_nouns": ["fixture"],
        "proposed_verbs": ["compare"],
        "falsifiable_test": "negative control fails",
        "evidence_status": "verified",
    }
    with pytest.raises(ValueError, match="ideation cannot assert verified status"):
        tool.Ideas.model_validate({"synthetic": True, "theme_id": "cheap-orbit", "ideas": [idea]})


def test_preview_never_constructs_a_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = module()
    monkeypatch.setattr(
        tool, "Client", lambda *_a, **_k: pytest.fail("offline preview called a service")
    )
    assert tool.main([]) == 0
    assert '"dry_run": true' in capsys.readouterr().out


@pytest.mark.parametrize("variants", [0, 5, True, 1.5])
def test_invalid_variant_choice_has_no_effects(variants: object) -> None:
    tool = module()
    with pytest.raises(ValueError, match="one to four"):
        tool.prepare(
            None, tool.load_pack(), "conformance-showcase", "conformance-showcase", variants
        )
