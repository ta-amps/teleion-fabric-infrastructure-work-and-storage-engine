"""The learner's use of a local Tessera store: principals, private nouns, blobs and claims.

Nothing here is a second queue or registry. Records, blobs, leases, receipts and
execution capture are Tessera's own APIs, called in-process on a task-owned SQLite
file. Two keys exist: the worker that owns every private noun, and another principal
used only to prove it cannot read them. Secrets are discarded at issue; only key ids
(not credentials) are kept in a mode-600 file so later ticks act as the same owner.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest

from tessera import QueueConfig, RecordWrite, Store
from tessera.errors import Forbidden, NotFound, VersionConflict
from tessera.receipts import canonical_json

NAMESPACE = QUEUE = "conformance-tuning"
WORKER = "tuning-worker"
STORES = {
    "specs": "tuning.specs",
    "data": "tuning.data",
    "models": "tuning.models",
    "evals": "tuning.evals",
}
CHUNK = 8 * 1024 * 1024


def stored(value: Any) -> Any:
    """The value exactly as Tessera stores it (whole floats become integers)."""
    return json.loads(canonical_json(value))


def noun_ref(record: Any, value: Any) -> dict[str, Any]:
    return {
        "store": record.store,
        "key": record.key,
        "version": record.version,
        "value_sha256": digest(value),
    }


class Fabric:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.store = Store(root / "fabric.db")
        principals = root / "principals.json"
        if principals.exists():
            ids = json.loads(principals.read_text())
        else:
            scopes = ["store", "namespace:" + NAMESPACE]
            _, worker = self.store.issue_key(WORKER, queues=[QUEUE], scopes=scopes, days=365)
            _, other = self.store.issue_key("other-principal", queues=[QUEUE], scopes=scopes)
            ids = {"worker_key_id": worker.key_id, "other_key_id": other.key_id}
            principals.write_text(json.dumps(ids, indent=1) + "\n")
            os.chmod(principals, 0o600)
        self.worker_key_id = ids["worker_key_id"]
        self.other_key_id = ids["other_key_id"]
        with self.scope():
            self.store.define_queue(
                QueueConfig(
                    name=QUEUE,
                    lease_seconds=3600,
                    heartbeat_seconds=60,
                    miss_limit=10,
                    max_attempts=1,
                )
            )

    def close(self) -> None:
        self.store.close()

    def grant(self) -> Any:
        return self.store.get_key(self.worker_key_id)

    @contextmanager
    def scope(self, key_id: str | None = None) -> Iterator[None]:
        with self.store.scope_context(namespace=NAMESPACE, key_id=key_id or self.worker_key_id):
            yield

    def put(self, area: str, role: str, value: dict[str, Any], tags: list[str]) -> dict[str, Any]:
        """Create-only private noun keyed by role and content digest; replays return the same ref."""
        value = stored(value)
        store, key = STORES[area], f"{role}.{digest(value)}"
        write = RecordWrite(
            store=store,
            key=key,
            value=value,
            expected_version=0,
            visibility_scope="private",
            tags=sorted({*tags, f"role:{role}", "data:example"}),
        )
        try:
            record = self.store.put_records([write])[0]
        except VersionConflict:
            record = self.store.get_record(store, key, 1)
        if digest(record.value) != digest(value):
            raise ValueError(f"readback of {key} differs from the written noun")
        return noun_ref(record, value)

    def read(self, ref: dict[str, Any]) -> dict[str, Any]:
        record = self.store.get_record(ref["store"], ref["key"], ref["version"])
        if digest(record.value) != ref["value_sha256"]:
            raise ValueError(f"{ref['key']} v{ref['version']} differs from its pinned digest")
        value: dict[str, Any] = record.value
        return value

    def list(self, area: str, prefix: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        records = self.store.list_records(STORES[area], prefix=prefix, limit=1000)
        return [(noun_ref(r, r.value), r.value) for r in records]

    def put_bytes(self, data: bytes) -> dict[str, Any]:
        """Private content-addressed chunks; the whole-file digest is checked on readback."""
        chunks = [
            self.store.put_blob(data[i : i + CHUNK], visibility_scope="private")
            for i in range(0, max(len(data), 1), CHUNK)
        ]
        blob = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "chunks": chunks}
        if self.get_bytes(blob) != data:
            raise ValueError("blob readback differs")
        return blob

    def get_bytes(self, blob: dict[str, Any]) -> bytes:
        data = b"".join(self.store.get_blob(sha) for sha in blob["chunks"])
        if hashlib.sha256(data).hexdigest() != blob["sha256"] or len(data) != blob["bytes"]:
            raise ValueError("blob content differs from its recorded digest")
        return data

    def pointer(self, learner: str) -> tuple[dict[str, Any] | None, int]:
        try:
            record = self.store.get_record(STORES["models"], f"champion.{learner}")
        except NotFound:
            return None, 0
        return record.value, record.version

    def set_pointer(
        self, learner: str, value: dict[str, Any], expected_version: int
    ) -> dict[str, Any]:
        """The one mutable noun: compare-and-set, so concurrent rounds cannot both move it."""
        value = stored(value)
        write = RecordWrite(
            store=STORES["models"],
            key=f"champion.{learner}",
            value=value,
            expected_version=expected_version,
            visibility_scope="private",
            tags=["role:champion_pointer", "data:example", "status:local_only"],
        )
        record = self.store.put_records([write])[0]
        return noun_ref(record, value)

    def denied_to_other(self, refs: list[dict[str, Any]], blobs: list[str], run_ids: list[str]):
        """Every private noun, blob and execution must be invisible to another principal."""
        denied, leaked = [], []
        with self.scope(self.other_key_id):
            grant = self.store.get_key(self.other_key_id)
            probes: list[tuple[str, Any]] = [
                (f"record:{r['key']}", lambda r=r: self.store.get_record(r["store"], r["key"]))
                for r in refs
            ]
            probes += [(f"blob:{b[:12]}", lambda b=b: self.store.get_blob(b)) for b in blobs]
            probes += [
                (
                    f"execution:{run[:16]}",
                    lambda run=run: self.store.get_execution(run, grant=grant),
                )
                for run in run_ids
            ]
            for name, probe in probes:
                try:
                    probe()
                except (Forbidden, NotFound):
                    denied.append(name)
                else:
                    leaked.append(name)
        return {"denied": len(denied), "leaked": leaked, "probes": len(probes)}
