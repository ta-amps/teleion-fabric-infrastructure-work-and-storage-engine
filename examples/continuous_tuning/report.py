"""Read-only learning curve from the stored nouns and tick logs: no model, no writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import weights_digest
from .fabric import Fabric

HERE = Path(__file__).parent
KEYS = (
    "n",
    "exact_match_strict",
    "exact_match_lenient",
    "json_valid_rate",
    "schema_valid_rate",
    "type_correct_rate",
    "stringified_rate",
    "refusal_accuracy",
    "refusal_recall",
    "false_refusal_rate",
    "accuracy",
    "tool_json_rate",
    "reached_allowance",
    "failure_counts",
)


def learning_curve(root: Path, base_noun: Path) -> dict[str, Any]:
    base = json.loads(base_noun.read_bytes())
    spec = json.loads((HERE / "learner_spec.json").read_bytes())
    learner = f"{spec['learner']}:{base['base_id']}"
    fabric = Fabric(root)
    try:
        with fabric.scope():
            state, version = fabric.pointer(learner)
            if state is None:
                return {"learner": learner, "rounds": []}

            def scored(ref: dict[str, Any] | None) -> dict[str, Any] | None:
                if ref is None:
                    return None
                run = fabric.read(ref)
                out = {k: run["aggregate"][k] for k in KEYS if k in run["aggregate"]}
                out.update(
                    seconds=run["seconds"],
                    tokens=run["tokens"],
                    peak_allocated_bytes=run["memory"]["peak_allocated_bytes"],
                    set_sha256=run["set"]["jsonl_sha256"],
                )
                return out

            rounds = []
            for ref in state["decisions"]:
                decision = fabric.read(ref)
                if decision.get("outcome") == "establish_baseline":
                    evals = decision["evaluations"]
                    rounds.append(
                        {
                            "round": decision["round"],
                            "model": "unchanged base",
                            "training_rows": 0,
                            "dev": scored(evals["dev"]),
                            "test": scored(evals["test"]),
                            "sanity": scored(evals["sanity"]),
                            "outcome": "establish_baseline",
                        }
                    )
                    continue
                candidate = decision["candidate"]
                adapter = fabric.read(candidate["adapter"])
                training_set = fabric.read(adapter["training_set"])
                evals = decision["evaluations"]
                training = adapter["training"]
                rounds.append(
                    {
                        "round": decision["round"],
                        "candidate": candidate["label"],
                        "strategy": candidate["strategy"],
                        "training_rows": training_set["rows"],
                        "training_sources": training_set["sources"],
                        "contamination": training_set["contamination"],
                        "dev_exact": candidate["dev_exact"],
                        "test": scored(evals["candidate"]["test"]),
                        "fresh": scored(evals["candidate"]["fresh"]),
                        "sanity": scored(evals["candidate"]["sanity"]),
                        "champion_test": scored(evals["champion"]["test"]),
                        "champion_fresh": scored(evals["champion"]["fresh"]),
                        "base_fresh": scored(evals["base"]["fresh"]),
                        "paired_test": decision["verdict"]["paired_test"],
                        "checks": decision["verdict"]["checks"],
                        "integrity": decision.get("integrity"),
                        "outcome": decision["verdict"]["outcome"],
                        "ablation": {k: scored(v) for k, v in decision.get("ablation", {}).items()},
                        "training": {
                            "optimizer_steps": training["optimizer_steps"],
                            "training_seconds": training["training_seconds"],
                            "peak_allocated_bytes": training["memory"]["peak_allocated_bytes"],
                            "peak_reserved_bytes": training["memory"]["peak_reserved_bytes"],
                            "trainable_parameters": training["trainable_parameters"],
                            "tokens": training["tokens"],
                            "init": training["init"],
                            "reload": training["reload"],
                            "learning_rate": adapter["effective_recipe"]["learning_rate"],
                            "first_loss": training["losses"][0] if training["losses"] else None,
                            "last_loss": training["losses"][-1] if training["losses"] else None,
                        },
                    }
                )
            ticks = []
            for path in sorted((root / "ticks").glob("tick-*.json")):
                log = json.loads(path.read_text())
                ticks.append(
                    {
                        "tick": log["tick"],
                        "verbs": len(log["verbs"]),
                        "ok": sum(1 for v in log["verbs"] if v["ok"]),
                        "stages": [f"{v['stage']}:{v['status']}" for v in log["verbs"]],
                        "seconds": round(sum(v["seconds"] for v in log["verbs"]), 3),
                        "executions_captured": sum(len(v.get("run_ids", [])) for v in log["verbs"]),
                        "privacy": log["privacy"],
                    }
                )
            return {
                "learner": learner,
                "base_model": {
                    "model_id": base["model_id"],
                    "weights_sha256": weights_digest(base),
                },
                "pointer_version": version,
                "champion": state["champion"]["label"],
                "rounds": rounds,
                "ticks": ticks,
            }
    finally:
        fabric.close()
