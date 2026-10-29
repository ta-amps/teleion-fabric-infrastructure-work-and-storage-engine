"""The promotion verb: apply the pre-registered rule and move the champion pointer.

Keeping the champion is a valid outcome. The decision noun pins the rule, the
candidate, the evaluations it compared and the integrity evidence; the pointer is the
learner's one mutable noun and moves only by compare-and-set.
"""

from __future__ import annotations

from typing import Any

from .dataset import read_jsonl
from .promotion import decide
from .verbs import Beat, Context, Result, pointer, tags


def decide_promotion(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    mode = brief["params"]["mode"]
    learner = brief["learner"]
    state, version = pointer(ctx, brief)
    now = ctx.now()
    if mode == "baseline":
        evals = deps["baseline_eval"]["nouns"]["evaluations"]["base"]
        value = {
            "category_id": "noun.eval_run",
            "role": "promotion_decision",
            "learner": learner,
            "round": brief["round"],
            "outcome": "establish_baseline",
            "rule": ctx.spec_refs["rule"],
            "evaluations": evals,
        }
        decision = ctx.fabric.put("evals", "decision", value, tags(brief))
        new_state = {
            "learner": learner,
            "champion": {"kind": "base", "base": ctx.spec_refs["base_model"], "label": "base"},
            "champion_evals": evals,
            "base_evals": evals,
            "champion_training_batches": [],
            "holdouts": deps["freeze"]["nouns"]["holdouts"],
            "fresh_holdouts": [],
            "round": brief["round"],
            "tuned": None,
            "last_decision": decision,
            "last_evaluated_at": now,
            "decisions": [decision],
        }
        ref = ctx.fabric.set_pointer(learner, new_state, version)
        return _decision_result(value, decision, ref, "Recorded the unchanged base as the champion")
    if state is None:
        raise ValueError("no champion pointer; run the baseline round first")
    holdout = deps["holdout_eval"]["summary"]
    if mode == "monitor":
        new_state = {
            **state,
            "last_evaluated_at": now,
            "fresh_holdouts": [*state["fresh_holdouts"], holdout["fresh"]],
        }
        ref = ctx.fabric.set_pointer(learner, new_state, version)
        value = {
            "category_id": "noun.eval_run",
            "role": "monitor_record",
            "learner": learner,
            "round": brief["round"],
            "evaluations": holdout["evaluations"],
            "drift": holdout.get("drift"),
        }
        decision = ctx.fabric.put("evals", "monitor", value, tags(brief))
        return _decision_result(
            value,
            decision,
            ref,
            "Recorded a fresh evaluation of the champion; no promotion considered",
        )
    selected = deps[brief["params"]["selection_step"]]["summary"]["selected"]
    cand = _loaded(ctx, holdout["evaluations"]["candidate"])
    champ = _loaded(
        ctx, {**state["champion_evals"], "fresh": holdout["evaluations"]["champion"]["fresh"]}
    )
    base = _loaded(ctx, state["base_evals"])
    adapter = ctx.fabric.read(selected["adapter"])
    training_set = ctx.fabric.read(adapter["training_set"])
    frozen = ctx.fabric.read(state["holdouts"])["parts"]
    integrity = {
        "no_contamination": not any(training_set["contamination"].values()),
        "frozen_base_unchanged": adapter["training"]["frozen_parameters_unchanged"],
        "adapter_reload_equivalent": adapter["training"]["reload"]["equivalent"],
        "holdout_digests_match": all(
            ctx.fabric.read(holdout["evaluations"]["candidate"][name])["set"]["jsonl_sha256"]
            == frozen[name]["jsonl"]["sha256"]
            for name in ("test", "sanity")
        ),
    }
    verdict = decide(ctx.specs["rule"], cand, champ, base, integrity)
    value = {
        "category_id": "noun.eval_run",
        "role": "promotion_decision",
        "learner": learner,
        "round": brief["round"],
        "candidate": selected,
        "champion_before": state["champion"],
        "rule": ctx.spec_refs["rule"],
        "verdict": verdict,
        "integrity": integrity,
        "evaluations": {
            "candidate": holdout["evaluations"]["candidate"],
            "champion": {
                **state["champion_evals"],
                "fresh": holdout["evaluations"]["champion"]["fresh"],
            },
            "base": {**state["base_evals"], "fresh": holdout["evaluations"]["base"]["fresh"]},
        },
        "ablation": holdout.get("ablation", {}),
    }
    decision = ctx.fabric.put(
        "evals", "decision", value, tags(brief, f"outcome:{verdict['outcome']}")
    )
    promoted = verdict["outcome"] == "promote_candidate"
    new_state = {
        **state,
        "round": brief["round"],
        "last_decision": decision,
        "last_evaluated_at": now,
        "decisions": [*state["decisions"], decision],
        "fresh_holdouts": [*state["fresh_holdouts"], holdout["fresh"]],
        "tuned": state.get("tuned") or selected.get("tuned"),
        "last_candidate_training_batches": training_set["sources"],
    }
    if promoted:
        new_state.update(
            champion={
                "kind": "adapter",
                "adapter": selected["adapter"],
                "label": selected["label"],
            },
            champion_evals={k: holdout["evaluations"]["candidate"][k] for k in ("test", "sanity")},
            champion_training_batches=[
                s for s in training_set["sources"] if not s.startswith("replay:")
            ],
        )
    ref = ctx.fabric.set_pointer(learner, new_state, version)
    what = "Promoted the candidate" if promoted else "Kept the champion"
    return _decision_result(value, decision, ref, f"{what} under the pre-registered rule")


def _loaded(ctx: Context, evals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for name, ref in evals.items():
        run = ctx.fabric.read(ref)
        scores = read_jsonl(ctx.fabric.get_bytes(run["scores"]))
        out[name] = {
            "aggregate": run["aggregate"],
            "ids": [s["id"] for s in scores],
            "exact": [bool(s.get("exact", s.get("correct"))) for s in scores],
        }
    return out


def _decision_result(value, decision, pointer_ref, solution) -> Result:
    verdict = value.get("verdict", {})
    failed = [c["check"] for c in verdict.get("checks", []) if c["passed"] is False]
    return Result(
        nouns={"decision": decision, "pointer": pointer_ref},
        summary={
            "outcome": verdict.get("outcome", value.get("outcome", value["role"])),
            "paired_test": verdict.get("paired_test"),
        },
        problem="Decide whether the candidate replaces the champion under the pre-registered rule",
        given="Candidate, champion and base evaluation runs plus integrity evidence",
        solution=solution,
        went_right=[c["check"] for c in verdict.get("checks", []) if c["passed"]][:8] or [solution],
        went_wrong=failed,
        next_steps=[
            "Mine the selected candidate's dev failures and accept new rows for the next tick"
        ],
        types=(
            "evaluation runs and integrity evidence",
            "decision noun and champion pointer version",
        ),
        refs=[decision, pointer_ref],
    )
