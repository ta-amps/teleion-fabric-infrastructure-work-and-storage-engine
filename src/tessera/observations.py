"""The optional report.learning block and the stable execution observation view.

Core infrastructure only: validate what a worker chose to report and expose a bounded,
permission-filtered observation of each execution. Learners decide what to learn.
deploy/cloudflare/execution_observations.js mirrors both functions.
"""

from __future__ import annotations

import math
import re
from typing import Any, NoReturn

from tessera.fingerprints import brief_view

OBSERVATION = "execution-observation-v1"
OUTCOMES = ("succeeded", "partial", "failed", "blocked", "no_change")
LIST_FIELDS = ("went_right", "went_wrong", "next_steps", "ideas")
LEARNING_FIELDS = {
    *LIST_FIELDS,
    "outcome",
    "types",
    "path",
    "reasoning_summary",
    "duration_seconds",
    "cost",
    "unknowns",
    "advice",
}
ADVICE_FIELDS = {"learner", "strategy", "version", "store", "key", "record_version"}
MAX_MEASURE = 1_000_000_000
MAX_SAFE_INT = 2**53 - 1
USE_POLICY = {
    "training_corpus": "excluded_by_default",
    "reason": "Execution reports are coordination evidence. Provider terms can restrict "
    "training on model outputs; any training use needs a separate opt-in decision.",
}
REPORTING_HINT = {
    "profile": "execution-learning-v1",
    "optional": True,
    "submit_field": "execution_report.learning",
    "requested": [
        "outcome",
        "went_right",
        "went_wrong",
        "next_steps",
        "ideas",
        "types",
        "path",
        "duration_seconds",
        "cost",
        "unknowns",
        "advice (learner outputs this run used)",
    ],
    "also_useful": [
        "summaries.problem",
        "summaries.solution",
        "harness",
        "model (null when no model was used)",
        "usage",
    ],
    "never_include": ["credentials or secrets", "raw prompts or transcripts", "private reasoning"],
    "missing_fields": "Omit unknown fields or name them in learning.unknowns. "
    "Submission never requires a learning field.",
}
_CURRENCY = re.compile(r"[A-Z]{3}")


def _fail(path: str, detail: str) -> NoReturn:
    raise ValueError(f"{path}: {detail}")


