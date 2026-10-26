"""Atomic storage (SPEC §6): versioned records written by compare-and-set, with every version kept,
and content-addressed blobs that can never change."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._base import StoreBase
from tessera.errors import InvalidRequest, NotFound, TesseraError, VersionConflict
from tessera.models import RecordVersion, RecordWrite
from tessera.receipts import blob_hash, canonical_json

MAX_BLOB_BYTES = 16 * 1024 * 1024


class StorageOps(StoreBase):
    def put_record(
        self, store: str, key: str, value: Any, *, expected_version: int | None
    ) -> RecordVersion:
        """Write one record if its head is still `expected_version` (None: the key must be new)."""
        write = RecordWrite(store=store, key=key, value=value, expected_version=expected_version)
        return self.put_records([write])[0]

    def put_records(self, writes: Sequence[RecordWrite]) -> list[RecordVersion]:
        """Apply every write or none: one conflict rolls the whole batch back."""
        if not writes:
            raise InvalidRequest("a batch needs at least one write")
        now = self.clock.now()
        written: list[RecordVersion] = []
        with self._tx() as c:
            for w in writes:
                head = c.execute(
                    "SELECT version FROM record_heads WHERE store = ? AND key = ?", (w.store, w.key)
                ).fetchone()
                current = None if head is None else int(head["version"])
                if current != w.expected_version:
                    raise VersionConflict(
                        f"{w.store}/{w.key}: expected version {w.expected_version}, head is {current}",
                        current_version=current,
                    )
                try:
                    text = canonical_json(w.value)
                except (TypeError, ValueError) as exc:
                    raise InvalidRequest(f"{w.store}/{w.key}: value is not JSON: {exc}") from exc
                version = (current or 0) + 1
                c.execute(
                    "INSERT INTO records (store, key, version, value, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (w.store, w.key, version, text, now),
                )
                c.execute(
                    "INSERT INTO record_heads (store, key, version) VALUES (?, ?, ?)"
                    " ON CONFLICT (store, key) DO UPDATE SET version = excluded.version",
                    (w.store, w.key, version),
                )
                written.append(
                    RecordVersion(
                        store=w.store, key=w.key, version=version, value=w.value, created_at=now
                    )
                )
            self._event(
                c,
                "records_written",
                now,
                detail={
                    "writes": [
                        {"store": r.store, "key": r.key, "version": r.version} for r in written
                    ]
                },
            )
        return written

    def get_record(self, store: str, key: str, version: int | None = None) -> RecordVersion:
        with self._read() as c:
            if version is None:
                head = c.execute(
                    "SELECT version FROM record_heads WHERE store = ? AND key = ?", (store, key)
                ).fetchone()
                if head is None:
                    raise NotFound(f"record {store}/{key} does not exist")
                version = int(head["version"])
            row = c.execute(
                "SELECT * FROM records WHERE store = ? AND key = ? AND version = ?",
                (store, key, version),
            ).fetchone()
        if row is None:
            raise NotFound(f"record {store}/{key} has no version {version}")
        return self._record(row)

    def record_history(self, store: str, key: str) -> list[RecordVersion]:
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM records WHERE store = ? AND key = ? ORDER BY version", (store, key)
            ).fetchall()
        if not rows:
            raise NotFound(f"record {store}/{key} does not exist")
        return [self._record(r) for r in rows]

    def put_blob(self, data: bytes) -> str:
        """Store bytes under their SHA-256 and return it. Storing the same bytes twice is a no-op."""
        if len(data) > MAX_BLOB_BYTES:
            raise InvalidRequest(f"blob is over {MAX_BLOB_BYTES} bytes")
        digest = blob_hash(data)
        now = self.clock.now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO blobs (sha256, data, size, created_at) VALUES (?, ?, ?, ?)",
                (digest, sqlite3.Binary(data), len(data), now),
            )
            if cur.rowcount:
                self._event(c, "blob_stored", now, detail={"sha256": digest, "size": len(data)})
        return digest

    def get_blob(self, sha256: str) -> bytes:
        with self._read() as c:
            row = c.execute("SELECT data FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            raise NotFound(f"blob {sha256} does not exist")
        data = bytes(row["data"])
        if blob_hash(data) != sha256:
            raise TesseraError(f"blob {sha256} failed its hash check")
        return data

    @staticmethod
    def _record(row: sqlite3.Row) -> RecordVersion:
        return RecordVersion(
            store=row["store"],
            key=row["key"],
            version=row["version"],
            value=json.loads(row["value"]),
            created_at=row["created_at"],
        )
