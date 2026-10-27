"""0-call progressive L0 scent search, monotonic delta feeds, and dual-resolution reflex recall.

Gives any connected agent (MCP coding harness, HTTPS chat assistant, or CLI worker) fast 0-model-call
discovery over versioned nouns and buckets without loading megabytes into context, monotonic cursor
feeds for live changes, and muscle-memory reflex recall paired with negative-knowledge tombstones.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Any, Literal

from tessera._buckets import BucketOps, _validate_bucket_name
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


def _extract_tags(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return []
    out: list[str] = []
    for k in ("domain", "surface", "stage", "kind", "category", "area", "track"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    raw_tags = obj.get("tags")
    if isinstance(raw_tags, list):
        out.extend(str(t).strip() for t in raw_tags if str(t).strip())
    elif isinstance(raw_tags, dict):
        out.extend(f"{k}:{v}" for k, v in sorted(raw_tags.items()))
    meta_tags = obj.get("metadata_tags")
    if isinstance(meta_tags, dict):
        out.extend(f"{k}:{v}" for k, v in sorted(meta_tags.items()))
    return list(dict.fromkeys(out))[:12]


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
        snippet = compact[:260] + ("…" if len(compact) > 260 else "")
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
    ) -> list[NounScent]:
        """0-call progressive L0 scent search across head CAS records and versioned bucket files."""
        cap = max(1, min(limit, 200))
        if store is not None:
            _validate_bucket_name(store)
        if bucket is not None:
            _validate_bucket_name(bucket)

        scents: list[NounScent] = []
        q_clean = query.strip()
        with self._read() as c:
            if bucket is None:
                if store is not None:
                    rec_rows = c.execute(
                        "SELECT r.* FROM record_heads h"
                        " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                        " WHERE h.store = ? ORDER BY r.created_at DESC LIMIT 1000",
                        (store,),
                    ).fetchall()
                else:
                    rec_rows = c.execute(
                        "SELECT r.* FROM record_heads h"
                        " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                        " ORDER BY r.created_at DESC LIMIT 1000"
                    ).fetchall()
                for row in rec_rows:
                    raw_val: str = row["value"]
                    parsed = json.loads(raw_val)
                    title, l0 = _extract_title_and_l0(row["key"], parsed)
                    tags = _extract_tags(parsed)
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
                            kind="record",
                            namespace=row["store"],
                            key=row["key"],
                            version=int(row["version"]),
                            title=title,
                            l0_summary=l0,
                            tags=tags,
                            size_bytes=len(raw_val.encode("utf-8")),
                            score=score,
                            updated_at=float(row["created_at"]),
                        )
                    )

            if store is None:
                if bucket is not None:
                    obj_rows = c.execute(
                        "SELECT * FROM bucket_objects WHERE bucket = ? ORDER BY updated_at DESC LIMIT 1000",
                        (bucket,),
                    ).fetchall()
                else:
                    obj_rows = c.execute(
                        "SELECT * FROM bucket_objects ORDER BY updated_at DESC LIMIT 1000"
                    ).fetchall()
                for row in obj_rows:
                    meta = json.loads(row["metadata"])
                    title, l0 = _extract_title_and_l0(row["key"], meta)
                    if l0.startswith("fields=[]"):
                        l0 = f"{row['content_type']} ({int(row['size'])} B) in bucket '{row['bucket']}'"
                    tags = _extract_tags(meta)
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
                            kind="object",
                            namespace=row["bucket"],
                            key=row["key"],
                            version=int(row["version"]),
                            title=title,
                            l0_summary=l0,
                            tags=tags,
                            size_bytes=int(row["size"]),
                            sha256=str(row["sha256"]),
                            score=score,
                            updated_at=float(row["updated_at"]),
                        )
                    )

        scents.sort(key=lambda s: (-s.score, -s.updated_at, s.namespace, s.key))
        return scents[:cap]

    def feeds(self) -> list[FeedSummary]:
        """List monotonic cursor-sequenced live delta feeds."""
        with self._read() as c:
            rows = c.execute(
                "SELECT feed, COUNT(*) AS entries, MAX(seq) AS latest_seq, MAX(created_at) AS updated_at"
                " FROM feed_entries GROUP BY feed ORDER BY feed"
            ).fetchall()
        return [
            FeedSummary(
                feed=r["feed"],
                entries=int(r["entries"]),
                latest_seq=int(r["latest_seq"]),
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    def publish_feed(
        self,
        feed: str,
        payload: dict[str, Any],
        *,
        kind: str = "delta",
        summary: str = "",
    ) -> FeedEntry:
        """Append one monotonic cursor-sequenced delta entry to `feed`."""
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

        now = self.clock.now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO feed_entries (feed, kind, summary, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (feed, clean_kind, clean_summary, payload_json, now),
            )
            seq = int(cur.lastrowid or 0)
            self._event(
                c,
                "feed_published",
                now,
                detail={"feed": feed, "seq": seq, "kind": clean_kind},
            )
        return FeedEntry(
            seq=seq,
            feed=feed,
            kind=clean_kind,
            summary=clean_summary,
            payload=dict(payload),
            created_at=now,
        )

    def poll_feed(self, feed: str, *, after: int = 0, limit: int = 100) -> list[FeedEntry]:
        """Read delta entries in `feed` with monotonic `seq > after`."""
        _validate_bucket_name(feed)
        cap = max(1, min(limit, 1000))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM feed_entries WHERE feed = ? AND seq > ? ORDER BY seq LIMIT ?",
                (feed, max(0, after), cap),
            ).fetchall()
        return [
            FeedEntry(
                seq=int(r["seq"]),
                feed=r["feed"],
                kind=str(r["kind"]),
                summary=str(r["summary"]),
                payload=json.loads(r["payload"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def match_reflex(self, query: str, *, limit: int = 5) -> ReflexMatch:
        """0-call dual-resolution muscle-memory + fuzzy step recall + dead-end tombstone lookup."""
        cap = max(1, min(limit, 50))
        q_clean = query.strip()
        q_sig = _canonical_sig(q_clean)
        hits: list[ReflexHit] = []
        with self._read() as c:
            rows = c.execute(
                "SELECT r.* FROM record_heads h"
                " JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version"
                " WHERE h.store = ? ORDER BY r.created_at DESC LIMIT 500",
                (REFLEX_STORE,),
            ).fetchall()
        for row in rows:
            val = json.loads(row["value"])
            if not isinstance(val, dict):
                continue
            pattern = str(val.get("pattern") or val.get("title") or row["key"])
            p_sig = _canonical_sig(pattern)
            exact = bool(q_sig and p_sig and q_sig == p_sig)
            tags = _extract_tags(val)
            _, l0 = _extract_title_and_l0(row["key"], val)
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
            verified_hits = int(val.get("verified_hits", 1))
            tier = str(
                val.get("tier")
                or ("deterministic_0_call" if verified_hits >= 3 else "hybrid_1_call")
            )
            hits.append(
                ReflexHit(
                    reflex_id=str(val.get("reflex_id") or row["key"]),
                    pattern=pattern,
                    tier=tier,
                    verified_hits=verified_hits,
                    exact_hit=exact,
                    score=score,
                    l0_summary=l0,
                    steps=steps,
                    tags=tags,
                    version=int(row["version"]),
                )
            )

        hits.sort(key=lambda h: (-int(h.exact_hit), -h.score, -h.verified_hits, h.reflex_id))
        top_hits = hits[:cap]
        dead_ends = self.search_nouns(q_clean, store=DEAD_END_STORE, limit=5) if q_clean else []

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

        with self._read() as c:
            q_rows = c.execute(
                "SELECT name, heartbeat_seconds FROM queues ORDER BY name"
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

        shared_stores = [s.store for s in self.stores()[:64]]
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
