"""0-call progressive L0 scent search, monotonic per-(namespace, feed) delta feeds, and dual-resolution reflex recall.

Gives any connected agent (MCP coding harness, HTTPS chat assistant, or CLI worker) fast 0-model-call
discovery over versioned nouns and buckets without loading megabytes into context, monotonic cursor
feeds for live changes, and muscle-memory reflex recall paired with negative-knowledge tombstones,
enforcing namespace isolation, Layer 1 governance visibility, quarantine hiding, and Layer 2 filters.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Any, Literal

from tessera._buckets import BucketOps, _validate_bucket_name, matches_layer2_filters
from tessera._storage import extract_payload_tags
from tessera._version import __version__
from tessera.errors import InvalidRequest
from tessera.models import (
    SURFACE_ID_PATTERN,
    FeedEntry,
    FeedSummary,
    HandshakeRequest,
    HandshakeResponse,
    NounScent,
    ReflexHit,
    ReflexMatch,
    VisibilityScope,
    can_view_governed,
    cohort_matches,
    normalize_cohorts,
    normalize_surface_list,
    normalize_tags,
)
from tessera.receipts import canonical_json

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_SURFACE_RE = re.compile(SURFACE_ID_PATTERN)
MAX_L0_CHARS = 520
MIN_CLAMPED_CHARS = 120
MAX_CLAMPED_CHARS = 16_000
MAX_OUTPUT_BYTES_LIMIT = 1_048_576
HANDSHAKE_CAPABILITIES: tuple[str, ...] = (
    "atomic_queues",
    "cas_stores",
    "named_buckets",
    "http_206_range",
    "l0_scent_search",
    "delta_feeds",
    "reflex_recall",
    "mcp_streamable_http",
    "surface_handshake_v1",
    "governance_envelope_v1",
    "namespace_isolation",
)
REFLEX_STORE = "tessera.reflexes"
DEAD_END_STORE = "negative.knowledge"


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _canonical_sig(text: str) -> str:
    return " ".join(sorted(set(_tokenize(text))))


def _trigrams(text: str) -> set[str]:
    clean = " ".join(_tokenize(text))
    if len(clean) < 3:
        return {clean} if clean else set()
    return {clean[i : i + 3] for i in range(len(clean) - 2)}


def _similarity(query: str, hay_title: str, hay_body: str, hay_tags: list[str]) -> float:
    q_low = query.strip().lower()
    if not q_low:
        return 1.0
    q_tokens = set(_tokenize(q_low))
    full_text = f"{hay_title} {' '.join(hay_tags)} {hay_body}".lower()
    h_tokens = set(_tokenize(full_text))
    tok_overlap = len(q_tokens & h_tokens) / max(1, len(q_tokens)) if q_tokens else 0.0
    substr_bonus = 0.25 if q_low in full_text else 0.0
    title_bonus = (
        0.15
        if q_tokens and (q_tokens & set(_tokenize(f"{hay_title} {' '.join(hay_tags)}")))
        else 0.0
    )
    q_tri = _trigrams(q_low)
    h_tri = _trigrams(f"{hay_title} {' '.join(hay_tags)} {hay_body[:600]}")
    tri_jaccard = len(q_tri & h_tri) / max(1, len(q_tri | h_tri)) if (q_tri and h_tri) else 0.0
    return round(min(1.0, 0.55 * tok_overlap + 0.25 * tri_jaccard + substr_bonus + title_bonus), 4)


def _extract_title_and_l0(key: str, val: Any) -> tuple[str, str]:
    if isinstance(val, dict):
        title = str(
            val.get("title")
            or val.get("name")
            or val.get("kit_id")
            or val.get("prov_id")
            or val.get("seed_id")
            or key
        ).strip()
        for field in ("l0_summary", "summary", "description", "goal", "reason", "pattern"):
            cand = val.get(field)
            if isinstance(cand, str) and cand.strip():
                text = " ".join(cand.split())
                return title[:140], text[:MAX_L0_CHARS]
        keys_preview = ", ".join(sorted(str(k) for k in val)[:10])
        compact = canonical_json(val)
        snippet = compact[:260] + ("..." if len(compact) > 260 else "")
        return title[:140], f"fields=[{keys_preview}] {snippet}"[:MAX_L0_CHARS]
    compact_any = canonical_json(val)
    return key[:140], compact_any[:MAX_L0_CHARS]


class IntelligenceOps(BucketOps):
    def search_nouns(
        self,
        query: str = "",
        *,
        store: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[NounScent]:
        """0-call progressive L0 scent search across head CAS records and versioned bucket files."""
        cap = max(1, min(limit, 200))
        if store is not None:
            _validate_bucket_name(store)
        if bucket is not None:
            _validate_bucket_name(bucket)

        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        scents: list[NounScent] = []
        q_clean = query.strip()

        with self._read() as c:
            if bucket is None:
                rec_where = "WHERE h.namespace = ?"
                rec_params: list[Any] = [ns]
                if store is not None:
                    rec_where += " AND h.store = ?"
                    rec_params.append(store)
                if tag and tag.strip():
                    rec_where += (
                        " AND EXISTS (SELECT 1 FROM noun_tags nt"
                        " WHERE nt.namespace = h.namespace AND nt.noun_type = 'record'"
                        " AND nt.container = h.store AND nt.record_id = h.key"
                        " AND LOWER(nt.tag) = LOWER(?))"
                    )
                    rec_params.append(tag.strip())
                rec_rows = c.execute(
                    "SELECT r.* FROM record_heads h"  # noqa: S608
                    " JOIN records r"
                    "   ON r.namespace = h.namespace AND r.store = h.store"
                    "  AND r.key = h.key AND r.version = h.version "
                    f"{rec_where} ORDER BY r.created_at DESC LIMIT 1000",
                    rec_params,
                ).fetchall()
                for row in rec_rows:
                    raw_val: str = row["value"]
                    parsed = json.loads(raw_val)
                    env_tags: list[str] = json.loads(row["tags"])
                    tags = normalize_tags([*env_tags, *extract_payload_tags(parsed)])
                    # Quarantined records NEVER appear in /v1/search.
                    if "status:quarantined" in tags:
                        continue
                    allowed = json.loads(row["allowed_surfaces"])
                    cohorts = json.loads(row["target_cohorts"])
                    prod = str(row["producer_surface"])
                    vis: VisibilityScope = row["visibility_scope"]
                    owner = row["owner_key_id"]
                    if not can_view_governed(
                        visibility_scope=vis,
                        producer_surface=prod,
                        allowed_surfaces=allowed,
                        owner_key_id=owner,
                        tags=tags,
                        viewer_surface=ctx.surface,
                        viewer_key_id=ctx.key_id,
                        is_admin=ctx.is_admin,
                        direct_get=False,
                        include_restricted=False,
                    ):
                        continue
                    title, l0 = _extract_title_and_l0(row["key"], parsed)
                    if not matches_layer2_filters(
                        haystack=f"{row['store']} {row['key']} {title} {l0} {raw_val[:1200]}",
                        tags=tags,
                        producer_surface=prod,
                        target_cohorts=cohorts,
                        q="",
                        tag=tag,
                        category=category,
                        cohort=cohort,
                        surface=surface,
                        is_admin=ctx.is_admin,
                        include_restricted=include_restricted,
                    ):
                        continue
                    score = _similarity(
                        q_clean,
                        f"{row['store']} {row['key']} {title}",
                        f"{l0} {raw_val[:1200]}",
                        tags,
                    )
                    if q_clean and score <= 0.05:
                        continue
                    scents.append(
                        NounScent(
                            namespace=ns,
                            kind="record",
                            container=str(row["store"]),
                            key=row["key"],
                            version=int(row["version"]),
                            title=title,
                            l0_summary=l0,
                            producer_surface=prod,
                            visibility_scope=vis,
                            allowed_surfaces=allowed,
                            target_cohorts=cohorts,
                            tags=tags,
                            owner_key_id=owner,
                            size_bytes=len(raw_val.encode("utf-8")),
                            score=score,
                            updated_at=float(row["created_at"]),
                        )
                    )

            if store is None:
                obj_where = "WHERE namespace = ?"
                obj_params: list[Any] = [ns]
                if bucket is not None:
                    obj_where += " AND bucket = ?"
                    obj_params.append(bucket)
                if tag and tag.strip():
                    obj_where += (
                        " AND EXISTS (SELECT 1 FROM noun_tags nt"
                        " WHERE nt.namespace = bucket_objects.namespace"
                        " AND nt.noun_type = 'object'"
                        " AND nt.container = bucket_objects.bucket"
                        " AND nt.record_id = bucket_objects.key"
                        " AND LOWER(nt.tag) = LOWER(?))"
                    )
                    obj_params.append(tag.strip())
                obj_rows = c.execute(
                    f"SELECT * FROM bucket_objects {obj_where} ORDER BY updated_at DESC LIMIT 1000",  # noqa: S608
                    obj_params,
                ).fetchall()
                for row in obj_rows:
                    meta = json.loads(row["metadata"])
                    env_tags = json.loads(row["tags"])
                    tags = normalize_tags([*env_tags, *extract_payload_tags(meta)])
                    # Quarantined objects NEVER appear in /v1/search.
                    if "status:quarantined" in tags:
                        continue
                    allowed = json.loads(row["allowed_surfaces"])
                    cohorts = json.loads(row["target_cohorts"])
                    prod = str(row["producer_surface"])
                    vis = row["visibility_scope"]
                    owner = row["owner_key_id"]
                    if not can_view_governed(
                        visibility_scope=vis,
                        producer_surface=prod,
                        allowed_surfaces=allowed,
                        owner_key_id=owner,
                        tags=tags,
                        viewer_surface=ctx.surface,
                        viewer_key_id=ctx.key_id,
                        is_admin=ctx.is_admin,
                        direct_get=False,
                        include_restricted=False,
                    ):
                        continue
                    title, l0 = _extract_title_and_l0(row["key"], meta)
                    if l0.startswith("fields=[]"):
                        l0 = f"{row['content_type']} ({int(row['size'])} B) in bucket '{row['bucket']}'"
                    if not matches_layer2_filters(
                        haystack=f"{row['bucket']} {row['key']} {title} {l0} {row['metadata']}",
                        tags=tags,
                        producer_surface=prod,
                        target_cohorts=cohorts,
                        q="",
                        tag=tag,
                        category=category,
                        cohort=cohort,
                        surface=surface,
                        is_admin=ctx.is_admin,
                        include_restricted=include_restricted,
                    ):
                        continue
                    score = _similarity(
                        q_clean,
                        f"{row['bucket']} {row['key']} {title}",
                        f"{l0} {row['metadata']}",
                        tags,
                    )
                    if q_clean and score <= 0.05:
                        continue
                    scents.append(
                        NounScent(
                            namespace=ns,
                            kind="object",
                            container=str(row["bucket"]),
                            key=row["key"],
                            version=int(row["version"]),
                            title=title,
                            l0_summary=l0,
                            producer_surface=prod,
                            visibility_scope=vis,
                            allowed_surfaces=allowed,
                            target_cohorts=cohorts,
                            tags=tags,
                            owner_key_id=owner,
                            size_bytes=int(row["size"]),
                            sha256=str(row["sha256"]),
                            score=score,
                            updated_at=float(row["updated_at"]),
                        )
                    )

        scents.sort(key=lambda s: (-s.score, -s.updated_at, s.namespace, s.container, s.key))
        return scents[:cap]

    def feeds(self, *, namespace: str | None = None) -> list[FeedSummary]:
        """List monotonic cursor-sequenced live delta feeds in `namespace`."""
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        with self._read() as c:
            rows = c.execute(
                "SELECT feed, seq, created_at, visibility_scope, producer_surface,"
                " allowed_surfaces, owner_key_id, tags, target_cohorts"
                " FROM feed_entries WHERE namespace = ? ORDER BY feed, seq",
                (ns,),
            ).fetchall()
        grouped: dict[str, tuple[int, int, float]] = {}
        for r in rows:
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
            fd = str(r["feed"])
            sq = int(r["seq"])
            ts = float(r["created_at"])
            prev_n, prev_seq, prev_ts = grouped.get(fd, (0, 0, 0.0))
            grouped[fd] = (prev_n + 1, max(prev_seq, sq), max(prev_ts, ts))
        return [
            FeedSummary(feed=fd, entries=n, latest_seq=latest, updated_at=ts)
            for fd, (n, latest, ts) in sorted(grouped.items())
        ]

    def publish_feed(
        self,
        feed: str,
        payload: dict[str, Any],
        *,
        kind: str = "delta",
        summary: str = "",
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        owner_key_id: str | None = None,
    ) -> FeedEntry:
        """Append one monotonic per-(namespace, feed) cursor-sequenced delta entry to `feed`."""
        _validate_bucket_name(feed)
        clean_kind = (kind or "delta").strip()[:64]
        clean_summary = " ".join(summary.split())[:MAX_L0_CHARS]
        try:
            payload_json = canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"{feed}: payload is not JSON: {exc}") from exc
        if len(payload_json.encode("utf-8")) > 65_536:
            raise InvalidRequest(
                f"{feed}: feed payload exceeds 64 KiB; store large bytes in a bucket"
            )

        ns = self._resolve_ns(namespace)
        prod, owner = self._resolve_write_governance(
            producer_surface=producer_surface, owner_key_id=owner_key_id
        )
        vis: VisibilityScope = visibility_scope
        self._assert_frontier_governance(prod, vis)
        allowed = normalize_surface_list(list(allowed_surfaces) if allowed_surfaces else [])
        cohorts = normalize_cohorts(list(target_cohorts) if target_cohorts else ["all"])
        effective_tags = normalize_tags([*(tags or ()), *extract_payload_tags(payload)])
        self._assert_publisher_tags(effective_tags)

        now = self.clock.now()
        seq = 0
        for attempt in range(8):
            with self._tx() as c:
                row = c.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq"
                    " FROM feed_entries WHERE namespace = ? AND feed = ?",
                    (ns, feed),
                ).fetchone()
                seq = int(row["next_seq"])
                try:
                    c.execute(
                        "INSERT INTO feed_entries"
                        " (namespace, seq, feed, kind, summary, payload,"
                        "  producer_surface, visibility_scope, allowed_surfaces,"
                        "  target_cohorts, tags, owner_key_id, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            ns,
                            seq,
                            feed,
                            clean_kind,
                            clean_summary,
                            payload_json,
                            prod,
                            vis,
                            canonical_json(allowed),
                            canonical_json(cohorts),
                            canonical_json(effective_tags),
                            owner,
                            now,
                        ),
                    )
                    self._event(
                        c,
                        "feed_published",
                        now,
                        namespace=ns,
                        detail={"feed": feed, "seq": seq, "kind": clean_kind},
                    )
                    break
                except sqlite3.IntegrityError:
                    if attempt == 7:
                        raise
        return FeedEntry(
            namespace=ns,
            seq=seq,
            feed=feed,
            kind=clean_kind,
            summary=clean_summary,
            payload=dict(payload),
            producer_surface=prod,
            visibility_scope=vis,
            allowed_surfaces=allowed,
            target_cohorts=cohorts,
            tags=effective_tags,
            owner_key_id=owner,
            created_at=now,
        )

    def poll_feed(
        self,
        feed: str,
        *,
        after: int = 0,
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[FeedEntry]:
        """Read delta entries in `feed` with monotonic `seq > after`, applying Layer 1 & Layer 2."""
        _validate_bucket_name(feed)
        cap = max(1, min(limit, 1000))
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        has_filters = bool(q.strip() or tag or category or cohort or surface or include_restricted)
        scan_cap = max(cap, 1000) if has_filters else max(cap * 2, 200)
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM feed_entries"
                " WHERE namespace = ? AND feed = ? AND seq > ? ORDER BY seq LIMIT ?",
                (ns, feed, max(0, after), scan_cap),
            ).fetchall()

        out: list[FeedEntry] = []
        for r in rows:
            tags_list: list[str] = json.loads(r["tags"])
            allowed: list[str] = json.loads(r["allowed_surfaces"])
            cohorts: list[str] = json.loads(r["target_cohorts"])
            prod = str(r["producer_surface"])
            vis: VisibilityScope = r["visibility_scope"]
            owner = r["owner_key_id"]
            if not can_view_governed(
                visibility_scope=vis,
                producer_surface=prod,
                allowed_surfaces=allowed,
                owner_key_id=owner,
                tags=tags_list,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
                include_restricted=include_restricted,
            ):
                continue
            if not matches_layer2_filters(
                haystack=f"{r['feed']} {r['kind']} {r['summary']} {r['payload']}",
                tags=tags_list,
                producer_surface=prod,
                target_cohorts=cohorts,
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                is_admin=ctx.is_admin,
                include_restricted=include_restricted,
            ):
                continue
            out.append(
                FeedEntry(
                    namespace=r["namespace"],
                    seq=int(r["seq"]),
                    feed=r["feed"],
                    kind=str(r["kind"]),
                    summary=str(r["summary"]),
                    payload=json.loads(r["payload"]),
                    producer_surface=prod,
                    visibility_scope=vis,
                    allowed_surfaces=allowed,
                    target_cohorts=cohorts,
                    tags=tags_list,
                    owner_key_id=owner,
                    created_at=float(r["created_at"]),
                )
            )
            if len(out) >= cap:
                break
        return out

    def match_reflex(
        self,
        query: str,
        *,
        limit: int = 5,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> ReflexMatch:
        """0-call dual-resolution muscle-memory + fuzzy step recall + dead-end tombstone lookup."""
        cap = max(1, min(limit, 50))
        ns = self._resolve_ns(namespace)
        ctx = self.current_scope()
        q_clean = query.strip()
        q_sig = _canonical_sig(q_clean)
        hits: list[ReflexHit] = []
        with self._read() as c:
            rows = c.execute(
                "SELECT r.* FROM record_heads h"
                " JOIN records r"
                "   ON r.namespace = h.namespace AND r.store = h.store"
                "  AND r.key = h.key AND r.version = h.version"
                " WHERE h.namespace = ? AND h.store = ? ORDER BY r.created_at DESC LIMIT 500",
                (ns, REFLEX_STORE),
            ).fetchall()
        for row in rows:
            val = json.loads(row["value"])
            if not isinstance(val, dict):
                continue
            env_tags: list[str] = json.loads(row["tags"])
            tags = normalize_tags([*env_tags, *extract_payload_tags(val)])
            # Quarantined reflexes NEVER appear in /v1/reflexes/match.
            if "status:quarantined" in tags or "status:verified" not in tags:
                continue
            if not isinstance(val.get("verified_hits"), int) or val["verified_hits"] < 1:
                continue
            allowed: list[str] = json.loads(row["allowed_surfaces"])
            cohorts: list[str] = json.loads(row["target_cohorts"])
            prod = str(row["producer_surface"])
            vis: VisibilityScope = row["visibility_scope"]
            owner = row["owner_key_id"]
            if not can_view_governed(
                visibility_scope=vis,
                producer_surface=prod,
                allowed_surfaces=allowed,
                owner_key_id=owner,
                tags=tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=False,
                include_restricted=False,
            ):
                continue
            pattern = str(val.get("pattern") or val.get("title") or row["key"])
            _, l0 = _extract_title_and_l0(row["key"], val)
            if not matches_layer2_filters(
                haystack=f"{pattern} {l0} {row['value']}",
                tags=tags,
                producer_surface=prod,
                target_cohorts=cohorts,
                q="",
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                is_admin=ctx.is_admin,
                include_restricted=include_restricted,
            ):
                continue
            p_sig = _canonical_sig(pattern)
            exact = bool(q_sig and p_sig and q_sig == p_sig)
            raw_steps = val.get("steps")
            steps = (
                [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
            )
            score = (
                1.0
                if exact
                else _similarity(q_clean, pattern, f"{l0} {canonical_json(steps)}", tags)
            )
            if q_clean and score < 0.15 and not exact:
                continue
            verified_hits = int(val["verified_hits"])
            tier = str(
                val.get("tier")
                or ("deterministic_0_call" if verified_hits >= 3 else "hybrid_1_call")
            )
            hits.append(
                ReflexHit(
                    namespace=ns,
                    reflex_id=str(val.get("reflex_id") or row["key"]),
                    pattern=pattern,
                    tier=tier,
                    verified_hits=verified_hits,
                    exact_hit=exact,
                    score=score,
                    l0_summary=l0,
                    steps=steps,
                    producer_surface=prod,
                    visibility_scope=vis,
                    allowed_surfaces=allowed,
                    target_cohorts=cohorts,
                    tags=tags,
                    owner_key_id=owner,
                    version=int(row["version"]),
                )
            )

        hits.sort(key=lambda h: (-int(h.exact_hit), -h.score, -h.verified_hits, h.reflex_id))
        top_hits = hits[:cap]
        dead_ends = (
            self.search_nouns(
                q_clean,
                store=DEAD_END_STORE,
                limit=5,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
                namespace=ns,
            )
            if q_clean
            else []
        )

        rec_tier: Literal["0_call_reflex", "hybrid_1_call", "agent_decompose"] = "agent_decompose"
        if top_hits:
            best = top_hits[0]
            if (best.exact_hit or best.score >= 0.72) and (
                best.verified_hits >= 3 or best.tier == "deterministic_0_call"
            ):
                rec_tier = "0_call_reflex"
            elif best.score >= 0.30:
                rec_tier = "hybrid_1_call"

        return ReflexMatch(
            query=q_clean,
            recommended_tier=rec_tier,
            hits=top_hits,
            dead_ends=dead_ends,
        )

    def handshake(
        self,
        request: HandshakeRequest,
        *,
        grant_queues: Sequence[str] | None = None,
        store_scope: bool = True,
        namespace: str | None = None,
    ) -> HandshakeResponse:
        """Negotiate Surface Handshake v1.0: validate surface_id, clamp limits, and narrow scopes."""
        proto = request.protocol_version.strip()
        if not proto.startswith("1."):
            raise InvalidRequest(
                f"unsupported handshake protocol_version '{proto}'; expected '1.0'"
            )
        surface_id = (request.surface_id or "").strip()
        if not surface_id or not _SURFACE_RE.fullmatch(surface_id):
            raise InvalidRequest(
                "surface_id is required in handshake body or X-Surface-Id header"
                " and must match ^[a-z0-9][a-z0-9_-]{0,47}$"
            )
        clamped_max_chars = max(
            MIN_CLAMPED_CHARS, min(MAX_CLAMPED_CHARS, int(request.requested_max_chars))
        )
        ns = self._resolve_ns(namespace)

        with self._read() as c:
            q_rows = c.execute(
                "SELECT name, heartbeat_seconds FROM queues WHERE namespace = ? ORDER BY name",
                (ns,),
            ).fetchall()
        defined_queues = [str(r["name"]) for r in q_rows]
        hb_seconds = min(int(r["heartbeat_seconds"]) for r in q_rows) if q_rows else 60

        if grant_queues is None:
            allowed_queues = (
                list(dict.fromkeys(request.requested_queues))
                if request.requested_queues
                else defined_queues
            )
        else:
            candidates = (
                list(dict.fromkeys(request.requested_queues))
                if request.requested_queues
                else (defined_queues if defined_queues else list(grant_queues))
            )
            allowed_queues = [
                q for q in candidates if any(fnmatchcase(q, pat) for pat in grant_queues)
            ]

        shared_stores = [s.store for s in self.stores(namespace=ns)[:64]]
        isolated_prefix = f"{surface_id}."
        capabilities = list(HANDSHAKE_CAPABILITIES)
        receipt_material = {
            "backend": "tessera",
            "backend_version": __version__,
            "protocol_version": "1.0",
            "surface_id": surface_id,
            "isolated_store_prefix": isolated_prefix,
            "clamped_max_chars": clamped_max_chars,
            "allowed_queues": allowed_queues,
            "store_scope": bool(store_scope),
        }
        handshake_id = hashlib.sha256(canonical_json(receipt_material).encode("utf-8")).hexdigest()
        return HandshakeResponse(
            protocol_version="1.0",
            backend="tessera",
            backend_version=__version__,
            surface_id=surface_id,
            isolated_store_prefix=isolated_prefix,
            shared_read_stores=shared_stores,
            clamped_max_chars=clamped_max_chars,
            max_output_bytes=MAX_OUTPUT_BYTES_LIMIT,
            heartbeat_seconds=hb_seconds,
            allowed_queues=allowed_queues,
            store_scope=bool(store_scope),
            transactional_db_exempt_from_middleware=True,
            capabilities=capabilities,
            handshake_id=handshake_id,
        )
