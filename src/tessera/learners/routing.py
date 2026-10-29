"""Routing-weights learner, strategy beta-bandit v1: advisory selection weights per family.

For each category (task family) and one alternative dimension (shim, method, harness,
model, template or phrasing), a deterministic run turns accepted and failed runs into
Beta(1 + accepted, 1 + failed) posteriors, holds weight at the prior until an
alternative has min_evidence attempts, keeps an exploration floor for every known
alternative and writes one versioned weights noun. Weights advise a worker's choice;
they never grant permission or override an explicit caller or provider choice. A
consumer records the weights version it used in report.learning.advice.
"""

from __future__ import annotations

from typing import Any

from tessera.learners.path_statistics import dimension
from tessera.learners.runtime import Api, LearnerRun, page_observations, write_outputs

LEARNER = "routing_weights"
STRATEGY = "beta-bandit"
VERSION = 1
CONTRACT = "tessera.routing-weights.v1"
DIMENSIONS = ("shim", "method", "harness", "model", "template", "phrasing")
DEFAULT_EXPLORATION = 0.1
DEFAULT_MIN_EVIDENCE = 20


def weights(
    observations: list[dict[str, Any]],
    *,
    alternative: str,
    exploration: float = DEFAULT_EXPLORATION,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
) -> list[dict[str, Any]]:
    """Per-category weights; deterministic and independent of observation order."""
    if alternative not in DIMENSIONS:
        raise ValueError("alternative must be one of " + ", ".join(DIMENSIONS))
    if type(exploration) not in (int, float) or not 0 < exploration < 1:
        raise ValueError("exploration must be a number strictly between 0 and 1")
    if type(min_evidence) is not int or not 1 <= min_evidence <= 10_000:
        raise ValueError("min_evidence must be an integer from 1 to 10000")
    unique = {item["run_id"]: item for item in observations}
    tallies: dict[str, dict[str, list[int]]] = {}
    for item in unique.values():
        value = dimension(item, alternative)
        if value is None:
            continue
        counts = tallies.setdefault(item["category"], {}).setdefault(str(value), [0, 0])
        counts[0 if item["ok"] else 1] += 1
    families = []
    for category in sorted(tallies):
        arms = tallies[category]
        rows: list[dict[str, Any]] = []
        for value in sorted(arms):
            accepted, failed = arms[value]
            attempts = accepted + failed
            mean = (1 + accepted) / (2 + attempts)
            rows.append(
                {
                    "value": value,
                    "attempts": attempts,
                    "accepted": accepted,
                    "posterior": {"alpha": 1 + accepted, "beta": 1 + failed, "mean": mean},
                    "evidence": "sufficient" if attempts >= min_evidence else "insufficient",
                    # Below the evidence threshold an arm keeps the uniform prior mean.
                    "score": mean if attempts >= min_evidence else 0.5,
                }
            )
        total = sum(row["score"] for row in rows)
        floor = exploration / len(rows)
        for row in rows:
            row["weight"] = floor + (1 - exploration) * row["score"] / total
        families.append(
            {
                "category": category,
                "alternative": alternative,
                "baseline": "uniform",
                "alternatives": rows,
                "ready": any(row["evidence"] == "sufficient" for row in rows),
            }
        )
    return families


def weights_key(queue: str, alternative: str, kind: str | None = None) -> str:
    scope = f"{queue}:{kind}" if kind else queue
    return f"{LEARNER}:{STRATEGY}:v{VERSION}:{scope}:{alternative}"


def run(api: Api, run_state: LearnerRun) -> dict[str, Any]:
    """Recompute weights over one bounded window and write a new weights version."""
    options = run_state.options
    alternative = options.get("alternative", "shim")
    exploration = options.get("exploration", DEFAULT_EXPLORATION)
    min_evidence = options.get("min_evidence", DEFAULT_MIN_EVIDENCE)
    observations, cursor = page_observations(api, run_state, after=options.get("after"))
    if not observations:
        return {"outcome": "no_change", "observations_read": 0, "cursor_after": cursor}
    families = weights(
        observations, alternative=alternative, exploration=exploration, min_evidence=min_evidence
    )
    value = {
        "contract": CONTRACT,
        "category_id": "noun.reflex_procedure",
        "learner": LEARNER,
        "strategy": STRATEGY,
        "version": VERSION,
        "spec": run_state.spec.model_dump(),
        "scope": {
            "namespace": run_state.namespace,
            "queue": run_state.source_queue,
            "kind": run_state.source_kind,
        },
        "rule": {
            "posterior": "Beta(1 + accepted, 1 + failed)",
            "exploration": exploration,
            "min_evidence": min_evidence,
            "weight": "exploration / arms + (1 - exploration) * score / sum(scores)",
        },
        "window": {"after": options.get("after"), "until": cursor},
        "families": families,
        "advice_only": True,
        "limitations": [
            "Weights advise a worker's choice; they never grant permission or override an "
            "explicit caller or provider choice.",
            "Observational: arms saw different inputs; prefer randomized assignment for proof.",
        ],
    }
    written = write_outputs(
        api,
        run_state,
        [
            run_state.output(
                weights_key(run_state.source_queue, alternative, run_state.source_kind),
                value,
                expected_version=run_state.state_version or None,
            )
        ],
    )
    return {
        "outcome": "updated",
        "observations_read": len(observations),
        "cursor_after": cursor,
        "families": len(families),
        "outputs": written,
    }
