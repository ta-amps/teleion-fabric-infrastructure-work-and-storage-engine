"""Atomic storage (SPEC §6): versioned records written by single-statement SQL compare-and-set,
with every version kept, namespace isolation, Layer 1 governance lens, and content-addressed blobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._base import StoreBase
from tessera._publication_guard import publication_proof
from tessera.errors import Forbidden, InvalidRequest, NotFound, TesseraError, VersionConflict
from tessera.models import (
    RecordVersion,
    RecordWrite,
    VisibilityScope,
    can_view_governed,
    normalize_cohorts,
    normalize_surface,
    normalize_surface_list,
    normalize_tags,
)
from tessera.receipts import blob_hash, canonical_json

MAX_BLOB_BYTES = 16 * 1024 * 1024


def extract_payload_tags(obj: Any) -> list[str]:
    """Extract searchable tag strings from a structured record value or object metadata dict."""
    if not isinstance(obj, dict):
        return []
    out: list[str] = []
    for k in ("domain", "surface", "stage", "kind", "category", "area", "track"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            clean = v.strip()
            out.append(clean)
            if k == "category" and not clean.lower().startswith("category:"):
                out.append(f"category:{clean}")
    raw_tags = obj.get("tags")
    if isinstance(raw_tags, list):
        out.extend(str(t).strip() for t in raw_tags if str(t).strip())
    elif isinstance(raw_tags, dict):
        out.extend(f"{k}:{v}" for k, v in sorted(raw_tags.items()))
    meta_tags = obj.get("metadata_tags")
    if isinstance(meta_tags, dict):
        out.extend(f"{k}:{v}" for k, v in sorted(meta_tags.items()))
    return normalize_tags(out)


class StorageOps(StoreBase):
    def put_record(
        self,
        store: str,
        key: str,
        value: Any,
        *,
        expected_version: int | None,
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        owner_key_id: str | None = None,
    ) -> RecordVersion:
        """Write one record if its head is still `expected_version` (None or 0: the key must be new)."""
        ns = self._resolve_ns(namespace)
        prod, owner = self._resolve_write_governance(
            producer_surface=producer_surface, owner_key_id=owner_key_id
        )
        write = RecordWrite(
            store=store,
            key=key,
            value=value,
            expected_version=expected_version,
            namespace=ns,
            producer_surface=prod,
            visibility_scope=visibility_scope,
            allowed_surfaces=list(allowed_surfaces) if allowed_surfaces is not None else [],
            target_cohorts=list(target_cohorts) if target_cohorts is not None else ["all"],
            tags=list(tags) if tags is not None else [],
            owner_key_id=owner,
        )
        return self.put_records([write], namespace=ns)[0]

    def put_records(
        self, writes: Sequence[RecordWrite], *, namespace: str | None = None
    ) -> list[RecordVersion]:
        """Apply every write atomically or none: one conflict rolls the whole batch back."""
        if not writes:
            raise InvalidRequest("a batch needs at least one write")
        now = self.clock.now()
        written: list[RecordVersion] = []
        with self._tx() as c:
            batch_ns = self._resolve_ns(namespace)
            for w in writes:
                ctx = self.current_scope()
                if ctx.anonymous:
                    if not w.store.startswith("demo."):
                        raise Forbidden("anonymous writes require a demo store")
                    w = w.model_copy(
                        update={
                            "visibility_scope": "private",
                            "producer_surface": "surface:tessera",
                            "owner_key_id": ctx.key_id,
                            "allowed_surfaces": [],
                            "target_cohorts": ["all"],
                            "tags": ["origin:anonymous", "demo:true", "untrusted:true"],
                        }
                    )
                if w.expected_version is not None and w.expected_version < 0:
                    raise InvalidRequest(
                        f"{w.store}/{w.key}: expected_version must be >= 0 or None"
                    )
                ns = self._resolve_ns(namespace, model_ns=w.namespace)
                batch_ns = ns
                previous = c.execute(
                    "SELECT * FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
                    (ns, w.store, w.key),
                ).fetchone()
                self._authorize_mutation(previous)
                if self.current_scope().key_id and not self.current_scope().is_admin:
                    publication_proof(c, ns, w.store, w.value)
                prod, owner = self._resolve_write_governance(
                    producer_surface=w.producer_surface,
                    owner_key_id=w.owner_key_id,
                )
                vis: VisibilityScope = w.visibility_scope
                allowed = normalize_surface_list(w.allowed_surfaces)
                cohorts = normalize_cohorts(w.target_cohorts)
                effective_tags = normalize_tags([*w.tags, *extract_payload_tags(w.value)])
                self._assert_publisher_tags(effective_tags)

                try:
                    text = canonical_json(w.value)
                except (TypeError, ValueError) as exc:
                    raise InvalidRequest(f"{w.store}/{w.key}: value is not JSON: {exc}") from exc

                allowed_json = canonical_json(allowed)
                cohorts_json = canonical_json(cohorts)
                tags_json = canonical_json(effective_tags)

                if w.expected_version in (None, 0):
                    cur = c.execute(
                        "INSERT OR IGNORE INTO record_heads"
                        " (namespace, store, key, version, producer_surface, visibility_scope,"
                        "  allowed_surfaces, target_cohorts, tags, owner_key_id, updated_at)"
                        " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            ns,
                            w.store,
                            w.key,
                            prod,
                            vis,
                            allowed_json,
                            cohorts_json,
                            tags_json,
                            owner,
                            now,
                        ),
                    )
                    if cur.rowcount != 1:
                        head = c.execute(
                            "SELECT version FROM record_heads"
                            " WHERE namespace = ? AND store = ? AND key = ?",
                            (ns, w.store, w.key),
                        ).fetchone()
                        current = None if head is None else int(head["version"])
                        raise VersionConflict(
                            f"{w.store}/{w.key}: expected version {w.expected_version}, head is {current}",
                            current_version=current,
                        )
                    version = 1
                else:
                    assert w.expected_version is not None
                    cur = c.execute(
                        "UPDATE record_heads SET version = version + 1,"
                        " producer_surface = ?, visibility_scope = ?, allowed_surfaces = ?,"
                        " target_cohorts = ?, tags = ?, owner_key_id = ?, updated_at = ?"
                        " WHERE namespace = ? AND store = ? AND key = ? AND version = ?",
                        (
                            prod,
                            vis,
                            allowed_json,
                            cohorts_json,
                            tags_json,
                            owner,
                            now,
                            ns,
                            w.store,
                            w.key,
                            w.expected_version,
                        ),
                    )
                    if cur.rowcount != 1:
                        head = c.execute(
                            "SELECT version FROM record_heads"
                            " WHERE namespace = ? AND store = ? AND key = ?",
                            (ns, w.store, w.key),
                        ).fetchone()
                        current = None if head is None else int(head["version"])
                        raise VersionConflict(
                            f"{w.store}/{w.key}: expected version {w.expected_version}, head is {current}",
                            current_version=current,
                        )
                    version = w.expected_version + 1

                c.execute(
                    "INSERT INTO records"
                    " (namespace, store, key, version, value, producer_surface, visibility_scope,"
                    "  allowed_surfaces, target_cohorts, tags, owner_key_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        w.store,
                        w.key,
                        version,
                        text,
                        prod,
                        vis,
                        allowed_json,
                        cohorts_json,
                        tags_json,
                        owner,
                        now,
                    ),
                )
                self._sync_noun_tags(
                    c,
                    namespace=ns,
                    noun_type="record",
                    container=w.store,
                    record_id=w.key,
                    tags=effective_tags,
                )
                written.append(
                    RecordVersion(
                        namespace=ns,
                        store=w.store,
                        key=w.key,
                        version=version,
                        value=w.value,
                        producer_surface=prod,
                        visibility_scope=vis,
                        allowed_surfaces=allowed,
                        target_cohorts=cohorts,
                        tags=effective_tags,
                        owner_key_id=owner,
                        created_at=now,
                    )
                )
            self._event(
                c,
                "records_written",
                now,
                namespace=batch_ns,
                detail={
                    "writes": [
                        {"store": r.store, "key": r.key, "version": r.version} for r in written
                    ]
                },
            )
        return written

    def get_record(
        self,
        store: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> RecordVersion:
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        with self._read() as c:
            head = c.execute(
                "SELECT version, tags, producer_surface FROM record_heads"
                " WHERE namespace = ? AND store = ? AND key = ?",
                (ns, store, key),
            ).fetchone()
            if head is None:
                raise NotFound(f"record {store}/{key} does not exist")
            target_version = int(head["version"]) if version is None else version
            row = c.execute(
                "SELECT * FROM records WHERE namespace = ? AND store = ? AND key = ? AND version = ?",
                (ns, store, key, target_version),
            ).fetchone()
        if row is None:
            raise NotFound(f"record {store}/{key} has no version {target_version}")
        rec = self._record(row)
        head_tags = json.loads(head["tags"])
        combined_tags = normalize_tags([*rec.tags, *head_tags])
        if not can_view_governed(
            visibility_scope=rec.visibility_scope,
            producer_surface=rec.producer_surface,
            allowed_surfaces=rec.allowed_surfaces,
            owner_key_id=rec.owner_key_id,
            tags=combined_tags,
            viewer_surface=ctx.surface,
            viewer_key_id=ctx.key_id,
            is_admin=ctx.is_admin,
            direct_get=True,
        ):
            raise NotFound(f"record {store}/{key} does not exist")
        return rec

    def record_history(
        self, store: str, key: str, *, namespace: str | None = None
    ) -> list[RecordVersion]:
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        with self._read() as c:
            head = c.execute(
                "SELECT tags FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
                (ns, store, key),
            ).fetchone()
            if head is None:
                raise NotFound(f"record {store}/{key} does not exist")
            rows = c.execute(
                "SELECT * FROM records WHERE namespace = ? AND store = ? AND key = ? ORDER BY version",
                (ns, store, key),
            ).fetchall()
        if not rows:
            raise NotFound(f"record {store}/{key} does not exist")
        head_tags = json.loads(head["tags"])
        visible: list[RecordVersion] = []
        for r in rows:
            rec = self._record(r)
            combined_tags = normalize_tags([*rec.tags, *head_tags])
            if can_view_governed(
                visibility_scope=rec.visibility_scope,
                producer_surface=rec.producer_surface,
                allowed_surfaces=rec.allowed_surfaces,
                owner_key_id=rec.owner_key_id,
                tags=combined_tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=True,
            ):
                visible.append(rec)
        if not visible:
            raise NotFound(f"record {store}/{key} does not exist")
        return visible

    def _store_blob_in_tx(
        self,
        c: sqlite3.Connection,
        data: bytes,
        now: float,
        *,
        namespace: str = "main",
        producer_surface: str = "surface:tessera",
        visibility_scope: str = "public_commons",
        allowed_surfaces: Sequence[str] = (),
        target_cohorts: Sequence[str] = ("all",),
        tags: Sequence[str] = (),
        owner_key_id: str | None = None,
    ) -> str:
        self._assert_publisher_tags(list(tags or ()))
        if len(data) > MAX_BLOB_BYTES:
            raise InvalidRequest(f"blob is over {MAX_BLOB_BYTES} bytes")
        digest = blob_hash(data)
        inline_bytes: bytes = data
        if self._blob_dir is not None:
            subdir = self._blob_dir / digest[:2]
            subdir.mkdir(parents=True, exist_ok=True)
            target = subdir / digest
            if not target.exists():
                tmp = subdir / f".{digest}.tmp"
                tmp.write_bytes(data)
                tmp.replace(target)
            inline_bytes = b""
        cur = c.execute(
            "INSERT OR IGNORE INTO blobs (sha256, data, size, created_at) VALUES (?, ?, ?, ?)",
            (digest, sqlite3.Binary(inline_bytes), len(data), now),
        )
        c.execute(
            "INSERT INTO blob_namespaces"
            " (namespace, sha256, producer_surface, visibility_scope, allowed_surfaces,"
            "  target_cohorts, tags, owner_key_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (namespace, sha256) DO NOTHING",
            (
                namespace,
                digest,
                normalize_surface(producer_surface),
                visibility_scope,
                canonical_json(normalize_surface_list(list(allowed_surfaces))),
                canonical_json(normalize_cohorts(list(target_cohorts))),
                canonical_json(normalize_tags(list(tags))),
                owner_key_id,
                now,
                now,
            ),
        )
        if cur.rowcount:
            self._event(
                c,
                "blob_stored",
                now,
                namespace=namespace,
                detail={"sha256": digest, "size": len(data)},
            )
        return digest

    def _load_blob_data(self, row: sqlite3.Row, sha256: str) -> bytes:
        data = bytes(row["data"])
        expected_size = int(row["size"])
        if not data and expected_size > 0 and self._blob_dir is not None:
            target = self._blob_dir / sha256[:2] / sha256
            if not target.is_file():
                raise NotFound(f"blob {sha256} file is missing from blob_dir")
            data = target.read_bytes()
        if blob_hash(data) != sha256:
            raise TesseraError(f"blob {sha256} failed its hash check")
        return data

    def put_blob(
        self,
        data: bytes,
        *,
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        owner_key_id: str | None = None,
    ) -> str:
        """Store bytes under their SHA-256 and return it. Storing the same bytes twice is a no-op."""
        if len(data) > MAX_BLOB_BYTES:
            raise InvalidRequest(f"blob is over {MAX_BLOB_BYTES} bytes")
        ns = self._resolve_ns(namespace)
        prod, owner = self._resolve_write_governance(
            producer_surface=producer_surface, owner_key_id=owner_key_id
        )
        now = self.clock.now()
        with self._tx() as c:
            return self._store_blob_in_tx(
                c,
                data,
                now,
                namespace=ns,
                producer_surface=prod,
                visibility_scope=visibility_scope,
                allowed_surfaces=allowed_surfaces or (),
                target_cohorts=target_cohorts or ("all",),
                tags=tags or (),
                owner_key_id=owner,
            )

    def get_blob(self, sha256: str, *, namespace: str | None = None) -> bytes:
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        with self._read() as c:
            bn = c.execute(
                "SELECT * FROM blob_namespaces WHERE namespace = ? AND sha256 = ?",
                (ns, sha256),
            ).fetchone()
            if bn is None:
                raise NotFound(f"blob {sha256} does not exist")
            bn_tags: list[str] = json.loads(bn["tags"])
            obj_rows = c.execute(
                "SELECT producer_surface, tags FROM bucket_objects"
                " WHERE namespace = ? AND sha256 = ?",
                (ns, sha256),
            ).fetchall()
            for obj_r in obj_rows:
                obj_tags: list[str] = json.loads(obj_r["tags"])
                if "status:quarantined" in obj_tags:
                    viewer_ok = ctx.is_admin or (
                        ctx.surface is not None
                        and normalize_surface(ctx.surface)
                        == normalize_surface(obj_r["producer_surface"])
                    )
                    if not viewer_ok:
                        raise NotFound(f"blob {sha256} does not exist")
            if not can_view_governed(
                visibility_scope=str(bn["visibility_scope"]),
                producer_surface=str(bn["producer_surface"]),
                allowed_surfaces=json.loads(bn["allowed_surfaces"]),
                owner_key_id=bn["owner_key_id"],
                tags=bn_tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=True,
            ):
                raise NotFound(f"blob {sha256} does not exist")
            row = c.execute("SELECT data, size FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            raise NotFound(f"blob {sha256} does not exist")
        return self._load_blob_data(row, sha256)

    @staticmethod
    def _record(row: sqlite3.Row) -> RecordVersion:
        return RecordVersion(
            namespace=row["namespace"],
            store=row["store"],
            key=row["key"],
            version=row["version"],
            value=json.loads(row["value"]),
            producer_surface=row["producer_surface"],
            visibility_scope=row["visibility_scope"],
            allowed_surfaces=json.loads(row["allowed_surfaces"]),
            target_cohorts=json.loads(row["target_cohorts"]),
            tags=json.loads(row["tags"]),
            owner_key_id=row["owner_key_id"],
            created_at=row["created_at"],
        )