def _object(value: Any, path: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if set(value) - fields:
        _fail(path, "unknown fields")
    return dict(value)


def _text(value: Any, path: str, cap: int) -> str:
    if not isinstance(value, str) or not value.strip(" \t\n\v\f\r"):
        _fail(path, "must be a nonempty string")
    if len(value.encode("utf-8")) > cap:
        _fail(path, f"exceeds {cap} UTF-8 bytes")
    return value


def _number(value: Any, path: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_MEASURE:
        _fail(path, f"must be a finite number from 0 to {MAX_MEASURE}")


def _integer(value: Any, path: str) -> None:
    whole = type(value) is int or (type(value) is float and value.is_integer())
    if not whole or not 1 <= value <= MAX_SAFE_INT:
        _fail(path, "must be a positive safe integer")


def normalize_learning(value: Any) -> dict[str, Any]:
    """Validate report.learning; every field is optional and an attributable worker claim."""
    learning = _object(value, "report.learning", LEARNING_FIELDS)
    if "outcome" in learning and learning["outcome"] not in OUTCOMES:
        _fail("report.learning.outcome", "must be one of " + ", ".join(OUTCOMES))
    for name in LIST_FIELDS:
        if name in learning:
            items = learning[name]
            if not isinstance(items, list) or len(items) > 8:
                _fail(f"report.learning.{name}", "must be an array of at most 8 strings")
            for i, item in enumerate(items):
                _text(item, f"report.learning.{name}[{i}]", 512)
    if "types" in learning:
        types = _object(learning["types"], "report.learning.types", {"input", "output"})
        for role, item in types.items():
            _text(item, f"report.learning.types.{role}", 256)
    if "path" in learning:
        path = _object(learning["path"], "report.learning.path", {"shim", "method"})
        for name, item in path.items():
            _text(item, f"report.learning.path.{name}", 128)
    if "reasoning_summary" in learning:
        _text(learning["reasoning_summary"], "report.learning.reasoning_summary", 2048)
    unknown: set[str] = set()
    if "unknowns" in learning:
        entries = learning["unknowns"]
        if not isinstance(entries, list) or len(entries) > 16:
            _fail("report.learning.unknowns", "must be an array of at most 16 entries")
        for i, item in enumerate(entries):
            entry = _object(item, f"report.learning.unknowns[{i}]", {"field", "reason"})
            unknown.add(_text(entry.get("field"), f"report.learning.unknowns[{i}].field", 64))
            _text(entry.get("reason"), f"report.learning.unknowns[{i}].reason", 512)
    for name in ("duration_seconds", "cost"):
        if name in learning and learning[name] is None and name not in unknown:
            _fail(f"report.learning.{name}", "null requires a matching learning.unknowns entry")
    if learning.get("duration_seconds") is not None:
        _number(learning["duration_seconds"], "report.learning.duration_seconds")
    if learning.get("cost") is not None:
        cost = _object(learning["cost"], "report.learning.cost", {"amount", "currency", "source"})
        _number(cost.get("amount"), "report.learning.cost.amount")
        currency = cost.get("currency")
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            _fail("report.learning.cost.currency", "must be a three-letter uppercase code")
        _text(cost.get("source"), "report.learning.cost.source", 128)
    if "advice" in learning:
        advice = learning["advice"]
        if not isinstance(advice, list) or len(advice) > 8:
            _fail("report.learning.advice", "must be an array of at most 8 entries")
        for i, item in enumerate(advice):
            entry = _object(item, f"report.learning.advice[{i}]", ADVICE_FIELDS)
            for name, cap in (("learner", 64), ("strategy", 64), ("store", 64), ("key", 256)):
                _text(entry.get(name), f"report.learning.advice[{i}].{name}", cap)
            for name in ("version", "record_version"):
                _integer(entry.get(name), f"report.learning.advice[{i}].{name}")
    return learning


def _name(value: Any, *fields: str) -> str | None:
    if value is None:
        return "none"
    if isinstance(value, dict):
        for field in fields:
            if isinstance(value.get(field), str) and value[field]:
                return str(value[field])[:128]
    return None


def observation(
    envelope: dict[str, Any],
    *,
    category: str,
    ok: bool,
    state_after: str | None,
    error: str | None,
) -> dict[str, Any]:
    """Bounded observation of one execution the caller is already permitted to read."""
    report = envelope.get("report") or {}
    summaries = report.get("summaries") or {}
    usage = report.get("usage") or {}
    total = usage.get("total_tokens")
    if type(total) is not int and all(
        type(usage.get(name)) is int for name in ("input_tokens", "output_tokens")
    ):
        total = usage["input_tokens"] + usage["output_tokens"]
    snapshot = envelope.get("input_snapshot") or {}
    brief = snapshot.get("brief")
    return {
        "observation": OBSERVATION,
        "run_id": envelope["run_id"],
        "task_id": envelope["task_id"],
        "fencing_token": envelope["fencing_token"],
        "receipt_hash": envelope["receipt_hash"],
        "report_sha256": envelope["report_sha256"],
        "created_at": envelope["created_at"],
        "queue": envelope["queue"],
        "category": category,
        "tags": list(envelope.get("tags", [])),
        "ok": ok,
        "state_after": state_after,
        "error": None if ok or error is None else error[:512],
        "brief": brief_view(brief) if isinstance(brief, dict) else None,
        "input": {"digest": (envelope.get("fingerprints") or {}).get("input_hash")},
        "observed": {
            "server_duration_seconds": envelope["evidence"]["server_duration_seconds"],
            "retried": envelope["fencing_token"] > 1,
        },
        "report": {
            "problem": summaries.get("problem"),
            "solution": summaries.get("solution"),
            "harness": _name(report.get("harness"), "name") if "harness" in report else None,
            "model": _name(report.get("model"), "name", "provider") if "model" in report else None,
            "total_tokens": total if type(total) is int else None,
            "usage_source": usage.get("source"),
            "learning": dict(report.get("learning") or {}),
        },
        "trust": envelope.get("trust", {}),
    }
