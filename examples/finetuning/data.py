"""Owned synthetic research observations and family-separated dataset manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SPLITS = ("train", "dev", "test")
INSTRUCTION = (
    "Turn this synthetic observation and project profile into a capability noun and "
    "a candidate follow-up verb. Preserve evidence and unknowns; do not claim execution. "
    "Return only JSON with capability and follow_up."
)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_rows() -> list[dict[str, Any]]:
    """Templates authored for this example; no external text or model outputs."""
    rows = []
    for family in range(10):
        split = "train" if family < 6 else "dev" if family < 8 else "test"
        for variation in range(2):
            identity = f"synthetic-{family:02d}-{variation}"
            task = "source extraction" if variation == 0 else "task planning"
            source = {
                "id": identity,
                "synthetic": True,
                "family": f"publisher-{family:02d}",
                "observation": f"Model M{family} supports {task}. Local latency and grounding are unmeasured.",
                "evidence_status": "synthetic_claim",
            }
            rows.append(
                {
                    "id": identity,
                    "split": split,
                    "source_family": source["family"],
                    "project_family": f"project-{family:02d}",
                    "input": {
                        "source": source,
                        "profile": {
                            "id": f"project-{family:02d}",
                            "interest": task,
                            "hardware": "unknown",
                        },
                    },
                    "output": {
                        "capability": {
                            "category_id": "noun.model_spec",
                            "model": f"M{family}",
                            "capability": task,
                            "evidence": [identity],
                            "relevance": "candidate",
                            "unknowns": ["local_latency", "grounding", "hardware_fit"],
                            "status": "synthetic_proposal",
                        },
                        "follow_up": {
                            "kind": "verb.benchmark_harness",
                            "goal": f"Compare {task} against the unchanged baseline on permitted local fixtures.",
                            "activation": "proposal_only",
                        },
                    },
                }
            )
    return rows


def validate_rows(rows: list[dict[str, Any]]) -> None:
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("dataset must have unique nonempty row identities")
    families: dict[str, dict[str, str]] = {
        field: {} for field in ("source_family", "project_family")
    }
    seen_splits = set()
    for row in rows:
        split = row["split"]
        if split not in SPLITS:
            raise ValueError("unknown split")
        seen_splits.add(split)
        for field, owners in families.items():
            family = row[field]
            if not isinstance(family, str) or not family:
                raise ValueError("a source and project family are required")
            if owners.setdefault(family, split) != split:
                raise ValueError("source or project family leaks across splits")
        source = row["input"]["source"]
        output = row["output"]
        if source.get("synthetic") is not True or source.get("id") != row["id"]:
            raise ValueError("example accepts only identified owned synthetic observations")
        if (
            source.get("family") != row["source_family"]
            or row["input"]["profile"]["id"] != row["project_family"]
        ):
            raise ValueError("family manifest must match source and project identities")
        if output["capability"]["evidence"] != [row["id"]]:
            raise ValueError("target evidence must point at its own observation")
        if output["capability"]["category_id"] != "noun.model_spec":
            raise ValueError("unsupported candidate noun category")
        if output["follow_up"]["activation"] != "proposal_only":
            raise ValueError("training examples cannot activate follow-up work")
    if seen_splits != set(SPLITS):
        raise ValueError("train, dev and test splits are all required")


def messages(row: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": INSTRUCTION + "\n" + canonical(row["input"]).decode()},
        {"role": "assistant", "content": canonical(row["output"]).decode()},
    ]


def prepare(directory: Path) -> dict[str, Any]:
    rows = synthetic_rows()
    validate_rows(rows)
    directory.mkdir(parents=True, exist_ok=False)
    files = {}
    for split in SPLITS:
        selected = [row for row in rows if row["split"] == split]
        for name, values in [
            (split + ".jsonl", selected),
            (split + "-messages.jsonl", [{"messages": messages(row)} for row in selected]),
        ]:
            path = directory / name
            path.write_bytes(b"".join(canonical(row) + b"\n" for row in values))
            files[name] = {"sha256": file_digest(path), "rows": len(values)}
    manifest = {
        "format": "tessera.synthetic-research.v1",
        "synthetic": True,
        "provenance": {
            "origin": "repository-authored deterministic templates",
            "license": "MIT",
            "generator_version": 1,
            "external_sources": [],
            "model_generated_training_data": False,
        },
        "dataset_sha256": digest(rows),
        "instruction_sha256": digest(INSTRUCTION),
        "split_policy": "Separate source families AND project families; never choose steps on test loss.",
        "splits": {
            split: {
                "ids": [r["id"] for r in rows if r["split"] == split],
                "source_families": sorted(
                    {r["source_family"] for r in rows if r["split"] == split}
                ),
                "project_families": sorted(
                    {r["project_family"] for r in rows if r["split"] == split}
                ),
            }
            for split in SPLITS
        },
        "files": files,
        "limitations": [
            "Tiny owned synthetic fixture; no factual or task-quality improvement can be inferred.",
            "No private histories, hidden reasoning, provider responses or third-party datasets.",
        ],
    }
    (directory / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    return manifest


def load(directory: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if (
        manifest.get("format") != "tessera.synthetic-research.v1"
        or manifest.get("synthetic") is not True
    ):
        raise ValueError("unsupported dataset manifest")
    if manifest.get("instruction_sha256") != digest(INSTRUCTION):
        raise ValueError("instruction drift")
    expected_files = {s + suffix for s in SPLITS for suffix in (".jsonl", "-messages.jsonl")}
    if set(manifest["files"]) != expected_files:
        raise ValueError("dataset file set differs")
    for name, entry in manifest["files"].items():
        if file_digest(directory / name) != entry["sha256"]:
            raise ValueError("dataset file fingerprint differs")
        if len((directory / name).read_bytes().splitlines()) != entry["rows"]:
            raise ValueError("dataset row count differs")
    splits = {
        split: [
            json.loads(line) for line in (directory / (split + ".jsonl")).read_bytes().splitlines()
        ]
        for split in SPLITS
    }
    rows = [row for split in SPLITS for row in splits[split]]
    validate_rows(rows)
    if digest(rows) != manifest["dataset_sha256"]:
        raise ValueError("dataset fingerprint differs")
    for split, selected in splits.items():
        declared = manifest["splits"][split]
        if any(row["split"] != split for row in selected) or declared["ids"] != [
            row["id"] for row in selected
        ]:
            raise ValueError("split membership differs")
        for field in ("source_family", "project_family"):
            if declared[field.replace("family", "families")] != sorted(
                {row[field] for row in selected}
            ):
                raise ValueError("split family manifest differs")
        chat = [
            json.loads(line)
            for line in (directory / (split + "-messages.jsonl")).read_bytes().splitlines()
        ]
        if chat != [{"messages": messages(row)} for row in selected]:
            raise ValueError("chat export differs from reviewed rows")
    return manifest, splits
