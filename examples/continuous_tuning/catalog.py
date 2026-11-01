"""Verb templates and the execution report every verb submits.

Each template names an existing Tessera verb category, what it reads and writes, its
typed input and output, acceptance checks and whether it may use a model. The report
uses execution-report-v1 fields. When the store accepts the optional `learning` block
(outcome, went_right, went_wrong, next_steps, types, path, unknowns, advice) or an
explicit null model, the report carries them; otherwise the same block rides in the
submitted output, so nothing is lost on a store without that support.
"""

from __future__ import annotations

from typing import Any

from examples.finetuning.data import digest

LOOP_VERSION = 1
REF = {
    "type": "object",
    "required": ["store", "key", "version", "value_sha256"],
    "properties": {
        "store": {"type": "string"},
        "key": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "value_sha256": {"type": "string"},
    },
}
BRIEF = {
    "type": "object",
    "required": ["stage", "step", "round", "learner", "spec_refs", "params"],
    "properties": {
        "stage": {"type": "string"},
        "step": {"type": "string"},
        "round": {"type": "integer", "minimum": 0},
        "learner": {"type": "string"},
        "spec_refs": {"type": "object", "additionalProperties": REF},
        "params": {"type": "object"},
    },
}


def output_schema(**nouns: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["step", "stage", "status", "nouns", "learning"],
        "properties": {
            "step": {"type": "string"},
            "stage": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "failed", "no_op"]},
            "nouns": {
                "type": "object",
                "description": "; ".join(f"{k}: {v}" for k, v in nouns.items()),
            },
            "learning": {"type": "object"},
        },
    }


VERBS: dict[str, dict[str, Any]] = {
    "tick": {
        "category": "verb.decompose_dag",
        "summary": "Evaluate the learner's trigger noun; when it fires, enqueue one round as a dependency wave.",
        "reads": ["learner_spec", "trigger", "champion pointer", "accepted example batches"],
        "writes": ["Tessera tasks for one round; nothing else"],
        "checks": [
            "Decides from stored nouns only",
            "Enqueues at most one round per tick",
            "Task ids derive from learner, round, step and tick, so a replayed tick adds nothing",
        ],
        "model": False,
        "output": output_schema(enqueued="task ids of the planned round"),
    },
    "generate_examples": {
        "category": "verb.solve_draft",
        "summary": "Draw owned examples from repository templates; labels are computed, never model-written.",
        "reads": ["tool snapshot", "templates", "batch plan"],
        "writes": ["candidate example batch nouns (noun.benchmark_suite) with JSONL blobs"],
        "checks": [
            "Every call label validates against the served tool schema",
            "Same batch name yields identical bytes",
            "No provider or model output enters a batch",
        ],
        "model": False,
        "output": output_schema(batches="candidate batch refs by name"),
    },
    "curate_dataset": {
        "category": "verb.ingest",
        "summary": "Freeze holdouts, accept new training rows and assemble training sets with zero contamination.",
        "reads": ["candidate batches", "frozen holdouts", "fresh holdouts", "accepted batches"],
        "writes": ["frozen holdout, accepted batch and training set nouns (noun.benchmark_suite)"],
        "checks": [
            "Training rows share no exact, request or template-shape fingerprint with any evaluation row",
            "Training rows come only from training families",
            "Duplicates are rejected with a recorded reason",
        ],
        "model": False,
        "output": output_schema(holdouts="frozen set ref", accepted="accepted batch refs"),
    },
    "tune_hyperparameters": {
        "category": "verb.benchmark_harness",
        "summary": "Bounded search: train one adapter per trial and select by dev score only.",
        "reads": ["training set", "search space", "recipe", "base model", "dev holdout"],
        "writes": ["adapter nouns (noun.model_spec), dev evaluation runs (noun.eval_run)"],
        "checks": ["Never reads test, fresh or sanity sets", "Trial count within max_trials"],
        "model": True,
        "output": output_schema(
            candidates="adapter and dev evaluation refs", selected="dev winner"
        ),
    },
    "train_adapter": {
        "category": "verb.solve_draft",
        "summary": "LoRA on a frozen base for one strategy's training set; save, hash and reload-check the adapter.",
        "reads": ["training set", "recipe", "base model", "champion adapter when continuing"],
        "writes": ["adapter noun (noun.model_spec, unadmitted) and private adapter blobs"],
        "checks": [
            "Frozen base parameters unchanged",
            "Adapter parameters changed",
            "Fresh-base reload gives identical greedy probe replies",
        ],
        "model": True,
        "output": output_schema(adapter="adapter ref"),
    },
    "evaluate": {
        "category": "verb.benchmark_harness",
        "summary": "Score models on pinned sets with strict metrics; dev selects, test and fresh holdouts decide.",
        "reads": ["adapters", "base model", "frozen and fresh holdouts", "sanity set"],
        "writes": ["evaluation run nouns (noun.eval_run) with per-row score blobs"],
        "checks": [
            "Set digests equal the frozen manifest",
            "Same prompts, batching and greedy decoding for every arm",
            "Replies reaching the decode allowance are flagged",
        ],
        "model": True,
        "output": output_schema(evaluations="evaluation run refs"),
    },
    "decide_promotion": {
        "category": "verb.verify_gate",
        "summary": "Apply the pre-registered rule to candidate versus champion; keeping the champion is valid.",
        "reads": ["promotion rule", "evaluation runs", "adapter and training set integrity"],
        "writes": [
            "promotion decision (noun.eval_run) and the champion pointer by compare-and-set"
        ],
        "checks": [
            "Rule digest pinned in the decision",
            "Every integrity check passes before promotion",
            "Moves only the private local pointer; no deployment, publication or admission",
        ],
        "model": False,
        "output": output_schema(decision="decision ref", pointer="champion pointer ref"),
    },
    "serve_check": {
        "category": "verb.benchmark_harness",
        "summary": "Load one base, attach several adapter nouns by name and confirm each routed adapter reproduces its solo evaluation.",
        "reads": ["adapter nouns", "their recorded dev evaluations", "base model", "dev holdout"],
        "writes": ["serving check (noun.eval_run)"],
        "checks": [
            "Exactly one base is loaded",
            "Each attached adapter's dev replies equal its recorded solo replies",
            "The base with adapters disabled equals its baseline replies",
        ],
        "model": True,
        "output": output_schema(serving_check="serving check ref"),
    },
    "mine_hard_examples": {
        "category": "verb.problemize",
        "summary": "Turn the selected candidate's dev failures into new labeled training rows plus a matched control.",
        "reads": ["dev evaluation run of the selected candidate", "dev rows' labels"],
        "writes": ["mined and control candidate batches (noun.benchmark_suite)"],
        "checks": [
            "Uses dev failures only, never test",
            "New rows come from training-family templates",
            "Lineage records which failures produced each requirement",
        ],
        "model": False,
        "output": output_schema(batches="mined and control batch refs"),
    },
}


