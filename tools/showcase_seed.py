"""Author bounded creative seed nouns and verbs. Preview is offline by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from tessera import TaskSpec
from tessera.client import Client
from tessera.errors import NotFound, VersionConflict

PACK = Path(__file__).resolve().parents[1] / "examples/showcase/opportunity-lab.json"
STAGES = {
    "discover",
    "ideate",
    "remix",
    "evaluate",
    "design_benchmark",
    "propose_seeds",
    "map_system",
}


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    beneficiary: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(min_length=1, max_length=1200)
    difference: str = Field(min_length=1, max_length=500)
    assumptions: list[str] = Field(min_length=1, max_length=5)
    proposed_nouns: list[str] = Field(min_length=1, max_length=5)
    proposed_verbs: list[str] = Field(min_length=1, max_length=5)
    falsifiable_test: str = Field(min_length=1, max_length=600)
    evidence_status: str = "proposal"

    @field_validator("evidence_status")
    @classmethod
    def proposal_only(cls, value: str) -> str:
        if value != "proposal":
            raise ValueError("ideation cannot assert verified status")
        return value


class Ideas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    example_only: StrictBool
    theme_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    ideas: list[Idea] = Field(min_length=1, max_length=3)

    @field_validator("example_only")
    @classmethod
    def marked_example(cls, value: bool) -> bool:
        if not value:
            raise ValueError("showcase outputs must be marked example_only")
        return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def load_pack(path: Path = PACK) -> dict[str, Any]:
    if path.stat().st_size > 65536:
        raise ValueError("pack exceeds 64 KiB")
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or data.get("example_only") is not True
    ):
        raise ValueError("invalid showcase pack")
    topics = data.get("themes", [])
    ids = [t["id"] for t in topics]
    if len(topics) != 11 or len(set(ids)) != 11:
        raise ValueError("eleven unique themes required")
    if set(data.get("procedures", {})) != STAGES:
        raise ValueError("creative procedures are incomplete")
    if data["bounds"].get("automatic_children") != 0 or data["bounds"].get("server_inference"):
        raise ValueError("creative seeds cannot activate inference or recursive work")
    return data


def nouns(pack: dict[str, Any]) -> dict[str, Any]:
    records = {
        "source": pack["source"],
        "profile": {k: pack[k] for k in ["example_only", "bounds", "evaluation", "boundaries"]},
        "idea-contract": Ideas.model_json_schema(),
    }
    records.update({"theme." + t["id"]: t for t in pack["themes"]})
    records.update({"procedure." + k: v for k, v in pack["procedures"].items()})
    return records


def task(
    pack: dict[str, Any],
    theme: dict[str, Any],
    queue: str,
    refs: list[dict[str, Any]],
    variant: int = 0,
) -> TaskSpec:
    identity = digest(
        {
            "theme": theme["id"],
            "variant": variant,
            "queue": queue,
            "refs": refs,
            "procedure": pack["procedures"]["ideate"],
        }
    )
    return TaskSpec(
        queue=queue,
        kind="verb.solve_draft",
        effect_class="write-internal",
        visibility_scope="private",
        idempotency_key="opportunity-lab:" + identity,
        tags=["category:verb.solve_draft", "stage:ideate", "status:quarantined", "data:example"],
        brief={
            "example_only": True,
            "stage": "ideate",
            "theme_id": theme["id"],
            "attempt_index": variant,
            "shared_brief_hash": digest({"theme": theme["id"], "refs": refs}),
            "approach": "practical"
            if variant == 0
            else "seek a materially different approach; cross-domain and unconventional ideas are welcome",
            "input_refs": refs,
            "instructions": pack["procedures"]["ideate"]["instructions"],
            "output_contract": Ideas.model_json_schema(),
            "output_store": "showcase.candidates",
            "output_key": "Use this task's actual task_id as the record key.",
            "acceptance": [
                "Output matches the pinned idea contract",
                "Theme matches this brief",
                "Every proposal includes an assumption and falsifiable test",
                "No claim of execution, verified novelty or publication",
            ],
            "bounds": pack["bounds"],
            "execution": {
                "reasoning": "authorized_worker",
                "server_fallback": False,
                "maximum_automatically_enqueued_children": 0,
            },
        },
    )


def prepare(
    backend: Any, pack: dict[str, Any], namespace: str, queue: str, variants: int = 2
) -> dict[str, Any]:
    """Use existing CAS/task authority; never create a queue or overwrite a noun."""
    if type(variants) is not int or not 1 <= variants <= 4:
        raise ValueError("choose one to four explicit alternative attempts")
    pins = {}
    for key, value in nouns(pack).items():
        try:
            record = backend.get_record("showcase.seeds", key, namespace=namespace)
        except NotFound:
            try:
                record = backend.put_record(
                    "showcase.seeds",
                    key,
                    value,
                    expected_version=0,
                    namespace=namespace,
                    visibility_scope="private",
                )
            except VersionConflict:
                record = backend.get_record("showcase.seeds", key, namespace=namespace)
        if digest(record.value) != digest(value):
            raise ValueError("seed identity conflict; use a new versioned key or namespace")
        pins[key] = {
            "store": record.store,
            "key": key,
            "version": record.version,
            "value_sha256": digest(record.value),
        }
    tasks = []
    for theme in pack["themes"]:
        refs = [
            pins[key]
            for key in [
                "source",
                "profile",
                "idea-contract",
                "procedure.ideate",
                "theme." + theme["id"],
            ]
        ]
        for variant in range(variants):
            spec = task(pack, theme, queue, refs, variant)
            tasks.append(backend.enqueue(spec, namespace=namespace))
    return {"noun_refs": pins, "task_ids": tasks, "automatic_followups": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="conformance-showcase")
    parser.add_argument("--queue", default="conformance-showcase")
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--variants", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--check-output", type=Path)
    args = parser.parse_args(argv)
    pack = load_pack()
    if args.check_output:
        value = Ideas.model_validate_json(args.check_output.read_text())
        if value.theme_id not in {t["id"] for t in pack["themes"]}:
            parser.error("unknown theme")
        print(json.dumps({"shape_valid": True, "ideas_verified": False}))
        return 0
    if not args.enqueue:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "nouns": nouns(pack),
                    "initial_verbs": 11 * args.variants,
                    "queue": args.queue,
                    "namespace": args.namespace,
                    "followups_activated": False,
                },
                indent=2,
            )
        )
        return 0
    url, key = os.environ.get("TESSERA_URL", ""), os.environ.get("TESSERA_KEY", "")
    parsed = urlsplit(url)
    if (
        not key
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        parser.error("enqueue needs scoped TESSERA_KEY and HTTPS TESSERA_URL (or local HTTP)")
    client = Client(url, key=key, namespace=args.namespace)
    try:
        result = prepare(client, pack, args.namespace, args.queue, args.variants)
    finally:
        client.close()
    print(json.dumps({"dry_run": False, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
