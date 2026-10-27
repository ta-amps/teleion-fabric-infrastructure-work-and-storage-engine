"""Queryable noun stores and persistent named file buckets (SPEC §6 extension).

Agents joining a shared fabric can list stores, query head records by prefix, define named buckets
(`nouns`, `verbs`, `kits`, `cases`, `provisions`, `traces`), and read/write versioned content-addressed
files with compare-and-set (`expected_version`) and complete version histories.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from tessera._storage import StorageOps
from tessera.errors import InvalidRequest, NotFound, VersionConflict
from tessera.models import (
    BUCKET_KEY_PATTERN,
    NAME_PATTERN,
    BucketConfig,
    BucketInfo,
    ObjectMeta,
    RecordVersion,
    StoreSummary,
)
from tessera.receipts import canonical_json

_BUCKET_RE = re.compile(NAME_PATTERN)
_OBJECT_KEY_RE = re.compile(BUCKET_KEY_PATTERN)


def _prefix_upper(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _validate_bucket_name(bucket: str) -> None:
    if not _BUCKET_RE.match(bucket):
        raise InvalidRequest(f"invalid bucket name {bucket!r}")


def _validate_object_key(key: str) -> None:
    if (
        not _OBJECT_KEY_RE.match(key)
        or key.endswith("/")
        or "//" in key
        or any(seg in {".", ".."} for seg in key.split("/"))
    ):
        raise InvalidRequest(f"invalid object key {key!r}")


class BucketOps(StorageOps):
    def stores(self) -> list[StoreSummary]:
        """List every CAS record store with its head-key count and latest update timestamp."""
        with self._read() as c:
            rows = c.execute(
                "SELECT h.store AS store, COUNT(*) AS records, MAX(r.created_at) AS updated_at"
                " FROM record_heads h"
                " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                " GROUP BY h.store ORDER BY h.store"
            ).fetchall()
        return [
            StoreSummary(
                store=r["store"], records=int(r["records"]), updated_at=float(r["updated_at"])
            )
            for r in rows
        ]

    def list_records(
        self, store: str, *, prefix: str = "", limit: int = 100
    ) -> list[RecordVersion]:
        """List current head records in `store`, optionally filtered by key `prefix`."""
        _validate_bucket_name(store)
        cap = max(1, min(limit, 1000))
        with self._read() as c:
            if prefix:
                rows = c.execute(
                    "SELECT r.* FROM record_heads h"
                    " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                    " WHERE h.store = ? AND h.key >= ? AND h.key < ? ORDER BY h.key LIMIT ?",
                    (store, prefix, _prefix_upper(prefix), cap),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT r.* FROM record_heads h"
                    " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                    " WHERE h.store = ? ORDER BY h.key LIMIT ?",
                    (store, cap),
                ).fetchall()
        return [self._record(r) for r in rows]

    def define_bucket(self, config: BucketConfig) -> BucketInfo:
        """Create or update a named bucket for versioned content-addressed files."""
        now = self.clock.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO buckets (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET description = excluded.description,"
                " updated_at = excluded.updated_at",
                (config.name, config.description, now, now),
            )
            self._event(
                c,
                "bucket_defined",
                now,
                detail={"bucket": config.name, "description": config.description},
            )
            return self._bucket_info(c, config.name)

    def buckets(self) -> list[BucketInfo]:
        """List every defined bucket with its object count and total bytes."""
        with self._read() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM buckets ORDER BY name")]
            return [self._bucket_info(c, name) for name in names]

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> ObjectMeta:
        """Store `data` at `(bucket, key)` using compare-and-set (`expected_version`) and keep history."""
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        clean_type = (content_type or "application/octet-stream").strip()[:255]
        try:
            meta_json = canonical_json(metadata or {})
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"{bucket}/{key}: metadata is not JSON: {exc}") from exc
        if len(meta_json.encode("utf-8")) > 16_384:
            raise InvalidRequest(f"{bucket}/{key}: metadata exceeds 16 KiB")

        now = self.clock.now()
        with self._tx() as c:
            b_row = c.execute("SELECT created_at FROM buckets WHERE name = ?", (bucket,)).fetchone()
            if b_row is None:
                raise NotFound(f"bucket '{bucket}' does not exist")
            head = c.execute(
                "SELECT version, created_at FROM bucket_objects WHERE bucket = ? AND key = ?",
                (bucket, key),
            ).fetchone()
            current = None if head is None else int(head["version"])
            if current != expected_version:
                raise VersionConflict(
                    f"{bucket}/{key}: expected version {expected_version}, head is {current}",
                    current_version=current,
                )
            digest = self._store_blob_in_tx(c, data, now)
            version = (current or 0) + 1
            created_at = now if head is None else float(head["created_at"])
            c.execute(
                "INSERT INTO bucket_object_versions"
                " (bucket, key, version, sha256, size, content_type, metadata, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bucket, key, version, digest, len(data), clean_type, meta_json, now),
            )
            c.execute(
                "INSERT INTO bucket_objects"
                " (bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (bucket, key) DO UPDATE SET"
                " version = excluded.version, sha256 = excluded.sha256, size = excluded.size,"
                " content_type = excluded.content_type, metadata = excluded.metadata,"
                " updated_at = excluded.updated_at",
                (bucket, key, version, digest, len(data), clean_type, meta_json, created_at, now),
            )
            c.execute("UPDATE buckets SET updated_at = ? WHERE name = ?", (now, bucket))
            self._event(
                c,
                "object_stored",
                now,
                detail={
                    "bucket": bucket,
                    "key": key,
                    "version": version,
                    "sha256": digest,
                    "size": len(data),
                },
            )
        return ObjectMeta(
            bucket=bucket,
            key=key,
            version=version,
            sha256=digest,
            size=len(data),
            content_type=clean_type,
            metadata=dict(metadata or {}),
            created_at=created_at,
            updated_at=now,
        )

    def get_object_meta(self, bucket: str, key: str, version: int | None = None) -> ObjectMeta:
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        with self._read() as c:
            if version is None:
                row = c.execute(
                    "SELECT * FROM bucket_objects WHERE bucket = ? AND key = ?", (bucket, key)
                ).fetchone()
                if row is None:
                    raise NotFound(f"object {bucket}/{key} does not exist")
                return self._object_meta(row, updated_at=float(row["updated_at"]))
            v_row = c.execute(
                "SELECT * FROM bucket_object_versions WHERE bucket = ? AND key = ? AND version = ?",
                (bucket, key, version),
            ).fetchone()
            if v_row is None:
                raise NotFound(f"object {bucket}/{key} has no version {version}")
            return self._object_meta(v_row, updated_at=float(v_row["created_at"]))

    def get_object(
        self, bucket: str, key: str, version: int | None = None
    ) -> tuple[ObjectMeta, bytes]:
        meta = self.get_object_meta(bucket, key, version)
        return meta, self.get_blob(meta.sha256)

    def list_objects(self, bucket: str, *, prefix: str = "", limit: int = 100) -> list[ObjectMeta]:
        _validate_bucket_name(bucket)
        cap = max(1, min(limit, 1000))
        with self._read() as c:
            if c.execute("SELECT 1 FROM buckets WHERE name = ?", (bucket,)).fetchone() is None:
                raise NotFound(f"bucket '{bucket}' does not exist")
            if prefix:
                rows = c.execute(
                    "SELECT * FROM bucket_objects WHERE bucket = ? AND key >= ? AND key < ?"
                    " ORDER BY key LIMIT ?",
                    (bucket, prefix, _prefix_upper(prefix), cap),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM bucket_objects WHERE bucket = ? ORDER BY key LIMIT ?",
                    (bucket, cap),
                ).fetchall()
        return [self._object_meta(r, updated_at=float(r["updated_at"])) for r in rows]

    def object_history(self, bucket: str, key: str) -> list[ObjectMeta]:
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM bucket_object_versions WHERE bucket = ? AND key = ?"
                " ORDER BY version",
                (bucket, key),
            ).fetchall()
        if not rows:
            raise NotFound(f"object {bucket}/{key} does not exist")
        return [self._object_meta(r, updated_at=float(r["created_at"])) for r in rows]

    @staticmethod
    def _bucket_info(c: sqlite3.Connection, name: str) -> BucketInfo:
        b = c.execute("SELECT * FROM buckets WHERE name = ?", (name,)).fetchone()
        if b is None:
            raise NotFound(f"bucket '{name}' does not exist")
        agg = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total"
            " FROM bucket_objects WHERE bucket = ?",
            (name,),
        ).fetchone()
        return BucketInfo(
            name=b["name"],
            description=b["description"],
            objects=int(agg["n"]),
            bytes=int(agg["total"]),
            created_at=float(b["created_at"]),
            updated_at=float(b["updated_at"]),
        )

    @staticmethod
    def _object_meta(row: sqlite3.Row, *, updated_at: float) -> ObjectMeta:
        return ObjectMeta(
            bucket=row["bucket"],
            key=row["key"],
            version=int(row["version"]),
            sha256=row["sha256"],
            size=int(row["size"]),
            content_type=row["content_type"],
            metadata=json.loads(row["metadata"]),
            created_at=float(row["created_at"]),
            updated_at=updated_at,
        )
