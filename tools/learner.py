#!/usr/bin/env python3
"""Learner seed-pack and run helper; offline commands are the default.

    uv run python tools/learner.py specs
    uv run python tools/learner.py pack --output /tmp/tessera-learners
    uv run python tools/learner.py preview --request run.json
    uv run python tools/learner.py enqueue --request run.json        # TESSERA_URL, TESSERA_KEY
    uv run python tools/learner.py work-once --namespace NS --queue Q --worker NAME
    uv run python tools/learner.py recall --request recall.json

Not a scheduler, daemon or model caller. Online commands use the caller's own grants;
use a queue dedicated to learner runs for work-once.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tessera.client import Client
from tessera.learners import SPECS, RunRequest, memory, pack, run_spec, value_digest
from tessera.learners.contract import RUN_CONTRACT
from tessera.learners.worker import run_claimed


def _load(path: Path) -> Any:
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("document exceeds 64 KiB")
    return json.loads(raw)


def _catalog_spec(request: RunRequest) -> Any:
    for item in SPECS:
        if item.key == request.spec.key:
            return item
    raise ValueError("the pinned spec key is not in this catalog")


def _client(parser: argparse.ArgumentParser, namespace: str) -> Client:
    url, key = os.environ.get("TESSERA_URL", ""), os.environ.get("TESSERA_KEY", "")
    parsed = urlsplit(url)
    if not key or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        parser.error("online commands need TESSERA_URL without credentials/query and TESSERA_KEY")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        parser.error("online commands need HTTPS or a local HTTP service")
    return Client(url, key=key, namespace=namespace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("specs")
    sub.add_parser("pack").add_argument("--output", type=Path, required=True)
    for name in ("preview", "enqueue", "recall"):
        sub.add_parser(name).add_argument("--request", type=Path, required=True)
    work = sub.add_parser("work-once")
    for flag in ("--namespace", "--queue", "--worker"):
        work.add_argument(flag, required=True)
    args = parser.parse_args(argv)

    if args.command == "specs":
        rows = [
            {
                "key": s.key,
                "status": s.status,
                "reads": s.reads,
                "writes": [w.kind for w in s.writes],
            }
            for s in SPECS
        ]
        print(json.dumps(rows, indent=2))
        return 0
    if args.command == "pack":
        print(json.dumps(pack.export(args.output), indent=2, sort_keys=True))
        return 0
    if args.command in ("preview", "enqueue"):
        request = RunRequest.model_validate(_load(args.request))
        task = run_spec(request, _catalog_spec(request))
        if args.command == "preview":
            print(json.dumps({"dry_run": True, "task": task.model_dump(mode="json")}, indent=2))
            return 0
        client = _client(parser, request.namespace)
        try:
            record = client.get_record(request.spec.store, request.spec.key, request.spec.version)
            if value_digest(record.value) != request.spec.value_sha256:
                parser.error("the stored spec noun differs from its pin")
            task_id = client.enqueue(task, idempotency_key=task.idempotency_key or "")
        finally:
            client.close()
        print(json.dumps({"dry_run": False, "enqueued": True, "task_id": task_id}))
        return 0
    if args.command == "recall":
        body = _load(args.request)
        client = _client(parser, body["namespace"])
        try:
            result = memory.recall(
                client,
                store=body["store"],
                queue=body["queue"],
                category=body["category"],
                brief=body["brief"],
                report=body.get("report"),
                input_digest=body.get("input_digest"),
                limit=body.get("limit", 5),
            )
        finally:
            client.close()
        print(json.dumps(result, indent=2))
        return 0
    client = _client(parser, args.namespace)
    try:
        lease = client.claim(args.queue, args.worker)
        if lease is None:
            print(json.dumps({"claimed": False}))
            return 0
        if lease.brief.get("contract") != RUN_CONTRACT:
            parser.error("claimed task is not a learner run; its lease will expire unchanged")
        result = run_claimed(client, lease)
    finally:
        client.close()
    print(json.dumps({"claimed": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
