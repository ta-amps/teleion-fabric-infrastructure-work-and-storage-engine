"""Fragility scoring, exponential staleness decay, recheck planning, and drift detection.

Models artifact volatility across static code (8760h), regulatory statute cards (2160h),
MCP/API schemas (168h), and live feeds/pricing/advisories (12h), scheduling prioritized
verb.recheck_fragility tasks and evaluating conditional HTTP/SHA-256 observations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .categories import CATEGORY_REGISTRY, FragilityDomain

RecheckStatus = Literal["unchanged", "drifted", "unreachable_degraded"]

HALF_LIFE_HOURS_BY_DOMAIN: dict[FragilityDomain, float] = {
    "static_code": 8760.0,
    "regulatory_statute": 2160.0,
    "mcp_api_schema": 168.0,
    "live_feed": 12.0,
}

BASE_FRAGILITY_BY_DOMAIN: dict[FragilityDomain, float] = {
    "static_code": 0.10,
    "regulatory_statute": 0.35,
    "mcp_api_schema": 0.65,
    "live_feed": 0.90,
}

_DOMAIN_ALIASES: dict[str, FragilityDomain] = {
    "static_code": "static_code",
    "code": "static_code",
    "regulatory_statute": "regulatory_statute",
    "statute": "regulatory_statute",
    "mcp_api_schema": "mcp_api_schema",
    "mcp": "mcp_api_schema",
    "api_schema": "mcp_api_schema",
    "live_feed": "live_feed",
    "pricing": "live_feed",
    "advisory": "live_feed",
}


class FragilityAssessment(BaseModel):
    """Computed fragility score, half-life, freshness confidence, and recheck flag."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    domain: FragilityDomain
    fragility_score: float = Field(ge=0.0, le=1.0)
    half_life_hours: float = Field(gt=0.0)
    age_hours: float = Field(ge=0.0)
    freshness_confidence: float = Field(ge=0.0, le=1.0)
    recheck_threshold: float = Field(gt=0.0, le=1.0)
    needs_recheck: bool