def weights_digest(base: dict[str, Any]) -> str:
    """One digest for the base weights, whether a single file or several shards."""
    files = base.get("weights", ["model.safetensors"])
    if len(files) == 1:
        return str(base["files_sha256"][files[0]])
    return digest({name: base["files_sha256"][name] for name in files})


def clip(items: list[str], size: int = 512) -> list[str]:
    out = []
    for item in [i for i in items if i and i.strip()][:8]:
        raw = item.encode()
        out.append(item if len(raw) <= size else raw[: size - 3].decode(errors="ignore") + "...")
    return out


def learning_block(
    *,
    outcome: str,
    went_right: list[str],
    went_wrong: list[str],
    next_steps: list[str],
    types: tuple[str, str],
    method: str,
    duration: float,
    advice: list[dict[str, Any]],
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "outcome": outcome,
        "went_right": clip(went_right),
        "went_wrong": clip(went_wrong),
        "next_steps": clip(next_steps),
        "types": {"input": types[0][:256], "output": types[1][:256]},
        "path": {"method": method[:128]},
        "duration_seconds": round(max(0.0, duration), 3),
        "unknowns": [
            {
                "field": "cost",
                "reason": "Owned local GPU; electricity and hardware time are not metered.",
            }
        ],
    }
    if advice:
        block["advice"] = advice[:8]
    return {key: value for key, value in block.items() if value != []}


def store_supports() -> dict[str, bool]:
    from tessera.execution_matching import ROOT_FIELDS, normalize_report

    try:
        normalize_report({"version": 1, "model": None})
        null_model = True
    except (TypeError, ValueError):
        null_model = False
    return {"learning": "learning" in ROOT_FIELDS, "null_model": null_model}


def execution_report(
    stage: str,
    *,
    problem: str,
    given: str,
    solution: str,
    learning: dict[str, Any],
    refs: list[dict[str, Any]],
    model: dict[str, Any] | None,
    usage: dict[str, Any],
    context: Any,
    environment: Any,
    toolchain: Any,
) -> dict[str, Any]:
    supports = store_supports()
    blocks = [
        {"kind": "solution", "text": "Went right: " + "; ".join(learning.get("went_right", []))},
        {"kind": "context", "text": "Next steps: " + "; ".join(learning.get("next_steps", []))},
        {
            "kind": "environment",
            "text": f"Worker-measured duration {learning['duration_seconds']} s",
        },
    ]
    if learning.get("went_wrong"):
        blocks.insert(
            1, {"kind": "error", "text": "Went wrong: " + "; ".join(learning["went_wrong"])}
        )
    report: dict[str, Any] = {
        "version": 1,
        "harness": {
            "name": "tessera-continuous-tuning",
            "version": str(LOOP_VERSION),
            "verb": stage,
        },
        "summaries": {
            "problem": problem[:4000],
            "input": given[:4000],
            "solution": solution[:4000],
        },
        "contract": {
            "input_schema": BRIEF,
            "output_schema": VERBS[stage]["output"],
            "context_hash": digest(context),
            "environment_hash": digest(environment),
            "toolchain_hash": digest(toolchain),
        },
        "blocks": [{"kind": b["kind"], "text": b["text"][:4000]} for b in blocks],
        "artifact_refs": [
            {"store": ref["store"], "key": ref["key"], "version": ref["version"]} for ref in refs
        ][:32],
        "usage": usage,
    }
    if model is not None:
        report["model"] = model
    elif supports["null_model"]:
        report["model"] = None
    else:
        report["model"] = {"used": False, "reason": "deterministic verb; no model was invoked"}
    if supports["learning"]:
        report["learning"] = learning
    return report


def no_model_usage() -> dict[str, Any]:
    return {
        "source": "not_applicable",
        "input_tokens": None,
        "output_tokens": None,
        "reason": "Deterministic verb; no model tokens were used.",
    }
