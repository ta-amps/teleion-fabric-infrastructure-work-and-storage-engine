"""Hard-example mining: turn dev failures into new, labeled training rows.

A failure becomes a feature requirement (for example an object-typed argument written
as literal JSON, or a missing-value refusal). New rows are drawn from training-family
templates only, so no dev text, tool or template enters training and dev keeps
measuring generalization. A matched random control batch of the same size lets the
next round test whether targeting helped more than the same amount of new data.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .task import generate, generate_targeted

MINING_VERSION = 1


def requirement(failure: dict[str, Any], row: dict[str, Any]) -> tuple[str, ...]:
    category = failure["category"]
    if category in ("missed_refusal", "wrong_refusal_reason"):
        return (f"refusal:{row['target']['refusal']}",)
    if category in ("invalid_json", "wrapped_json", "wrong_shape") and row["kind"] == "refusal":
        return (f"refusal:{row['target']['refusal']}",)
    if category == "stringified_value":
        typed = f"arg_type:{failure['type']}"
        if failure["type"] in ("object", "array"):
            return ("kind:call", typed, "literal_json")
        return ("kind:call", typed)
    if category in ("type_error", "missing_argument", "wrong_value"):
        return ("kind:call", f"arg_type:{failure['type']}")
    if category == "extra_argument":
        return ("kind:call", "optionals:none")
    if category == "schema_violation":
        return ("kind:call", "optionals:some")
    if category in ("wrong_tool", "unoffered_tool"):
        return ("kind:call", "confusable")
    return ("kind:call",)


def plan(
    scores: list[dict[str, Any]], rows: dict[str, dict[str, Any]], budget: int
) -> dict[str, Any]:
    """Allocate the mining budget across failure requirements, in proportion to their counts."""
    sources: dict[tuple[str, ...], list[str]] = {}
    for item in scores:
        for failure in item["failures"]:
            key = requirement(failure, rows[item["id"]])
            sources.setdefault(key, []).append(item["id"])
    counts = Counter({key: len(ids) for key, ids in sources.items()})
    total = sum(counts.values())
    if not total or budget <= 0:
        return {"requirements": [], "allocation": [], "failures": 0}
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    allocation = {key: max(2, round(budget * count / total)) for key, count in ranked}
    while sum(allocation.values()) > budget:
        largest = max(allocation, key=lambda key: (allocation[key], key))
        if allocation[largest] <= 1:
            break
        allocation[largest] -= 1
    requirements = [list(key) for key, _ in ranked for _ in range(allocation[key])][:budget]
    return {
        "requirements": requirements,
        "allocation": [
            {"requires": list(key), "failures": counts[key], "rows": allocation[key]}
            for key, _ in ranked
        ],
        "failures": total,
        "source_ids": {"|".join(key): sorted(set(ids)) for key, ids in sources.items()},
    }


def mine(
    scores: list[dict[str, Any]],
    rows: dict[str, dict[str, Any]],
    budget: int,
    round_id: int,
    tools: dict[str, Any],
) -> dict[str, Any]:
    plan_ = plan([s for s in scores if not s["exact"]], rows, budget)
    mined = generate_targeted(plan_["requirements"], f"mined-r{round_id}", "mined", tools)
    for row, required in zip(mined, plan_["requirements"], strict=True):
        row["lineage"] = {"mined_from_split": "dev", "requires": required, "round": round_id}
    control = (
        generate("train", len(mined), f"control-r{round_id}", "control", tools) if mined else []
    )
    return {
        "version": MINING_VERSION,
        "mined": mined,
        "control": control,
        "allocation": plan_["allocation"],
        "failures_seen": plan_["failures"],
        "source_ids": plan_.get("source_ids", {}),
    }
