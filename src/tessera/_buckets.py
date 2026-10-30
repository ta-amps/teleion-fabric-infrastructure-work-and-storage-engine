"""Queryable noun stores and persistent named file buckets (SPEC §6 extension).

Agents joining a shared fabric can list stores, query head records by prefix and governance/tag filters,
define named buckets (`nouns`, `verbs`, `kits`, `cases`, `provisions`, `traces`), and read/write versioned
content-addressed files with single-statement SQL compare-and-set (`expected_version`) and complete version
histories isolated per namespace.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._storage import StorageOps, extract_payload_tags, noun_filter_sql
from tessera.errors import Forbidden, InvalidRequest, NotFound, VersionConflict
from tessera.models import (
    BUCKET_KEY_PATTERN,
    NAME_PATTERN,
    BucketConfig,
    BucketInfo,
    ObjectMeta,
    RecordVersion,
    StoreSummary,
    VisibilityScope,
    can_view_governed,
    canonical_category_tags,
    category_from_tag,
    cohort_matches,
    normalize_cohorts,
    normalize_surface,
    normalize_surface_list,
    tag_matches_category,
)
from tessera.receipts import canonical_json

_BUCKET_RE = re.compile(NAME_PATTERN)
_OBJECT_KEY_RE = re.compile(BUCKET_KEY_PATTERN)
_FILTER_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


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


def matches_layer2_filters(
    *,
    haystack: str,
    tags: Sequence[str],
    producer_surface: str,
    target_cohorts: Sequence[str],
    q: str = "",
    tag: str | None = None,
    category: str | None = None,
    cohort: str | None = None,
    surface: str | None = None,
    is_admin: bool = False,
    include_restricted: bool = False,
) -> bool:
    if not cohort_matches(
        list(target_cohorts), cohort, is_admin=is_admin, include_restricted=include_restricted
    ):
        return False
    if (
        surface
        and surface.strip()
        and normalize_surface(producer_surface) != normalize_surface(surface)
    ):
        return False
    if tag and tag.strip():
        cat_tag = category_from_tag(tag)
        want_tag = f"category:{cat_tag}" if cat_tag else tag.strip().lower()
        if all(t.strip().lower() != want_tag for t in tags):
            return False
    if category and category.strip() and not tag_matches_category(tags, category):
        return False
    q_clean = q.strip().lower()
    if q_clean:
        hay_low = f"{haystack} {' '.join(tags)}".lower()
        if q_clean not in hay_low:
            q_toks = set(_FILTER_TOKEN_RE.findall(q_clean))
            h_toks = set(_FILTER_TOKEN_RE.findall(hay_low))
            if not q_toks or not (q_toks & h_toks):
                return False
    return True


class BucketOps(StorageOps):
    def stores(self, *, namespace: str | None = None) -> list[StoreSummary]:
        """List every CAS record store in `namespace` with its visible head-key count and latest timestamp."""
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        with self._read() as c:
            rows = c.execute(
                "SELECT h.store AS store, r.created_at AS created_at,"
                " h.visibility_scope, h.producer_surface, h.allowed_surfaces,"
                " h.owner_key_id, h.tags, h.target_cohorts"
                " FROM record_heads h"
                " JOIN records r"
                "   ON r.namespace = h.namespace AND r.store = h.store"
                "  AND r.key = h.key AND r.version = h.version"
                " WHERE h.namespace = ?"
                " ORDER BY h.store",
                (ns,),
            ).fetchall()
        grouped: dict[str, tuple[int, float]] = {}
        for r in rows:
            tags_list = json.loads(r["tags"])
            if not can_view_governed(
                visibility_scope=r["visibility_scope"],
                producer_surface=r["producer_surface"],
                allowed_surfaces=json.loads(r["allowed_surfaces"]),
                owner_key_id=r["owner_key_id"],
                tags=tags_list,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
            ):
                continue
            if not cohort_matches(
                json.loads(r["target_cohorts"]),
                None,
                is_admin=ctx.is_admin,
                include_restricted=False,
            ):
                continue
            st = str(r["store"])
            ts = float(r["created_at"])
            prev_n, prev_ts = grouped.get(st, (0, 0.0))
            grouped[st] = (prev_n + 1, max(prev_ts, ts))
        return [
            StoreSummary(store=st, records=n, updated_at=ts)
            for st, (n, ts) in sorted(grouped.items())
        ]

    def list_records(
        self,
        store: str,
        *,
        prefix: str = "",
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[RecordVersion]:
        """List current head records in `store`, applying Layer 1 visibility and Layer 2 filters."""
        _validate_bucket_name(store)
        cap = max(1, min(limit, 1000))
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        has_filters = bool(q.strip() or tag or category or cohort or surface or include_restricted)
        scan_cap = max(cap, 1000) if has_filters else max(cap * 2, 200)
        with self._read() as c:
            params: list[Any] = [ns, store]
            where = "WHERE h.namespace = ? AND h.store = ?"
            if prefix:
                where += " AND h.key >= ? AND h.key < ?"
                params.extend([prefix, _prefix_upper(prefix)])
            predicate, filter_params = noun_filter_sql("record", tag=tag, category=category)
            where += predicate
            params.extend(filter_params)
            params.append(scan_cap)
            rows = c.execute(
                "SELECT r.* FROM record_heads h"  # noqa: S608
                " JOIN records r"
                "   ON r.namespace = h.namespace AND r.store = h.store"
                "  AND r.key = h.key AND r.version = h.version "
                f"{where} ORDER BY h.key LIMIT ?",
                params,
            ).fetchall()

        out: list[RecordVersion] = []
        for r in rows:
            rec = self._record(r)
            rec = rec.model_copy(
                update={
                    "tags": canonical_category_tags(
                        rec.value, [*rec.tags, *extract_payload_tags(rec.value)]
                    )
                }
            )
            if not can_view_governed(
                visibility_scope=rec.visibility_scope,
                producer_surface=rec.producer_surface,
                allowed_surfaces=rec.allowed_surfaces,
                owner_key_id=rec.owner_key_id,
                tags=rec.tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
                include_restricted=include_restricted,
            ):
                continue
            if not matches_layer2_filters(
                haystack=f"{rec.store} {rec.key} {r['value']}",
                tags=rec.tags,
                producer_surface=rec.producer_surface,
                target_cohorts=rec.target_cohorts,
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                is_admin=ctx.is_admin,
                include_restricted=include_restricted,
            ):
                continue
            out.append(rec)
            if len(out) >= cap:
                break
        return out

    def define_bucket(self, config: BucketConfig, *, namespace: str | None = None) -> BucketInfo:
        """Create or update a named bucket for versioned content-addressed files."""
        if self.current_scope().surface == "surface:frontier":
            raise Forbidden(
                "Frontier uses versioned records; this backend has no bucket governance"
            )
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO buckets (namespace, name, description, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (namespace, name) DO UPDATE SET description = excluded.description,"
                " updated_at = excluded.updated_at",
                (ns, config.name, config.description, now, now),
            )
            self._event(
                c,
                "bucket_defined",
                now,
                namespace=ns,
                detail={"bucket": config.name, "description": config.description},
            )
            return self._bucket_info(c, ns, config.name)

    def buckets(self, *, namespace: str | None = None) -> list[BucketInfo]:
        """List every defined bucket in `namespace` with its object count and total bytes."""
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            names = [
                r["name"]
                for r in c.execute(
                    "SELECT name FROM buckets WHERE namespace = ? ORDER BY name", (ns,)
                )
            ]
            return [self._bucket_info(c, ns, name) for name in names]

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        owner_key_id: str | None = None,
    ) -> ObjectMeta:
        """Store `data` at `(bucket, key)` using single-statement SQL CAS (`expected_version`) and keep history."""
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        if expected_version is not None and expected_version < 0:
            raise InvalidRequest(f"{bucket}/{key}: expected_version must be >= 0 or None")
        clean_type = (content_type or "application/octet-stream").strip()[:255]
        try:
            meta_json = canonical_json(metadata or {})
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"{bucket}/{key}: metadata is not JSON: {exc}") from exc
        if len(meta_json.encode("utf-8")) > 16_384:
            raise InvalidRequest(f"{bucket}/{key}: metadata exceeds 16 KiB")

        ns = self._resolve_ns(namespace)
        prod, owner = self._resolve_write_governance(
            producer_surface=producer_surface, owner_key_id=owner_key_id
        )
        vis: VisibilityScope = visibility_scope
        self._assert_frontier_governance(prod, vis)
        allowed = normalize_surface_list(list(allowed_surfaces) if allowed_surfaces else [])
        cohorts = normalize_cohorts(list(target_cohorts) if target_cohorts else ["all"])
        try:
            effective_tags = canonical_category_tags(
                metadata or {}, [*(tags or ()), *extract_payload_tags(metadata or {})], strict=True
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self._assert_publisher_tags(effective_tags)
        allowed_json = canonical_json(allowed)
        cohorts_json = canonical_json(cohorts)
        tags_json = canonical_json(effective_tags)

        now = self.clock.now()
        with self._tx() as c:
            b_row = c.execute(
                "SELECT created_at FROM buckets WHERE namespace = ? AND name = ?",
                (ns, bucket),
            ).fetchone()
            if b_row is None:
                raise NotFound(f"bucket '{bucket}' does not exist")

            digest = self._store_blob_in_tx(
                c,
                data,
                now,
                namespace=ns,
                producer_surface=prod,
                visibility_scope=vis,
                allowed_surfaces=allowed,
                target_cohorts=cohorts,
                tags=effective_tags,
                owner_key_id=owner,
            )

            previous = c.execute(
                "SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                (ns, bucket, key),
            ).fetchone()
            self._authorize_mutation(previous)
            if expected_version in (None, 0):
                cur = c.execute(
                    "INSERT OR IGNORE INTO bucket_objects"
                    " (namespace, bucket, key, version, sha256, size, content_type, metadata,"
                    "  producer_surface, visibility_scope, allowed_surfaces, target_cohorts,"
                    "  tags, owner_key_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        bucket,
                        key,
                        digest,
                        len(data),
                        clean_type,
                        meta_json,
                        prod,
                        vis,
                        allowed_json,
                        cohorts_json,
                        tags_json,
                        owner,
                        now,
                        now,
                    ),
                )
                if cur.rowcount != 1:
                    head = c.execute(
                        "SELECT version FROM bucket_objects"
                        " WHERE namespace = ? AND bucket = ? AND key = ?",
                        (ns, bucket, key),
                    ).fetchone()
                    current = None if head is None else int(head["version"])
                    raise VersionConflict(
                        f"{bucket}/{key}: expected version {expected_version}, head is {current}",
                        current_version=current,
                    )
                version = 1
                created_at = now
            else:
                assert expected_version is not None
                cur = c.execute(
                    "UPDATE bucket_objects SET version = version + 1,"
                    " sha256 = ?, size = ?, content_type = ?, metadata = ?,"
                    " producer_surface = ?, visibility_scope = ?, allowed_surfaces = ?,"
                    " target_cohorts = ?, tags = ?, owner_key_id = ?, updated_at = ?"
                    " WHERE namespace = ? AND bucket = ? AND key = ? AND version = ?",
                    (
                        digest,
                        len(data),
                        clean_type,
                        meta_json,
                        prod,
                        vis,
                        allowed_json,
                        cohorts_json,
                        tags_json,
                        owner,
                        now,
                        ns,
                        bucket,
                        key,
                        expected_version,
                    ),
                )
                if cur.rowcount != 1:
                    head = c.execute(
                        "SELECT version FROM bucket_objects"
                        " WHERE namespace = ? AND bucket = ? AND key = ?",
                        (ns, bucket, key),
                    ).fetchone()
                    current = None if head is None else int(head["version"])
                    raise VersionConflict(
                        f"{bucket}/{key}: expected version {expected_version}, head is {current}",
                        current_version=current,
                    )
                version = expected_version + 1
                c_row = c.execute(
                    "SELECT created_at FROM bucket_objects"
                    " WHERE namespace = ? AND bucket = ? AND key = ?",
                    (ns, bucket, key),
                ).fetchone()
                created_at = now if c_row is None else float(c_row["created_at"])

            c.execute(
                "INSERT INTO bucket_object_versions"
                " (namespace, bucket, key, version, sha256, size, content_type, metadata,"
                "  producer_surface, visibility_scope, allowed_surfaces, target_cohorts,"
                "  tags, owner_key_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ns,
                    bucket,
                    key,
                    version,
                    digest,
                    len(data),
                    clean_type,
                    meta_json,
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
                noun_type="object",
                container=bucket,
                record_id=key,
                tags=effective_tags,
            )
            c.execute(
                "UPDATE buckets SET updated_at = ? WHERE namespace = ? AND name = ?",
                (now, ns, bucket),
            )
            self._event(
                c,
                "object_stored",
                now,
                namespace=ns,
                detail={
                    "bucket": bucket,
                    "key": key,
                    "version": version,
                    "sha256": digest,
                    "size": len(data),
                },
            )
        return ObjectMeta(
            namespace=ns,
            bucket=bucket,
            key=key,
            version=version,
            sha256=digest,
            size=len(data),
            content_type=clean_type,
            metadata=dict(metadata or {}),
            producer_surface=prod,
            visibility_scope=vis,
            allowed_surfaces=allowed,
            target_cohorts=cohorts,
            tags=effective_tags,
            owner_key_id=owner,
            created_at=created_at,
            updated_at=now,
        )

    def get_object_meta(
        self,
        bucket: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> ObjectMeta:
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            head = c.execute(
                "SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                (ns, bucket, key),
            ).fetchone()
            if head is None:
                raise NotFound(f"object {bucket}/{key} does not exist")
            if version is None:
                meta = self._object_meta(head, updated_at=float(head["updated_at"]))
            else:
                v_row = c.execute(
                    "SELECT * FROM bucket_object_versions"
                    " WHERE namespace = ? AND bucket = ? AND key = ? AND version = ?",
                    (ns, bucket, key, version),
                ).fetchone()
                if v_row is None:
                    raise NotFound(f"object {bucket}/{key} has no version {version}")
                meta = self._object_meta(v_row, updated_at=float(v_row["created_at"]))
        if not self._can_read_version(head if version is None else v_row, head):
            raise NotFound(f"object {bucket}/{key} does not exist")
        return meta

    def get_object(
        self,
        bucket: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> tuple[ObjectMeta, bytes]:
        ns = self._resolve_ns(namespace)
        meta = self.get_object_meta(bucket, key, version, namespace=ns)
        # The object's ACL governs its bytes, whoever registered those bytes first.
        return meta, self._read_blob(meta.sha256, ns, raw=False)

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[ObjectMeta]:
        _validate_bucket_name(bucket)
        cap = max(1, min(limit, 1000))
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        has_filters = bool(q.strip() or tag or category or cohort or surface or include_restricted)
        scan_cap = max(cap, 1000) if has_filters else max(cap * 2, 200)
        with self._read() as c:
            if (
                c.execute(
                    "SELECT 1 FROM buckets WHERE namespace = ? AND name = ?", (ns, bucket)
                ).fetchone()
                is None
            ):
                raise NotFound(f"bucket '{bucket}' does not exist")
            params: list[Any] = [ns, bucket]
            where = "WHERE namespace = ? AND bucket = ?"
            if prefix:
                where += " AND key >= ? AND key < ?"
                params.extend([prefix, _prefix_upper(prefix)])
            predicate, filter_params = noun_filter_sql("object", tag=tag, category=category)
            where += predicate
            params.extend(filter_params)
            params.append(scan_cap)
            rows = c.execute(
                f"SELECT * FROM bucket_objects {where} ORDER BY key LIMIT ?",  # noqa: S608
                params,
            ).fetchall()

        out: list[ObjectMeta] = []
        for r in rows:
            meta = self._object_meta(r, updated_at=float(r["updated_at"]))
            meta = meta.model_copy(
                update={
                    "tags": canonical_category_tags(
                        meta.metadata, [*meta.tags, *extract_payload_tags(meta.metadata)]
                    )
                }
            )
            if not can_view_governed(
                visibility_scope=meta.visibility_scope,
                producer_surface=meta.producer_surface,
                allowed_surfaces=meta.allowed_surfaces,
                owner_key_id=meta.owner_key_id,
                tags=meta.tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
                include_restricted=include_restricted,
            ):
                continue
            if not matches_layer2_filters(
                haystack=f"{meta.bucket} {meta.key} {r['metadata']}",
                tags=meta.tags,
                producer_surface=meta.producer_surface,
                target_cohorts=meta.target_cohorts,
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                is_admin=ctx.is_admin,
                include_restricted=include_restricted,
            ):
                continue
            out.append(meta)
            if len(out) >= cap:
                break
        return out

    def object_history(
        self, bucket: str, key: str, *, namespace: str | None = None
    ) -> list[ObjectMeta]:
        _validate_bucket_name(bucket)
        _validate_object_key(key)
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            head = c.execute(
                "SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                (ns, bucket, key),
            ).fetchone()
            if head is None:
                raise NotFound(f"object {bucket}/{key} does not exist")
            rows = c.execute(
                "SELECT * FROM bucket_object_versions"
                " WHERE namespace = ? AND bucket = ? AND key = ? ORDER BY version",
                (ns, bucket, key),
            ).fetchall()
        if not rows:
            raise NotFound(f"object {bucket}/{key} does not exist")
        visible = [
            self._object_meta(r, updated_at=float(r["created_at"]))
            for r in rows
            if self._can_read_version(r, head)
        ]
        if not visible:
            raise NotFound(f"object {bucket}/{key} does not exist")
        return visible

    def _bucket_info(self, c: sqlite3.Connection, ns: str, name: str) -> BucketInfo:
        b = c.execute(
            "SELECT * FROM buckets WHERE namespace = ? AND name = ?", (ns, name)
        ).fetchone()
        if b is None:
            raise NotFound(f"bucket '{name}' does not exist")
        ctx = self.current_scope()
        obj_rows = c.execute(
            "SELECT size, visibility_scope, producer_surface, allowed_surfaces,"
            " owner_key_id, tags, target_cohorts"
            " FROM bucket_objects WHERE namespace = ? AND bucket = ?",
            (ns, name),
        ).fetchall()
        count = 0
        total_bytes = 0
        for r in obj_rows:
            if not can_view_governed(
                visibility_scope=r["visibility_scope"],
                producer_surface=r["producer_surface"],
                allowed_surfaces=json.loads(r["allowed_surfaces"]),
                owner_key_id=r["owner_key_id"],
                tags=json.loads(r["tags"]),
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
            ):
                continue
            if not cohort_matches(
                json.loads(r["target_cohorts"]),
                None,
                is_admin=ctx.is_admin,
                include_restricted=False,
            ):
                continue
            count += 1
            total_bytes += int(r["size"])
        return BucketInfo(
            name=b["name"],
            description=b["description"],
            objects=count,
            bytes=total_bytes,
            created_at=float(b["created_at"]),
            updated_at=float(b["updated_at"]),
        )

    @staticmethod
    def _object_meta(row: sqlite3.Row, *, updated_at: float) -> ObjectMeta:
        return ObjectMeta(
            namespace=row["namespace"],
            bucket=row["bucket"],
            key=row["key"],
            version=int(row["version"]),
            sha256=row["sha256"],
            size=int(row["size"]),
            content_type=row["content_type"],
            metadata=json.loads(row["metadata"]),
            producer_surface=row["producer_surface"],
            visibility_scope=row["visibility_scope"],
            allowed_surfaces=json.loads(row["allowed_surfaces"]),
            target_cohorts=json.loads(row["target_cohorts"]),
            tags=json.loads(row["tags"]),
            owner_key_id=row["owner_key_id"],
            created_at=float(row["created_at"]),
            updated_at=updated_at,
        )
