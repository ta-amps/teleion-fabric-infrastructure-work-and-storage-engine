"""Seed pack: the learner's nouns (spec, trigger, rule, recipes, base models, tool snapshot,
verb templates) as create-only Tessera `RecordWrite` payloads, plus one tick verb seed.

Building is offline and deterministic. Exporting or importing the pack executes,
enqueues, activates, admits and publishes nothing, carries no weights or credentials,
and grants no permission. An owner writes the records with the batch-record API,
verifies version-1 readback, then explicitly enqueues the tick seed in an existing queue.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest

from .catalog import BRIEF, VERBS
from .fabric import NAMESPACE, QUEUE, stored
from .tools import load_snapshot

HERE = Path(__file__).parent
PACK_ID = "continuous-tuning.tool-call-specialist"
PACK_VERSION = "1.0.0"
FORMAT = "tessera.continuous-tuning.seed-pack.v1"
STORE = "tuning.specs"
METHOD = "continuous-tuning.pack-dimensions.v1"
# Base-model descriptions are configuration: a worker passes its own with `pack --base`. The
# descriptions used for the 2026-10-29 runs are kept in archive/.
BASES: tuple[Path, ...] = ()
SPECS = {
    "trigger": ("trigger.json", "trigger"),
    "rule": ("promotion_rule.json", "promotion_rule"),
    "recipe": ("recipes/lora-sft-r16.json", "training_recipe"),
    "search": ("recipes/search-lr3.json", "search_space"),
}
HANDLERS = {
    "tick": "examples.continuous_tuning.loop.tick_verb",
    "generate_examples": "examples.continuous_tuning.verbs.generate_examples",
    "curate_dataset": "examples.continuous_tuning.verbs.curate_dataset",
    "mine_hard_examples": "examples.continuous_tuning.verbs.mine_hard_examples",
    "decide_promotion": "examples.continuous_tuning.decide.decide_promotion",
    "tune_hyperparameters": "examples.continuous_tuning.model_verbs.tune_hyperparameters",
    "train_adapter": "examples.continuous_tuning.model_verbs.train_adapter",
    "evaluate": "examples.continuous_tuning.model_verbs.evaluate",
    "serve_check": "examples.continuous_tuning.serve.serve_check",
}
BASIS = {
    "determinism": "1.0: static repository JSON; every build yields identical bytes",
    "evidence_grounding": "share of declared references that resolve to records in this pack; a noun without references is self-contained (1.0)",
    "token_efficiency": "1 - canonical value bytes / 65536, the per-record budget this pack keeps; floor 0",
    "safety_compliance": "share of pack safety checks passed: no secret-like strings, private visibility, no public effect, nothing activated",
    "verification_tier": "deterministic_gate: tests/test_continuous_tuning.py validates every record",
}
SECRETS = re.compile(r"\b(?:tsk_|tan_|hf_|ghp_|github_pat_|sk-|LLM_)[A-Za-z0-9_\-]{16,}")
SCOPE = {
    "namespace": NAMESPACE,
    "visibility_scope": "private",
    "allowed_surfaces": [],
    "target_cohorts": ["all"],
}


def safety(value: dict[str, Any]) -> float:
    text = json.dumps(value)
    checks = [
        not SECRETS.search(text),
        '"public"' not in json.dumps(value.get("execution", {})),
        value.get("activation", "none") == "none",
        value.get("visibility", "private") == "private",
    ]
    return round(sum(checks) / len(checks), 6)


def build_pack(
    namespace: str = NAMESPACE, queue: str = QUEUE, bases: Sequence[Path] = BASES
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    refs: dict[str, dict[str, Any]] = {}
    categories: dict[str, str] = {}

    def noun(name: str, category: str, role: str, payload: dict[str, Any], requires=()) -> None:
        relations = [
            {
                "relation": "requires_noun",
                "target_id": refs[target]["key"],
                "target_category": categories[target],
                "note": f"uses {target}",
            }
            for target in requires
        ]
        value = stored(
            {
                "category_id": category,
                "role": role,
                "pack": PACK_ID,
                "activation": "none",
                "dimension_method": METHOD,
                **payload,
                "relations": relations,
            }
        )
        size = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        value["dimensions"] = {
            "determinism": 1.0,
            "evidence_grounding": 1.0 if all(r["target_id"] for r in relations) else 0.0,
            "token_efficiency": round(max(0.0, 1 - size / 65536), 6),
            "safety_compliance": safety(value),
            "verification_tier": "deterministic_gate",
        }
        value["dimension_basis"] = BASIS
        value["spdx_license"] = "MIT"
        value = stored(value)
        key = f"{role}.{digest(value)}"
        records.append(
            {
                **SCOPE,
                "namespace": namespace,
                "store": STORE,
                "key": key,
                "expected_version": 0,
                "value": value,
                "tags": [f"category:{category}", f"role:{role}", "status:proposal", "data:example", "spdx:MIT"],
            }
        )  # fmt: skip
        refs[name] = {"store": STORE, "key": key, "version": 1, "value_sha256": digest(value)}
        categories[name] = category

    noun("tools", "noun.mcp_server", "tool_snapshot", load_snapshot())
    base_values = []
    for path in bases:
        base = json.loads((HERE / path).read_bytes())
        noun(f"base:{base['base_id']}", "noun.model_spec", "base_model", base)
        base_values.append(base)
    for name, (path, role) in SPECS.items():
        noun(name, "noun.code_node", role, json.loads((HERE / path).read_bytes()))
    for stage, template in VERBS.items():
        # The verb's own category sits under its own key: a noun has exactly one category.
        payload = {
            "stage": stage,
            "handler": HANDLERS[stage],
            "brief_schema": BRIEF,
            "verb_category": template["category"],
            **{key: value for key, value in template.items() if key != "category"},
        }
        noun(f"verb:{stage}", "noun.code_node", "verb_template", payload, ())
    spec = json.loads((HERE / "learner_spec.json").read_bytes())
    components = [
        "tools",
        *[f"base:{b['base_id']}" for b in base_values],
        *SPECS,
        *[f"verb:{s}" for s in VERBS],
    ]
    noun("learner_spec", "noun.code_node", "learner_spec", spec, components)
    noun(
        "index",
        "noun.skill_pack",
        "pack_index",
        {
            "title": "Continuous tuning learner for typed Tessera tool calls",
            "definition": "Index of the learner spec, its trigger, rule, recipes, base-model descriptions, tool snapshot and verb templates. A discovery label, not a service, queue or permission.",
            "seed_kinds": ["verb.decompose_dag"],
        },
        [*components, "learner_spec"],
    )  # fmt: skip
    tasks = []
    for base in base_values:
        spec_refs = {
            "tools": refs["tools"],
            "base_model": refs[f"base:{base['base_id']}"],
            "learner_spec": refs["learner_spec"],
            **{name: refs[name] for name in SPECS},
        }
        brief = {
            "stage": "tick",
            "step": "tick",
            "round": 0,
            "learner": f"{spec['learner']}:{base['base_id']}",
            "spec_refs": spec_refs,
            "params": {},
            "execution": {
                "reasoning": "authorized_worker",
                "server_fallback": False,
                "automatic_promotion": False,
                "automatic_children": 0,
            },
            "required": VERBS["tick"]["checks"],
        }
        tasks.append(
            {
                **SCOPE,
                "namespace": namespace,
                "queue": queue,
                "kind": "verb.decompose_dag",
                "effect_class": "write-internal",
                "idempotency_key": "continuous-tuning-seed:" + digest({"queue": queue, "brief": brief}),
                "tags": ["category:verb.decompose_dag", "stage:tick", "status:proposal", "data:example"],
                "brief": brief,
            }
        )  # fmt: skip
    return {
        "format": FORMAT,
        "pack_id": PACK_ID,
        "version": PACK_VERSION,
        "status": "offline_candidate",
        "example_only": True,
        "license": "MIT",
        "importer": None,
        "importer_reason": "No manifest endpoint or generic importer exists. An owner writes the records with the batch-record API, verifies version-1 readback, then explicitly enqueues a tick seed in an existing queue.",
        "does_not": ["execute", "enqueue", "activate", "admit", "publish", "carry weights", "carry credentials", "grant permissions"],
        "base_model_license": "Any base weights a worker supplies keep their own license and acceptable use policy; the pack carries none.",
        "records": records,
        "tasks": tasks,
    }  # fmt: skip


def dumps(value: Any) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def pack_files(pack: dict[str, Any]) -> dict[str, str]:
    files = {}
    for record in pack["records"]:
        files[f"records/{record['key'].split('.')[0]}.{record['key'].split('.')[-1][:16]}.json"] = (
            dumps(record)
        )
    for task in pack["tasks"]:
        files[f"tasks/tick.{task['brief']['learner'].split(':')[-1]}.json"] = dumps(task)
    return files


def manifest(pack: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    import hashlib

    counts: dict[str, int] = {}
    for record in pack["records"]:
        category = record["value"]["category_id"]
        counts[category] = counts.get(category, 0) + 1
    return {
        **{k: v for k, v in pack.items() if k not in ("records", "tasks")},
        "files": [
            {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
            for path, text in sorted(files.items())
        ],
        "records": len(pack["records"]),
        "tasks": len(pack["tasks"]),
        "categories": dict(sorted(counts.items())),
        "evaluation": {"result": None, "reason": "A pack holds specifications; measured results live in evidence/ and are not pack content."},
    }  # fmt: skip


def write_pack(directory: Path, bases: Sequence[Path] = BASES) -> dict[str, Any]:
    pack = build_pack(bases=bases)
    files = pack_files(pack)
    for stale in [*directory.glob("records/*.json"), *directory.glob("tasks/*.json")]:
        if str(stale.relative_to(directory)) not in files:
            stale.unlink()
    for path, text in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_text(text)
    value = manifest(pack, files)
    (directory / "manifest.json").write_text(dumps(value))
    return {"records": value["records"], "tasks": value["tasks"], "files": len(value["files"])}
