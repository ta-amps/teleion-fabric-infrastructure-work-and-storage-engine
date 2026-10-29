"""Memory learner, strategy minhash-lsh v1: asynchronous fingerprint index plus recall.

The index run is a deterministic verb. It pages permitted execution observations,
fingerprints them with tessera.fingerprints and writes immutable segment nouns plus
one versioned head noun. Submission stays unchanged: the hot path writes no index
rows. Recall runs in the consumer: it reads the head and segments it may read,
ranks entries locally and reads candidates back through get_execution.
"""

from __future__ import annotations

from typing import Any

from tessera.errors import NotFound
from tessera.fingerprints import PROFILE, brief_view, problem_fingerprints, similarity
from tessera.learners.contract import NounRef, value_digest
from tessera.learners.runtime import Api, LearnerRun, page_observations, write_outputs
from tessera.learners.stats import outcome_statistics, reason_text
from tessera.models import RecordWrite
from tessera.observations import USE_POLICY

LEARNER = "memory"
STRATEGY = "minhash-lsh"
VERSION = 1
SEGMENT_CONTRACT = "tessera.memory-index-segment.v1"
HEAD_CONTRACT = "tessera.memory-index-head.v1"
SEGMENT_LIMIT = 128
SIMILARITY_MIN = 0.3
SCAN_LIMIT = 20_000
EMPTY_VIEW: dict[str, Any] = {"template": None, "text": {}, "shape": {}, "type": "unavailable"}


def scope_key(queue: str, kind: str | None = None) -> str:
    """One index per (queue, optional kind) scope, so differently scoped runs never collide."""
    return f"{LEARNER}:{STRATEGY}:v{VERSION}:{queue}" + (f":{kind}" if kind else "")


def head_key(queue: str, kind: str | None = None) -> str:
    return scope_key(queue, kind) + ":head"


def segment_key(queue: str, sequence: int, kind: str | None = None) -> str:
    return scope_key(queue, kind) + f":seg:{sequence:06d}"


def observation_fingerprints(observation: dict[str, Any]) -> dict[str, Any]:
    report = {
        "summaries": {"problem": observation["report"].get("problem") or ""},
        "learning": observation["report"].get("learning") or {},
    }
    view = observation.get("brief") or EMPTY_VIEW
    return problem_fingerprints(
        observation["category"], view, report, observation["input"]["digest"]
    )


def entry(observation: dict[str, Any]) -> dict[str, Any]:
    """Compact, observation-shaped index entry: enough to rank and summarize outcomes."""
    found = observation_fingerprints(observation)
    learning = observation["report"].get("learning") or {}
    return {
        "seq": observation["seq"],
        "run_id": observation["run_id"],
        "task_id": observation["task_id"],
        "fencing_token": observation["fencing_token"],
        "receipt_hash": observation["receipt_hash"],
        "created_at": observation["created_at"],
        "category": observation["category"],
        "template": found["template"],
        "phrasing": found["phrasing"],
        "exact": found["exact"],
        "minhash": found["minhash"],
        "bands": found["bands"],
        "ok": observation["ok"],
        "error": None if observation["ok"] else reason_text(observation.get("error") or ""),
        "observed": dict(observation["observed"]),
        "report": {
            "total_tokens": observation["report"].get("total_tokens"),
            "harness": observation["report"].get("harness"),
            "model": observation["report"].get("model"),
            "learning": {
                **{
                    name: learning[name]
                    for name in ("outcome", "duration_seconds", "cost", "path")
                    if name in learning
                },
                "went_wrong": [reason_text(text) for text in learning.get("went_wrong", [])[:3]],
            },
        },
    }


def _scope(run: LearnerRun) -> dict[str, Any]:
    return {"namespace": run.namespace, "queue": run.source_queue, "kind": run.source_kind}


