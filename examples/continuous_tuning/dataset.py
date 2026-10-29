"""Curation: fingerprints, deduplication, contamination checks and dataset manifests.

Three fingerprints per row: `exact` (request, offered tools and label), `request`
(normalized text) and `shape` (the text with every slot value masked, which identifies
template plus frame). Training rows may share none of them with any evaluation row,
and must come from training families. Rows live in JSONL bytes pinned by SHA-256.
"""

from __future__ import annotations

import json
import re
from typing import Any

from examples.finetuning.data import canonical, digest

from .task import GENERATOR_VERSION, INSTRUCTION
from .templates import VOCAB
from .tools import FAMILY_SPLIT

DATASET_FORMAT = "tessera.continuous-tuning.dataset.v1"
TRAINING_ORIGINS = ("initial", "stream", "mined", "control", "augmented")
EVAL_ORIGINS = ("frozen_holdout", "fresh_holdout", "sanity")
_VALUES = sorted({value for values in VOCAB.values() for value in values}, key=len, reverse=True)
_MASKS = [
    (re.compile(r"\{.*\}|\[.*\]", re.DOTALL), "<json>"),
    (re.compile(r"\([^()]*\)"), "<obj>"),
    (re.compile(r'"[^"]*"'), "<q>"),
    (re.compile(r"\b(?:t|run|submit)[_-][0-9a-f]{6,}\b"), "<id>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def text_of(row: dict[str, Any]) -> str:
    return row.get("request") or row.get("prompt") or ""


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def mask(text: str) -> str:
    for pattern, token in _MASKS[:3]:
        text = pattern.sub(token, text)
    for value in _VALUES:
        text = text.replace(value, "<v>")
    for pattern, token in _MASKS[3:]:
        text = pattern.sub(token, text)
    return normalize(text)


def fingerprints(row: dict[str, Any]) -> dict[str, str]:
    text = text_of(row)
    label = row.get("target", row.get("answer"))
    return {
        "exact": digest({"text": text, "offered": row.get("offered"), "label": label}),
        "request": digest(normalize(text)),
        "shape": digest(mask(text)),
    }


def jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def read_jsonl(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def fingerprint_sets(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {"exact": set(), "request": set(), "shape": set()}
    for row in rows:
        for name, value in fingerprints(row).items():
            sets[name].add(value)
    return sets


def accept(
    candidates: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    evaluation: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept training candidates that are new, owned and disjoint from every evaluation row."""
    held_out = fingerprint_sets(evaluation)
    seen = fingerprint_sets(pool)
    accepted, rejected = [], []
    for row in candidates:
        reason = None
        fp = fingerprints(row)
        if row.get("origin") not in TRAINING_ORIGINS or row.get("split") != "train":
            reason = "not a training-origin row"
        elif row.get("family") and FAMILY_SPLIT[row["family"]] != "train":
            reason = "held-out family"
        elif row.get("generator_version") != GENERATOR_VERSION:
            reason = "generator version differs"
        elif any(fp[name] in held_out[name] for name in fp):
            reason = "contamination: shares a fingerprint with an evaluation row"
        elif fp["exact"] in seen["exact"] or fp["request"] in seen["request"]:
            reason = "duplicate of an accepted row"
        if reason:
            rejected.append({"id": row["id"], "reason": reason})
            continue
        accepted.append(row)
        for name, value in fp.items():
            seen[name].add(value)
    return accepted, rejected


def contamination(train: list[dict[str, Any]], evaluation: list[dict[str, Any]]) -> dict[str, int]:
    """Counts of shared fingerprints; every count must be zero before training or scoring."""
    left, right = fingerprint_sets(train), fingerprint_sets(evaluation)
    counts = {name: len(left[name] & right[name]) for name in left}
    counts["held_out_family_rows"] = sum(
        1 for row in train if row.get("family") and FAMILY_SPLIT[row["family"]] != "train"
    )
    return counts


def manifest(role: str, parts: dict[str, list[dict[str, Any]]], **extra: Any) -> dict[str, Any]:
    """A `noun.benchmark_suite` value: rows are pinned by the SHA-256 of their JSONL bytes."""
    summary = {}
    for name, rows in parts.items():
        summary[name] = {
            "rows": len(rows),
            "jsonl_sha256": digest_bytes(jsonl(rows)),
            "ids_sha256": digest([row["id"] for row in rows]),
            "fingerprints_sha256": digest(
                {k: sorted(v) for k, v in fingerprint_sets(rows).items()}
            ),
            "families": sorted({row["family"] for row in rows if row.get("family")}),
            "templates": len({row["template"] for row in rows if row.get("template")}),
            "origins": sorted({row.get("origin", "") for row in rows}),
            "refusals": sum(1 for row in rows if row.get("kind") == "refusal"),
        }
    return {
        "format": DATASET_FORMAT,
        "role": role,
        "generator_version": GENERATOR_VERSION,
        "instruction_sha256": digest(INSTRUCTION),
        "split_policy": "Whole tool families and their templates and frames belong to one split; "
        "dev selects, the frozen test decides promotion, training never sees either.",
        "provenance": {
            "origin": "repository-authored deterministic templates and computed labels",
            "license": "MIT",
            "model_generated_training_data": False,
            "external_sources": [],
        },
        "parts": summary,
        **extra,
    }


def digest_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
