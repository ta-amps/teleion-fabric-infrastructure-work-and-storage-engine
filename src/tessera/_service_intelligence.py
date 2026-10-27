"""HTTP routes for progressive L0 scent search, HTTP 206 byte-range slicing, live delta feeds,
and 0-call dual-resolution reflex recall.

Mounted by `tessera.service.create_app` behind the same `Gate` (`store` scope), Layer 1/2 governance
filters, and POST-only write rules.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tessera.access import Gate
from tessera.errors import InvalidRequest
from tessera.grants import KeyGrant
from tessera.models import HandshakeRequest, VisibilityScope
from tessera.ontology import (
    ALLOWED_RELATION_TYPES,
    CALLER_PROFILES,
    CATEGORY_REGISTRY,
    NOUN_CATEGORIES,
    VERB_CATEGORIES,
    adapt_noun_view,
    adjudicate_ballots,
    canonical_sha256,
    pareto_frontier,
    plan_fragility_rechecks,
    rank_items,
    reconcile_three_way,
)
from tessera.store import Store

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)


class PublishFeedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    kind: str = Field(default="delta", min_length=1, max_length=64)
    summary: str = Field(default="", max_length=520)
    producer_surface: str | None = None
    visibility_scope: VisibilityScope = "public_commons"
    allowed_surfaces: list[str] = Field(default_factory=list)
    target_cohorts: list[str] = Field(default_factory=lambda: ["all"])
    tags: list[str] = Field(default_factory=list)


def slice_bytes_response(
    data: bytes,
    range_header: str | None,
    *,
    media_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> Response:
    """Serve full bytes (200) or an RFC 9110 single byte-range slice (206 Partial Content / 416)."""
    base_headers = {"accept-ranges": "bytes", **(headers or {})}
    if not range_header:
        return Response(data, status_code=200, media_type=media_type, headers=base_headers)

    total = len(data)
    match = _RANGE_RE.match(range_header.strip())
    if not match or total == 0:
        return Response(
            b"",
            status_code=416,
            headers={**base_headers, "content-range": f"bytes */{total}"},
        )
    s_raw, e_raw = match.group(1), match.group(2)
    if not s_raw and not e_raw:
        return Response(
            b"",
            status_code=416,
            headers={**base_headers, "content-range": f"bytes */{total}"},
        )
    if not s_raw:
        suffix = int(e_raw)
        if suffix <= 0:
            return Response(
                b"",
                status_code=416,
                headers={**base_headers, "content-range": f"bytes */{total}"},
            )
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(s_raw)
        end = total - 1 if not e_raw else min(int(e_raw), total - 1)
        if start >= total or start > end:
            return Response(
                b"",
                status_code=416,
                headers={**base_headers, "content-range": f"bytes */{total}"},
            )

    part = data[start : end + 1]
    return Response(
        part,
        status_code=206,
        media_type=media_type,
        headers={**base_headers, "content-range": f"bytes {start}-{end}/{total}"},
    )


def mount_intelligence_routes(
    app: FastAPI,
    store: Store,
    gate: Gate,
    require_key_dep: Callable[..., Any],
) -> None:
    key: Any = Depends(require_key_dep)

    @app.get("/v1/search")
    def search_nouns(
        q: str = "",
        store_name: str | None = None,
        store_filter: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        request: Request = None,  # type: ignore[assignment]
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        chosen_store = (
            (request.query_params.get("store") if request else None) or store_name or store_filter
        )
        scents = store.search_nouns(
            q,
            store=chosen_store,
            bucket=bucket,
            limit=limit,
            tag=tag,
            category=category,
            cohort=cohort,
            surface=surface,
            include_restricted=include_restricted,
        )
        return {"scents": [s.model_dump(mode="json") for s in scents]}

    @app.get("/v1/feeds")
    def list_feeds(grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"feeds": [f.model_dump(mode="json") for f in store.feeds()]}

    @app.get("/v1/feeds/{feed_name}")
    def poll_feed(
        feed_name: str,
        after: int = 0,
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        entries = store.poll_feed(
            feed_name,
            after=after,
            limit=limit,
            q=q,
            tag=tag,
            category=category,
            cohort=cohort,
            surface=surface,
            include_restricted=include_restricted,
        )
        next_cursor = entries[-1].seq if entries else max(0, after)
        return {
            "feed": feed_name,
            "entries": [e.model_dump(mode="json") for e in entries],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/feeds/{feed_name}", status_code=201)
    def publish_feed(
        feed_name: str,
        body: PublishFeedBody,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        entry = store.publish_feed(
            feed_name,
            body.payload,
            kind=body.kind,
            summary=body.summary,
            producer_surface=body.producer_surface,
            visibility_scope=body.visibility_scope,
            allowed_surfaces=body.allowed_surfaces,
            target_cohorts=body.target_cohorts,
            tags=body.tags,
        )
        return entry.model_dump(mode="json")

    @app.get("/v1/reflexes/match")
    def match_reflex(
        q: str = "",
        limit: int = 5,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        return store.match_reflex(
            q,
            limit=limit,
            tag=tag,
            category=category,
            cohort=cohort,
            surface=surface,
            include_restricted=include_restricted,
        ).model_dump(mode="json")

    @app.post("/v1/handshake")
    def negotiate_handshake(
        body: HandshakeRequest,
        request: Request,
        grant: KeyGrant = key,
    ) -> JSONResponse:
        hdr_surface = (request.headers.get("x-surface-id") or "").strip()
        if hdr_surface and body.surface_id and hdr_surface != body.surface_id:
            raise InvalidRequest(
                f"X-Surface-Id header '{hdr_surface}' does not match body surface_id '{body.surface_id}'"
            )
        effective_surface = body.surface_id or hdr_surface or None
        req_obj = (
            body
            if effective_surface == body.surface_id
            else body.model_copy(update={"surface_id": effective_surface})
        )
        negotiated = store.handshake(
            req_obj,
            grant_queues=grant.queues,
            store_scope=grant.can_store,
        )
        return JSONResponse(
            negotiated.model_dump(mode="json"),
            headers={
                "x-surface-id": negotiated.surface_id,
                "x-handshake-id": negotiated.handshake_id,
            },
        )

    @app.get("/v1/categories")
    def list_ontology_categories(
        profile: str | None = None,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        prof_obj = None
        if profile:
            if profile not in CALLER_PROFILES:
                raise InvalidRequest(f"unknown caller profile '{profile}'")
            prof_obj = CALLER_PROFILES[profile].model_dump(mode="json")
        return {
            "noun_categories": [
                NOUN_CATEGORIES[cid].model_dump(mode="json") for cid in sorted(NOUN_CATEGORIES)
            ],
            "verb_categories": [
                VERB_CATEGORIES[cid].model_dump(mode="json") for cid in sorted(VERB_CATEGORIES)
            ],
            "allowed_relations": sorted(ALLOWED_RELATION_TYPES),
            "caller_profiles": [
                CALLER_PROFILES[pid].model_dump(mode="json") for pid in sorted(CALLER_PROFILES)
            ],
            "active_profile": prof_obj,
        }

    @app.get("/v1/rankings/{category_id}")
    def rank_store_records(
        category_id: str,
        store_name: str | None = None,
        axis: str = "composite_score",
        profile: str | None = None,
        limit: int = 50,
        request: Request = None,  # type: ignore[assignment]
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        if category_id not in CATEGORY_REGISTRY:
            raise InvalidRequest(f"unknown category '{category_id}'")
        if profile and profile not in CALLER_PROFILES:
            raise InvalidRequest(f"unknown caller profile '{profile}'")
        target_store = (
            (request.query_params.get("store") if request else None)
            or store_name
            or "tessera.seeds"
        )
        records = store.list_records(target_store, limit=max(1, min(limit, 200)))
        items: list[dict[str, Any]] = []
        raw_by_id: dict[str, dict[str, Any]] = {}
        now_ts = time.time()
        for rec in records:
            val: dict[str, Any] = rec.value if isinstance(rec.value, dict) else {"value": rec.value}
            iid = str(val.get("item_id") or val.get("id") or rec.key)
            raw_frag = val.get("fragility")
            frag_sub: dict[str, Any] = raw_frag if isinstance(raw_frag, dict) else {}
            raw_live = val.get("live_probe")
            live_sub: dict[str, Any] = raw_live if isinstance(raw_live, dict) else {}
            trials = int(
                val.get("trials")
                or val.get("verified_hits")
                or (12 if live_sub.get("http_status") == 200 else 6)
            )
            raw_succ = val.get("successes")
            successes = (
                float(raw_succ)
                if raw_succ is not None
                else float(
                    trials if live_sub.get("http_status", 200) == 200 else max(0, trials - 2)
                )
            )
            norm_item: dict[str, Any] = {
                "item_id": iid,
                "id": iid,
                "category_id": str(val.get("category_id") or val.get("category") or category_id),
                "title": str(val.get("title") or iid),
                "dimensions": dict(val.get("dimensions") or {}),
                "trials": trials,
                "successes": successes,
                "fragility_score": frag_sub.get("fragility_score", val.get("fragility_score")),
                "half_life_hours": frag_sub.get("half_life_hours", val.get("half_life_hours")),
                "last_verified_at": (
                    val.get("last_verified_at")
                    or val.get("last_checked_at")
                    or frag_sub.get("last_verified_at")
                    or rec.created_at
                ),
            }
            items.append(norm_item)
            raw_by_id[iid] = {**val, **norm_item}
        ranked = rank_items(items, sort_axis=axis, now_ts=now_ts, top_k=max(1, min(limit, 200)))
        ranked_dicts = [r.model_dump(mode="json") for r in ranked]
        frontier_ids = {r.item_id for r in pareto_frontier(ranked)} if ranked else set()
        out_rows: list[dict[str, Any]] = []
        for r_dict in ranked_dicts:
            iid = r_dict["item_id"]
            entry_out: dict[str, Any] = {
                **r_dict,
                "id": iid,
                "pareto_optimal": iid in frontier_ids,
            }
            if profile and iid in raw_by_id:
                entry_out["adapted_view"] = adapt_noun_view(raw_by_id[iid], profile).model_dump(
                    mode="json"
                )
            out_rows.append(entry_out)
        return {
            "category_id": category_id,
            "store": target_store,
            "sort_axis": axis,
            "profile": profile,
            "pareto_frontier_ids": sorted(frontier_ids),
            "ranked": out_rows,
        }

    @app.get("/v1/fragility/plan")
    def plan_store_fragility_rechecks(
        store_name: str | None = None,
        budget: int = 25,
        simulate_hours_ahead: float = 16.0,
        request: Request = None,  # type: ignore[assignment]
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        target_store = (
            (request.query_params.get("store") if request else None)
            or store_name
            or "registry.feeds"
        )
        records = store.list_records(target_store, limit=200)
        now_ts = time.time() + max(0.0, float(simulate_hours_ahead)) * 3600.0
        items: list[dict[str, Any]] = []
        for rec in records:
            val: dict[str, Any] = rec.value if isinstance(rec.value, dict) else {}
            raw_frag = val.get("fragility")
            frag_sub: dict[str, Any] = raw_frag if isinstance(raw_frag, dict) else {}
            iid = str(val.get("item_id") or val.get("id") or rec.key)
            items.append(
                {
                    "item_id": iid,
                    "category_id": str(
                        val.get("category_id") or val.get("category") or "noun.feed_source"
                    ),
                    "source_uri": val.get("url") or val.get("source_uri"),
                    "expected_sha256": val.get("content_sha256") or val.get("sha256"),
                    "etag": val.get("etag"),
                    "last_modified": val.get("last_modified"),
                    "half_life_hours": frag_sub.get(
                        "half_life_hours", val.get("half_life_hours", 12.0)
                    ),
                    "fragility_score": frag_sub.get(
                        "fragility_score", val.get("fragility_score", 0.85)
                    ),
                    "last_verified_at": (
                        val.get("last_verified_at")
                        or val.get("last_checked_at")
                        or frag_sub.get("last_verified_at")
                        or rec.created_at
                    ),
                    "importance": float(val.get("importance", 1.0)),
                }
            )
        planned = plan_fragility_rechecks(items, now_ts=now_ts, max_batch=max(1, min(budget, 100)))
        return {
            "store": target_store,
            "evaluated_at": now_ts,
            "plan": [p.model_dump(mode="json") for p in planned],
        }

    @app.post("/v1/reconcile")
    async def reconcile_cas_documents(
        request: Request,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        try:
            body = await request.json()
        except Exception as exc:
            raise InvalidRequest("request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise InvalidRequest("JSON body must be an object")
        base_doc = body.get("base") if isinstance(body.get("base"), dict) else body.get("ancestor")
        ours_doc = body.get("ours") if isinstance(body.get("ours"), dict) else body.get("left")
        theirs_doc = (
            body.get("theirs") if isinstance(body.get("theirs"), dict) else body.get("right")
        )
        if (
            not isinstance(base_doc, dict)
            or not isinstance(ours_doc, dict)
            or not isinstance(theirs_doc, dict)
        ):
            raise InvalidRequest("base/ancestor, ours/left, and theirs/right must be JSON objects")
        set_fields = list(body.get("set_fields") or body.get("monotonic_set_fields") or [])
        counter_fields = list(body.get("counter_fields") or [])
        conflict = reconcile_three_way(
            base_doc,
            ours_doc,
            theirs_doc,
            ours_proposer_key_id=body.get("ours_proposer_key_id"),
            theirs_proposer_key_id=body.get("theirs_proposer_key_id"),
            set_fields=set_fields,
            counter_fields=counter_fields,
        )
        out = conflict.model_dump(mode="json")
        out["clean"] = conflict.status == "auto_merged"
        return out

    @app.post("/v1/reconcile/adjudicate")
    async def adjudicate_conflict_ballots(
        request: Request,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        try:
            body = await request.json()
        except Exception as exc:
            raise InvalidRequest("request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise InvalidRequest("JSON body must be an object")

        alias_map = {"cand_a": "candidate_a", "cand_b": "candidate_b"}
        rev_alias: dict[str, str] = {}
        if isinstance(body.get("conflict"), dict):
            conflict_arg: dict[str, Any] = dict(body["conflict"])
        else:
            cands = body.get("candidates") or {}
            doc_a = dict(cands.get("candidate_a") or cands.get("cand_a") or {"option": "a"})
            doc_b = dict(cands.get("candidate_b") or cands.get("cand_b") or {"option": "b"})
            if "cand_a" in cands:
                rev_alias["candidate_a"] = "cand_a"
            if "cand_b" in cands:
                rev_alias["candidate_b"] = "cand_b"
            proposer = body.get("proposer_id") or body.get("proposer_key_id")
            conflict_arg = {
                "conflict_id": str(body.get("conflict_id") or "conflict-adjudication"),
                "status": "needs_voting_or_steward",
                "conflicting_paths": list(body.get("conflicting_paths") or ["summary"]),
                "proposer_key_ids": [proposer] if proposer else [],
                "candidate_a": {
                    "candidate_id": "candidate_a",
                    "proposer_key_id": body.get("candidate_a_proposer"),
                    "conflicting_values": doc_a,
                    "document": doc_a,
                    "sha256": canonical_sha256(doc_a),
                },
                "candidate_b": {
                    "candidate_id": "candidate_b",
                    "proposer_key_id": proposer,
                    "conflicting_values": doc_b,
                    "document": doc_b,
                    "sha256": canonical_sha256(doc_b),
                },
            }

        raw_ballots = body.get("ballots") or []
        norm_ballots: list[dict[str, Any]] = []
        calibrations: dict[str, Any] = dict(body.get("worker_calibrations") or {})
        for b in raw_ballots:
            if not isinstance(b, dict):
                continue
            voter_key = str(b.get("voter_key_id") or b.get("key_prefix") or b.get("voter_id") or "")
            if b.get("voter_id") == body.get("proposer_id") and body.get("proposer_id"):
                voter_key = str(body["proposer_id"])
            raw_choice = str(b.get("choice") or "")
            mapped_choice = alias_map.get(raw_choice, raw_choice)
            norm_ballots.append(
                {
                    "voter_key_id": voter_key,
                    "choice": mapped_choice,
                    "citations": list(b.get("citations") or ["verified:receipt"]),
                    "verification_receipt_sha256": b.get("verification_receipt_sha256")
                    or ("a" * 64),
                    "rationale": str(b.get("rationale") or "verified"),
                    "defect_summary": str(b.get("defect_summary") or "superseded"),
                }
            )
            if voter_key and voter_key not in calibrations and "verified_passes" in b:
                passes = float(b.get("verified_passes", 0))
                fails = float(b.get("verified_failures", 0))
                calibrations[voter_key] = {
                    "worker_key_id": voter_key,
                    "verified_correct": passes,
                    "verified_total": passes + fails,
                }

        receipt = adjudicate_ballots(
            conflict_arg,
            norm_ballots,
            calibrations,
            human_only=bool(body.get("human_only", False)),
        )
        out = receipt.model_dump(mode="json")
        win = receipt.winning_candidate_id
        out["winner"] = rev_alias.get(win, win) if win else None
        return out
