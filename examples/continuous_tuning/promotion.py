"""The pre-registered promotion rule, applied to candidate and champion evaluations.

`promotion_rule.json` was committed before round 1; its digest is pinned in every
decision. Keeping the champion is a valid outcome. A promotion moves only the private
local champion pointer that the next round compares against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest

from .task import Draw

RULE_PATH = Path(__file__).with_name("promotion_rule.json")
RULE_FORMAT = "tessera.continuous-tuning.promotion-rule.v1"


def load_rule(path: Path = RULE_PATH) -> dict[str, Any]:
    rule = json.loads(path.read_bytes())
    if rule.get("format") != RULE_FORMAT or rule.get("version") != 1:
        raise ValueError("unsupported promotion rule")
    return rule


def paired_bootstrap(
    candidate: list[bool], champion: list[bool], resamples: int, seed: str, confidence: float
) -> dict[str, float]:
    """Percentile interval of the paired mean difference, from SHA-256 seeded resamples."""
    if len(candidate) != len(champion) or not candidate:
        raise ValueError("paired evaluations must cover the same nonempty rows")
    n = len(candidate)
    differences = [int(a) - int(b) for a, b in zip(candidate, champion, strict=True)]
    draw = Draw("paired-bootstrap", seed, n)
    means = sorted(sum(differences[draw.below(n)] for _ in range(n)) / n for _ in range(resamples))
    tail = (1 - confidence) / 2
    return {
        "delta": round(sum(differences) / n, 6),
        "lower": round(means[int(tail * resamples)], 6),
        "upper": round(means[min(resamples - 1, int((1 - tail) * resamples))], 6),
        "improved_rows": sum(1 for d in differences if d > 0),
        "regressed_rows": sum(1 for d in differences if d < 0),
    }


def metric(evaluation: dict[str, Any], split: str, name: str) -> float | None:
    part = evaluation.get(split)
    return None if part is None else part["aggregate"].get(name)


def decide(
    rule: dict[str, Any],
    candidate: dict[str, Any],
    champion: dict[str, Any],
    base: dict[str, Any],
    integrity: dict[str, bool],
) -> dict[str, Any]:
    """candidate/champion/base: {split: {"aggregate": ..., "exact": [bool per frozen row]}}."""
    promotion = rule["promotion"]
    checks = []
    primary = promotion["primary_metric"]
    boot = promotion["paired_bootstrap"]
    if candidate["test"].get("ids") != champion["test"].get("ids"):
        raise ValueError("candidate and champion were not scored on the same frozen rows")
    paired = paired_bootstrap(
        candidate["test"]["exact"],
        champion["test"]["exact"],
        boot["resamples"],
        boot["seed"],
        boot["confidence"],
    )
    checks.append(
        {
            "check": f"test {primary} improves by at least {promotion['min_delta']}",
            "passed": paired["delta"] >= promotion["min_delta"],
            "observed": paired["delta"],
        }
    )
    checks.append(
        {
            "check": "paired bootstrap lower bound above zero",
            "passed": paired["lower"] > boot["require_lower_bound_above"],
            "observed": paired["lower"],
        }
    )
    for guard in promotion["no_regression"]:
        reference = base if guard.get("against") == "base" else champion
        if guard["split"] not in candidate or guard["split"] not in reference:
            # Only a guard marked optional (the per-round fresh holdout) may be absent.
            passed = None if guard.get("optional") else False
            checks.append({"check": guard["name"], "passed": passed, "observed": "not evaluated"})
            continue
        new = metric(candidate, guard["split"], guard["metric"])
        old = metric(reference, guard["split"], guard["metric"])
        passed = new is not None and old is not None and new >= old - guard["max_drop"]
        checks.append({"check": guard["name"], "passed": passed, "observed": [old, new]})
    for name in promotion["integrity"]:
        checks.append({"check": f"integrity: {name}", "passed": bool(integrity.get(name))})
    required = [check for check in checks if check["passed"] is not None]
    promote = all(check["passed"] for check in required)
    return {
        "rule_id": rule["rule_id"],
        "rule_sha256": digest(rule),
        "outcome": "promote_candidate" if promote else "keep_champion",
        "paired_test": paired,
        "checks": checks,
        "effects": promotion["effects"],
    }
