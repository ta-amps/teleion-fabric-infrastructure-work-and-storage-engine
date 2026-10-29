"""Path statistics learner, strategy wilson-grouping v1: what worked, per path.

A deterministic run groups permitted execution observations by path dimensions
(category, template, phrasing, shim, method, harness, model, queue, tagset) and
writes one versioned scorecard noun. Ordinary reasoning stays with whoever reads it.
"""

from __future__ import annotations

from typing import Any

from tessera.fingerprints import PROFILE, phrasing_hash
from tessera.learners.contract import value_digest
from tessera.learners.runtime import Api, LearnerRun, page_observations, write_outputs
from tessera.learners.stats import outcome_statistics
from tessera.receipts import canonical_json

LEARNER = "path_statistics"
STRATEGY = "wilson-grouping"
VERSION = 1
CONTRACT = "tessera.path-statistics.v1"
GROUP_DIMENSIONS = (
    "category",
    "template",
    "phrasing",
    "shim",
    "method",
    "harness",
    "model",
    "queue",
    "tagset",
)
DEFAULT_GROUP_BY = ("category", "template", "phrasing", "shim")
SMALL_SAMPLE = 5
LIMITATIONS = (
    "Observational: paths saw different inputs, workers and times; differences are associations.",
    "Worker-reported fields (outcome, tokens, cost, shim, model, went_wrong) are unverified claims.",
    "Only executions readable under the aggregating worker's grants are included.",
    "Accepted means the submission was recorded with ok=true; it is not independent admission.",
)


def dimension(observation: dict[str, Any], name: str) -> Any:
    report = observation["report"]
    path = (report.get("learning") or {}).get("path") or {}
    brief = observation.get("brief")
    if name == "template":
        return brief.get("template") if brief else None
    if name == "phrasing":
        return phrasing_hash(brief) if brief else None
    if name in ("shim", "method"):
        return path.get(name)
    if name in ("harness", "model"):
        return report.get(name)
    if name == "tagset":
        return ",".join(sorted(observation.get("tags", []))) or None
    return observation.get(name)


def runs_digest(observations: list[dict[str, Any]]) -> str:
    """Order-independent identity of the exact runs an aggregate used."""
    pins = sorted([o["run_id"], o["receipt_hash"], o["report_sha256"]] for o in observations)
    return value_digest(pins)


def aggregate(
    observations: list[dict[str, Any]],
    *,
    group_by: tuple[str, ...] | list[str] = DEFAULT_GROUP_BY,
    top: int = 5,
) -> dict[str, Any]:
    """Deterministic for the same runs, independent of input order."""
    dims = tuple(group_by)
    if not dims or len(set(dims)) != len(dims) or set(dims) - set(GROUP_DIMENSIONS):
        raise ValueError(
            "group_by must name distinct dimensions from " + ", ".join(GROUP_DIMENSIONS)
        )
    if type(top) is not int or not 1 <= top <= 20:
        raise ValueError("top must be an integer from 1 to 20")
    unique = {item["run_id"]: item for item in observations}
    groups: dict[str, list[dict[str, Any]]] = {}
    keys: dict[str, dict[str, Any]] = {}
    for item in unique.values():
        key = {name: dimension(item, name) for name in dims}
        identity = canonical_json(key)
        keys[identity] = key
        groups.setdefault(identity, []).append(item)
    rendered: list[dict[str, Any]] = []
    for identity, members in groups.items():
        statistics = outcome_statistics(members, top)
        rendered.append(
            {
                "path": keys[identity],
                "statistics": statistics,
                "sample": "small" if statistics["attempts"] < SMALL_SAMPLE else "adequate",
                "example_run_ids": sorted(member["run_id"] for member in members)[:3],
            }
        )
    rendered.sort(key=lambda g: (-g["statistics"]["attempts"], canonical_json(g["path"])))
    every = list(unique.values())
    return {
        "group_by": list(dims),
        "runs": len(unique),
        "runs_sha256": runs_digest(every),
        "overall": outcome_statistics(every, top),
        "groups": rendered,
        "evidence_class": "observational",
        "limitations": list(LIMITATIONS),
    }


def stats_key(queue: str, kind: str | None = None) -> str:
    return f"{LEARNER}:{STRATEGY}:v{VERSION}:{queue}" + (f":{kind}" if kind else "")


def run(api: Api, run_state: LearnerRun) -> dict[str, Any]:
    """Aggregate one bounded window of observations into a new scorecard version."""
    options = run_state.options
    group_by = tuple(options.get("group_by", DEFAULT_GROUP_BY))
    top = options.get("top", 5)
    after = options.get("after")
    observations, cursor = page_observations(api, run_state, after=after)
    if not observations:
        return {"outcome": "no_change", "observations_read": 0, "cursor_after": cursor}
    value = {
        "contract": CONTRACT,
        "category_id": "noun.eval_run",
        "learner": LEARNER,
        "strategy": STRATEGY,
        "version": VERSION,
        "fingerprint_profile": PROFILE,
        "spec": run_state.spec.model_dump(),
        "scope": {
            "namespace": run_state.namespace,
            "queue": run_state.source_queue,
            "kind": run_state.source_kind,
        },
        "window": {"after": after, "until": cursor, "max_observations": run_state.max_observations},
        **aggregate(observations, group_by=group_by, top=top),
    }
    written = write_outputs(
        api,
        run_state,
        [
            run_state.output(
                stats_key(run_state.source_queue, run_state.source_kind),
                value,
                expected_version=run_state.state_version or None,
            )
        ],
    )
    return {
        "outcome": "updated",
        "observations_read": len(observations),
        "cursor_after": cursor,
        "groups": len(value["groups"]),
        "outputs": written,
    }