def run(api: Api, run_state: LearnerRun) -> dict[str, Any]:
    """Index new observations after the head cursor; a no_change result writes nothing."""
    head: dict[str, Any] | None = None
    if run_state.state_version:
        record = api.get_record(
            run_state.output_store,
            head_key(run_state.source_queue, run_state.source_kind),
            run_state.state_version,
            namespace=run_state.namespace,
        )
        head = record.value
        if not isinstance(head, dict) or head.get("contract") != HEAD_CONTRACT:
            raise ValueError("pinned state is not a memory index head")
    observations, cursor = page_observations(api, run_state, after=head["cursor"] if head else None)
    if not observations:
        return {"outcome": "no_change", "observations_read": 0, "cursor_after": cursor}
    entries = [entry(item) for item in observations]
    segments = list(head["segments"]) if head else []
    writes: list[RecordWrite] = []
    for offset in range(0, len(entries), SEGMENT_LIMIT):
        sequence = len(segments) + 1
        chunk = entries[offset : offset + SEGMENT_LIMIT]
        value: dict[str, Any] = {
            "contract": SEGMENT_CONTRACT,
            "learner": LEARNER,
            "strategy": STRATEGY,
            "version": VERSION,
            "fingerprint_profile": PROFILE,
            "scope": _scope(run_state),
            "sequence": sequence,
            "entries": chunk,
            "use_policy": USE_POLICY,
        }
        key = segment_key(run_state.source_queue, sequence, run_state.source_kind)
        writes.append(run_state.output(key, value, expected_version=None))
        segments.append(
            {
                "key": key,
                "version": 1,
                "entries": len(chunk),
                "value_sha256": value_digest(value),
            }
        )
    value = {
        "contract": HEAD_CONTRACT,
        "learner": LEARNER,
        "strategy": STRATEGY,
        "version": VERSION,
        "fingerprint_profile": PROFILE,
        "spec": run_state.spec.model_dump(),
        "scope": _scope(run_state),
        "cursor": cursor,
        "segments": segments,
        "entries": sum(item["entries"] for item in segments),
        "use_policy": USE_POLICY,
    }
    key = head_key(run_state.source_queue, run_state.source_kind)
    writes.append(run_state.output(key, value, expected_version=run_state.state_version or None))
    written = write_outputs(api, run_state, writes)
    return {
        "outcome": "updated",
        "observations_read": len(observations),
        "cursor_after": cursor,
        "segments_written": len(writes) - 1,
        "outputs": written,
    }


def recall(
    api: Api,
    *,
    store: str,
    queue: str,
    category: str,
    brief: dict[str, Any],
    report: dict[str, Any] | None = None,
    input_digest: str | None = None,
    limit: int = 5,
    kind: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Similar prior executions with outcome statistics; evidence, never reuse permission."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20")
    head = api.get_record(store, head_key(queue, kind), namespace=namespace)
    if not isinstance(head.value, dict) or head.value.get("contract") != HEAD_CONTRACT:
        raise ValueError("record is not a memory index head")
    query = problem_fingerprints(category, brief_view(brief), report or {}, input_digest)
    bands = set(query["bands"])
    # Small indexes are scanned exactly; LSH bands bound the work once an index grows.
    mode = "scan" if head.value["entries"] <= SCAN_LIMIT else "bands"
    scored: list[tuple[float, bool, dict[str, Any]]] = []
    for segment in head.value["segments"]:
        record = api.get_record(store, segment["key"], segment["version"], namespace=namespace)
        if value_digest(record.value) != segment["value_sha256"]:
            raise ValueError("memory index segment differs from its head pin")
        for item in record.value["entries"]:
            exact = bool(query["exact"]) and item["exact"] == query["exact"]
            if mode == "bands" and not exact and not bands & set(item["bands"]):
                continue
            score = 1.0 if exact else similarity(query["minhash"], item["minhash"])
            if score is not None and score >= SIMILARITY_MIN:
                scored.append((score, exact, item))
    ranked = sorted(
        (row for row in scored if row[2]["ok"]),
        key=lambda row: (-row[0], -row[2]["created_at"], row[2]["run_id"]),
    )
    executions: list[dict[str, Any]] = []
    unreadable = 0
    for score, exact, item in ranked:
        if len(executions) == limit:
            break
        try:
            execution = api.get_execution(item["run_id"], namespace=namespace)
        except NotFound:
            unreadable += 1
            continue
        execution.pop("input_snapshot", None)
        executions.append(
            {"similarity": score, "exact_match": exact, **item, "execution": execution}
        )
    index = NounRef(
        store=store, key=head.key, version=head.version, value_sha256=value_digest(head.value)
    )
    return {
        "profile": PROFILE,
        "learner": {"learner": LEARNER, "strategy": STRATEGY, "version": VERSION},
        "index": index.model_dump(),
        "advice": {
            "learner": LEARNER,
            "strategy": STRATEGY,
            "version": VERSION,
            "store": store,
            "key": head.key,
            "record_version": head.version,
        },
        "query": {
            name: query[name]
            for name in ("category", "template", "phrasing", "exact", "feature_count", "bands")
        },
        "executions": executions,
        "statistics": outcome_statistics([row[2] for row in scored]),
        "coverage": {
            "candidate_mode": mode,
            "indexed_entries": head.value["entries"],
            "similar": len(scored),
            "similarity_min": SIMILARITY_MIN,
            "unreadable_candidates": unreadable,
            "complete": False,
            "reason": "Entries indexed up to the head cursor; scanned when small, otherwise "
            "filtered by four MinHash bands or the exact fingerprint. Statistics cover similar "
            "indexed entries readable through this index noun. Not semantic equivalence or a "
            "complete search.",
        },
        "evidence_only": True,
        "reuse_requires_validation": True,
        "use_policy": USE_POLICY,
    }
