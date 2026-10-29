"""Deterministic seed-pack export of learner spec nouns, a default config and verb templates.

Exporting or importing the pack performs no network call, enqueue, activation or
publication. The default config enables nothing; an operator writes a config noun.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tessera.learners.catalog import SPECS
from tessera.learners.contract import (
    CONFIG_CONTRACT,
    RUN_CONTRACT,
    LearnerConfig,
    RunRequest,
    value_digest,
)
from tessera.models import RecordWrite
from tessera.observations import OBSERVATION, REPORTING_HINT

PACK = "tessera.learners"
PACK_VERSION = 1


def _name(key: str) -> str:
    return key.replace(":", "-") + ".json"


def files() -> dict[str, Any]:
    """Relative path -> JSON value. Same output for the same source revision."""
    out: dict[str, Any] = {}
    for spec in SPECS:
        out[f"nouns/specs/{_name(spec.key)}"] = spec.model_dump(mode="json")
    out["nouns/config/default.json"] = LearnerConfig().model_dump(mode="json")
    for spec in SPECS:
        if spec.status != "implemented":
            continue
        value = spec.model_dump(mode="json")
        out[f"verbs/{_name(spec.key)}"] = {
            "contract": RUN_CONTRACT,
            "learner": spec.learner,
            "strategy": spec.strategy,
            "spec_version": spec.version,
            "spec_key": spec.key,
            "spec_value_sha256": value_digest(value),
            "kind": spec.verb.kind,
            "runner": spec.verb.runner,
            "request_schema": RunRequest.model_json_schema(),
            "reporting": REPORTING_HINT,
        }
    out["manifest.json"] = {
        "pack": PACK,
        "version": PACK_VERSION,
        "requires": {"observations": OBSERVATION, "read": "GET /v1/executions or MCP tool"},
        "activation": "none: importing or exporting enqueues, enables or publishes nothing",
        "config_contract": CONFIG_CONTRACT,
        "files": {path: value_digest(value) for path, value in sorted(out.items())},
    }
    return out


def export(directory: Path) -> dict[str, Any]:
    """Write the pack files; returns the manifest."""
    for path, value in sorted(files().items()):
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    manifest: dict[str, Any] = files()["manifest.json"]
    return manifest


def spec_records(namespace: str) -> list[RecordWrite]:
    """Private spec nouns for an explicit, separately authorized import write."""
    return [spec.record(namespace) for spec in SPECS]
