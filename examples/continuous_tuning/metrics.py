"""Strict scoring of one generated reply against its computed label, plus a failure taxonomy.

Strict means the whole reply is one JSON object in the stated contract, with values of
the served schema types. A lenient parse (code fences, surrounding prose, the
`name`/`parameters` alias) is reported only as a diagnostic, to separate formatting
failures from wrong tools or values. No value is coerced in either mode.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .tools import FAMILY_OF, allowed_types, canonical_arguments, json_type, type_matches, validate

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
CATEGORIES = (
    "invalid_json",
    "wrapped_json",
    "wrong_shape",
    "false_refusal",
    "missed_refusal",
    "wrong_refusal_reason",
    "unoffered_tool",
    "wrong_tool",
    "missing_argument",
    "extra_argument",
    "stringified_value",
    "type_error",
    "wrong_value",
    "schema_violation",
)


def parse_strict(text: str) -> Any:
    try:
        value = json.loads(text.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_lenient(text: str) -> Any:
    candidates = [match.group(1) for match in FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start in [i for i, char in enumerate(candidate) if char == "{"][:50]:
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except ValueError:
                continue
            if isinstance(value, dict):
                if {"name", "parameters"} <= set(value):
                    value = {"tool": value["name"], "arguments": value["parameters"]}
                return value
    return None


def interpret(value: Any) -> tuple[str, Any, Any]:
    if isinstance(value, dict) and set(value) == {"refusal"} and isinstance(value["refusal"], str):
        return "refusal", value["refusal"], None
    if (
        isinstance(value, dict)
        and set(value) == {"tool", "arguments"}
        and isinstance(value["tool"], str)
        and isinstance(value["arguments"], dict)
    ):
        return "call", value["tool"], value["arguments"]
    return "bad_shape", None, None


def stringified(value: Any, schema: dict[str, Any]) -> bool:
    """The field failure: an object, array, number or boolean sent as a JSON string."""
    if not isinstance(value, str) or "string" in allowed_types(schema):
        return False
    try:
        decoded = json.loads(value)
    except ValueError:
        return False
    return not isinstance(decoded, str) and type_matches(decoded, schema)


def compare(row: dict[str, Any], kind: str, tool: Any, args: Any, tools: dict[str, Any]):
    target, failures = row["target"], []
    if "refusal" in target:
        if kind == "call":
            failures.append({"category": "missed_refusal"})
        elif tool != target["refusal"]:
            failures.append({"category": "wrong_refusal_reason"})
        return failures, None, None
    if kind == "refusal":
        return [{"category": "false_refusal"}], False, False
    if tool not in row["offered"] or tool not in tools:
        return [{"category": "unoffered_tool"}], False, False
    if tool != target["tool"]:
        return [{"category": "wrong_tool", "predicted": tool}], False, False
    schema = tools[tool]["inputSchema"]
    properties = schema.get("properties", {})
    predicted = canonical_arguments(args, schema)
    gold = target["arguments"]
    types_ok = all(type_matches(v, properties.get(k, {})) for k, v in predicted.items())
    for name, expected in gold.items():
        expected_type = json_type(expected)
        if name not in predicted:
            failures.append({"category": "missing_argument", "arg": name, "type": expected_type})
        elif stringified(predicted[name], properties.get(name, {})):
            failures.append({"category": "stringified_value", "arg": name, "type": expected_type})
        elif not type_matches(predicted[name], properties.get(name, {})):
            failures.append({"category": "type_error", "arg": name, "type": expected_type})
        elif predicted[name] != expected:
            failures.append({"category": "wrong_value", "arg": name, "type": expected_type})
    for name in sorted(set(predicted) - set(gold)):
        failures.append({"category": "extra_argument", "arg": name})
    errors = validate(predicted, schema)
    explained = {"type_error", "stringified_value", "missing_argument", "extra_argument"}
    if errors and not any(f["category"] in explained for f in failures):
        failures.append({"category": "schema_violation", "detail": errors[0][:200]})
    return failures, types_ok, not errors


def score(row: dict[str, Any], output: str, tools: dict[str, Any]) -> dict[str, Any]:
    strict = parse_strict(output)
    kind, tool, args = interpret(strict)
    if strict is None:
        lenient = parse_lenient(output)
        failures = [{"category": "wrapped_json" if lenient is not None else "invalid_json"}]
        types_ok, schema_valid = (None, None) if "refusal" in row["target"] else (False, False)
    elif kind == "bad_shape":
        lenient = parse_lenient(output)
        failures = [{"category": "wrong_shape"}]
        types_ok, schema_valid = (None, None) if "refusal" in row["target"] else (False, False)
    else:
        lenient = strict
        failures, types_ok, schema_valid = compare(row, kind, tool, args, tools)
    lenient_kind, lenient_tool, lenient_args = interpret(lenient)
    lenient_exact = (
        lenient_kind != "bad_shape"
        and not compare(row, lenient_kind, lenient_tool, lenient_args, tools)[0]
    )
    gold_refusal = row["target"].get("refusal")
    predicted_refusal = tool if kind == "refusal" else None
    return {
        "id": row["id"],
        "tool": row["tool"],
        "family": row["family"],
        "gold_kind": row["kind"],
        "json_valid": strict is not None,
        "shape_valid": kind != "bad_shape",
        "schema_valid": schema_valid,
        "types_ok": types_ok,
        "stringified": any(f["category"] == "stringified_value" for f in failures),
        "exact": not failures,
        "exact_lenient": lenient_exact,
        "refusal_correct": (gold_refusal == predicted_refusal)
        if gold_refusal
        else predicted_refusal is None,
        "failures": failures,
        "features": row["features"],
    }


def rate(values: list[Any]) -> float | None:
    counted = [bool(value) for value in values if value is not None]
    return round(sum(counted) / len(counted), 6) if counted else None


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [s for s in scores if s["gold_kind"] == "call"]
    refusals = [s for s in scores if s["gold_kind"] == "refusal"]
    by_family: dict[str, list[bool]] = {}
    for item in scores:
        by_family.setdefault(item["family"] or "unsupported", []).append(item["exact"])
    failure_counts = Counter(f["category"] for s in scores for f in s["failures"])
    return {
        "n": len(scores),
        "n_calls": len(calls),
        "n_refusals": len(refusals),
        "exact_match_strict": rate([s["exact"] for s in scores]),
        "exact_match_lenient": rate([s["exact_lenient"] for s in scores]),
        "json_valid_rate": rate([s["json_valid"] for s in scores]),
        "shape_valid_rate": rate([s["shape_valid"] for s in scores]),
        "schema_valid_rate": rate([s["schema_valid"] for s in calls]),
        "type_correct_rate": rate([s["types_ok"] for s in calls]),
        "stringified_rate": rate([s["stringified"] for s in calls]),
        "refusal_accuracy": rate([s["refusal_correct"] for s in scores]),
        "refusal_recall": rate([s["refusal_correct"] for s in refusals]),
        "false_refusal_rate": rate(
            [any(f["category"] == "false_refusal" for f in s["failures"]) for s in calls]
        ),
        "exact_by_family": {name: rate(values) for name, values in sorted(by_family.items())},
        "failure_counts": dict(sorted(failure_counts.items())),
    }


def family_of(tool: str | None) -> str | None:
    return FAMILY_OF.get(tool or "")