class RecheckTaskSpec(BaseModel):
    """Prioritized verb.recheck_fragility task emitted by plan_fragility_rechecks."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    verb_category: Literal["verb.recheck_fragility"] = "verb.recheck_fragility"
    target_item_id: str
    target_category_id: str
    domain: FragilityDomain
    source_uri: str | None = None
    expected_sha256: str | None = None
    conditional_headers: dict[str, str] = Field(default_factory=dict)
    half_life_hours: float = Field(gt=0.0)
    freshness_confidence: float = Field(ge=0.0, le=1.0)
    fragility_score: float = Field(ge=0.0, le=1.0)
    importance: float = Field(ge=0.0)
    priority_urgency: float = Field(ge=0.0)
    planned_at: float


class RecheckObservation(BaseModel):
    """Outcome of comparing a live recheck response against prior SHA-256 and ETag."""

    status: RecheckStatus
    http_status: int
    prev_sha256: str
    observed_sha256: str | None = None
    etag_matched: bool | None = None
    drift_detected: bool
    degraded: bool
    should_reset_freshness: bool
    should_invalidate_dependents: bool

    def __str__(self) -> str:
        return self.status

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.status == other
        return super().__eq__(other)


def _to_epoch_seconds(ts: float | int | str) -> float:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if isinstance(ts, str):
        normalized = ts.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized).timestamp()
    raise TypeError(f"Unsupported timestamp type {type(ts).__name__}")


def resolve_fragility_domain(domain_or_category: str) -> FragilityDomain:
    """Resolve a canonical FragilityDomain from a category_id or domain alias."""
    if domain_or_category in CATEGORY_REGISTRY:
        return CATEGORY_REGISTRY[domain_or_category].fragility_domain
    key = domain_or_category.strip().lower()
    if key in _DOMAIN_ALIASES:
        return _DOMAIN_ALIASES[key]
    raise KeyError(
        f"Unknown fragility domain or category {domain_or_category!r}; "
        f"expected category_id or one of {sorted(HALF_LIFE_HOURS_BY_DOMAIN)}"
    )


def resolve_half_life_hours(
    domain_or_category: str,
    *,
    override_half_life_hours: float | None = None,
) -> float:
    """Return canonical half-life hours (8760h, 2160h, 168h, or 12h) or explicit override."""
    if override_half_life_hours is not None:
        if override_half_life_hours <= 0.0:
            raise ValueError("override_half_life_hours must be > 0")
        return float(override_half_life_hours)
    if domain_or_category in CATEGORY_REGISTRY:
        return CATEGORY_REGISTRY[domain_or_category].default_half_life_hours
    domain = resolve_fragility_domain(domain_or_category)
    return HALF_LIFE_HOURS_BY_DOMAIN[domain]


def compute_fragility_score(
    domain_or_category: str,
    *,
    historical_drift_rate: float = 0.0,
    external_dependency_count: int = 0,
    upstream_mutability: float | None = None,
) -> float:
    """Compute bounded fragility_score in [0.0, 1.0] from domain baseline and drift signals."""
    domain = resolve_fragility_domain(domain_or_category)
    base = BASE_FRAGILITY_BY_DOMAIN[domain]

    drift = max(0.0, min(1.0, float(historical_drift_rate)))
    dep_count = max(0, int(external_dependency_count))
    # Saturating dependency pressure in [0.0, 0.15]
    dep_pressure = 0.15 * (1.0 - (1.0 / (1.0 + 0.25 * dep_count)))

    if upstream_mutability is None:
        mutability = base
    else:
        mutability = max(0.0, min(1.0, float(upstream_mutability)))

    blended = 0.55 * base + 0.25 * drift + 0.20 * mutability + dep_pressure
    return round(max(0.0, min(1.0, blended)), 6)


def freshness_confidence(
    last_verified_at: float | int | str,
    now_ts: float | int | str,
    half_life_hours: float,
) -> float:
    """Compute exponential freshness decay: 0.5 ** (age_hours / half_life_hours)."""
    if half_life_hours <= 0.0:
        raise ValueError(f"half_life_hours must be > 0, got {half_life_hours}")
    verified_sec = _to_epoch_seconds(last_verified_at)
    now_sec = _to_epoch_seconds(now_ts)
    age_hours = max(0.0, now_sec - verified_sec) / 3600.0
    confidence = float(0.5 ** (age_hours / float(half_life_hours)))
    return max(0.0, min(1.0, confidence))


def needs_recheck(freshness: float, threshold: float = 0.5) -> bool:
    """Return True when freshness confidence has decayed below threshold."""
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be in (0.0, 1.0], got {threshold}")
    if not (0.0 <= freshness <= 1.0):
        raise ValueError(f"freshness must be in [0.0, 1.0], got {freshness}")
    return freshness < threshold


def assess_fragility(
    item_id: str,
    domain_or_category: str,
    *,
    last_verified_at: float | int | str,
    now_ts: float | int | str,
    recheck_threshold: float = 0.5,
    half_life_hours: float | None = None,
    historical_drift_rate: float = 0.0,
    external_dependency_count: int = 0,
) -> FragilityAssessment:
    """Produce a full FragilityAssessment for an ontology artifact."""
    domain = resolve_fragility_domain(domain_or_category)
    hl = resolve_half_life_hours(domain_or_category, override_half_life_hours=half_life_hours)
    f_score = compute_fragility_score(
        domain_or_category,
        historical_drift_rate=historical_drift_rate,
        external_dependency_count=external_dependency_count,
    )
    verified_sec = _to_epoch_seconds(last_verified_at)
    now_sec = _to_epoch_seconds(now_ts)
    age_hours = max(0.0, now_sec - verified_sec) / 3600.0
    fresh = freshness_confidence(verified_sec, now_sec, hl)
    return FragilityAssessment(
        item_id=item_id,
        domain=domain,
        fragility_score=f_score,
        half_life_hours=hl,
        age_hours=round(age_hours, 6),
        freshness_confidence=round(fresh, 6),
        recheck_threshold=recheck_threshold,
        needs_recheck=needs_recheck(fresh, recheck_threshold),
    )


def plan_fragility_rechecks(
    items: Sequence[Mapping[str, Any] | BaseModel],
    now_ts: float | int | str,
    max_batch: int = 25,
) -> list[RecheckTaskSpec]:
    """Emit prioritized verb.recheck_fragility tasks sorted by urgency = importance * (1 - freshness)."""
    if max_batch <= 0:
        return []
    now_sec = _to_epoch_seconds(now_ts)
    candidates: list[RecheckTaskSpec] = []

    for raw in items:
        rec = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
        item_id = str(rec.get("item_id") or rec.get("id") or "")
        if not item_id:
            raise ValueError("Each item in plan_fragility_rechecks requires 'item_id' or 'id'")

        category_id = str(rec.get("category_id") or "noun.feed_source")
        domain_or_cat = str(rec.get("domain") or category_id)
        domain = resolve_fragility_domain(domain_or_cat)
        hl = resolve_half_life_hours(
            domain_or_cat,
            override_half_life_hours=rec.get("half_life_hours"),
        )

        last_verified = rec.get("last_verified_at", 0.0)
        threshold = float(rec.get("recheck_threshold", 0.5))
        importance = max(0.0, float(rec.get("importance", 1.0)))

        fresh = freshness_confidence(last_verified, now_sec, hl)
        if not needs_recheck(fresh, threshold):
            continue

        frag_score = (
            float(rec["fragility_score"])
            if "fragility_score" in rec and rec["fragility_score"] is not None
            else compute_fragility_score(
                domain_or_cat,
                historical_drift_rate=float(rec.get("historical_drift_rate", 0.0)),
                external_dependency_count=int(rec.get("external_dependency_count", 0)),
            )
        )
        urgency = importance * (1.0 - fresh)

        headers: dict[str, str] = {}
        etag = rec.get("etag")
        if isinstance(etag, str) and etag.strip():
            headers["If-None-Match"] = etag.strip()
        last_modified = rec.get("last_modified")
        if isinstance(last_modified, str) and last_modified.strip():
            headers["If-Modified-Since"] = last_modified.strip()

        task_seed = json.dumps(
            {
                "verb": "verb.recheck_fragility",
                "target_item_id": item_id,
                "planned_at": round(now_sec, 3),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        task_id = "recheck_" + hashlib.sha256(task_seed.encode("utf-8")).hexdigest()[:16]

        candidates.append(
            RecheckTaskSpec(
                task_id=task_id,
                target_item_id=item_id,
                target_category_id=category_id,
                domain=domain,
                source_uri=rec.get("source_uri"),
                expected_sha256=(
                    rec.get("expected_sha256")
                    or rec.get("content_sha256")
                    or rec.get("prev_sha256")
                ),
                conditional_headers=headers,
                half_life_hours=hl,
                freshness_confidence=round(fresh, 6),
                fragility_score=round(max(0.0, min(1.0, frag_score)), 6),
                importance=round(importance, 6),
                priority_urgency=round(urgency, 6),
                planned_at=now_sec,
            )
        )

    candidates.sort(key=lambda t: (-t.priority_urgency, t.freshness_confidence, t.target_item_id))
    return candidates[:max_batch]


def _normalize_etag(etag: str | None) -> str | None:
    if etag is None:
        return None
    stripped = etag.strip()
    if stripped.startswith("W/"):
        stripped = stripped[2:].strip()
    return stripped.strip('"') or None


def evaluate_recheck_observation(
    prev_sha256: str,
    new_bytes: bytes | bytearray | str | None,
    http_status: int,
    *,
    prev_etag: str | None = None,
    new_etag: str | None = None,
) -> RecheckObservation:
    """Compare prior SHA-256 and optional HTTP ETag against a fresh probe response.

    Emits status in {'unchanged', 'drifted', 'unreachable_degraded'}.
    """
    norm_prev_sha = (prev_sha256 or "").strip().lower()
    norm_prev_etag = _normalize_etag(prev_etag)
    norm_new_etag = _normalize_etag(new_etag)
    etag_matched: bool | None = None
    if norm_prev_etag is not None and norm_new_etag is not None:
        etag_matched = norm_prev_etag == norm_new_etag

    status_code = int(http_status)

    # HTTP 304 Not Modified: authoritative unchanged signal unless ETag explicitly disagrees
    if status_code == 304:
        if etag_matched is False:
            return RecheckObservation(
                status="drifted",
                http_status=status_code,
                prev_sha256=norm_prev_sha,
                observed_sha256=None,
                etag_matched=False,
                drift_detected=True,
                degraded=False,
                should_reset_freshness=False,
                should_invalidate_dependents=True,
            )
        return RecheckObservation(
            status="unchanged",
            http_status=status_code,
            prev_sha256=norm_prev_sha,
            observed_sha256=norm_prev_sha or None,
            etag_matched=(True if etag_matched is None else etag_matched),
            drift_detected=False,
            degraded=False,
            should_reset_freshness=True,
            should_invalidate_dependents=False,
        )

    # Network failure (status 0), 4xx, or 5xx -> unreachable_degraded
    if status_code < 200 or status_code >= 400:
        return RecheckObservation(
            status="unreachable_degraded",
            http_status=status_code,
            prev_sha256=norm_prev_sha,
            observed_sha256=None,
            etag_matched=etag_matched,
            drift_detected=False,
            degraded=True,
            should_reset_freshness=False,
            should_invalidate_dependents=False,
        )

    # 2xx response without body bytes (e.g. HEAD response)
    if new_bytes is None:
        if etag_matched is True:
            return RecheckObservation(
                status="unchanged",
                http_status=status_code,
                prev_sha256=norm_prev_sha,
                observed_sha256=norm_prev_sha or None,
                etag_matched=True,
                drift_detected=False,
                degraded=False,
                should_reset_freshness=True,
                should_invalidate_dependents=False,
            )
        if etag_matched is False:
            return RecheckObservation(
                status="drifted",
                http_status=status_code,
                prev_sha256=norm_prev_sha,
                observed_sha256=None,
                etag_matched=False,
                drift_detected=True,
                degraded=False,
                should_reset_freshness=False,
                should_invalidate_dependents=True,
            )
        return RecheckObservation(
            status="unreachable_degraded",
            http_status=status_code,
            prev_sha256=norm_prev_sha,
            observed_sha256=None,
            etag_matched=None,
            drift_detected=False,
            degraded=True,
            should_reset_freshness=False,
            should_invalidate_dependents=False,
        )

    raw_bytes = new_bytes.encode("utf-8") if isinstance(new_bytes, str) else bytes(new_bytes)
    observed_sha = hashlib.sha256(raw_bytes).hexdigest()

    if norm_prev_sha and observed_sha == norm_prev_sha:
        return RecheckObservation(
            status="unchanged",
            http_status=status_code,
            prev_sha256=norm_prev_sha,
            observed_sha256=observed_sha,
            etag_matched=etag_matched,
            drift_detected=False,
            degraded=False,
            should_reset_freshness=True,
            should_invalidate_dependents=False,
        )

    return RecheckObservation(
        status="drifted",
        http_status=status_code,
        prev_sha256=norm_prev_sha,
        observed_sha256=observed_sha,
        etag_matched=etag_matched,
        drift_detected=True,
        degraded=False,
        should_reset_freshness=False,
        should_invalidate_dependents=True,
    )
