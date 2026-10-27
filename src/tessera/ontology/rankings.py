"""Empirical-Bayes shrunk rankings, Wilson bounds, composite scores, and Pareto frontiers.

Prevents sparse 1-of-1 lucky observations from outranking battle-tested 48-of-50 artifacts
by combining Beta/Dirichlet Empirical-Bayes shrinkage, Wilson lower confidence bounds
(z = 1.96), category-weighted dimension quality, exponential freshness confidence, and
verifier calibration weights.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .categories import CATEGORY_REGISTRY, get_category_spec
from .fragility import freshness_confidence, resolve_half_life_hours

DEFAULT_WILSON_Z: float = 1.96
DEFAULT_PRIOR_MEAN: float = 0.50
DEFAULT_PRIOR_WEIGHT: float = 10.0
DEFAULT_PARETO_AXES: tuple[str, ...] = (
    "quality",
    "token_efficiency",
    "freshness",
    "low_fragility",
)

_LOWER_IS_BETTER_AXES: frozenset[str] = frozenset({"fragility_score", "cost_usd", "latency_p95_ms"})

TItem = TypeVar("TItem", bound=Mapping[str, Any] | BaseModel)


class RankedArtifact(BaseModel):
    """Deterministic multi-axis ranking record for a categorized artifact."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    category_id: str
    trials: int = Field(ge=0)
    successes: float = Field(ge=0.0)
    observed_pass_rate: float = Field(ge=0.0, le=1.0)
    eb_pass_rate: float = Field(ge=0.0, le=1.0)
    wilson_lower_bound: float = Field(ge=0.0, le=1.0)
    raw_dimension_score: float = Field(ge=0.0, le=1.0)
    eb_dimension_score: float = Field(ge=0.0, le=1.0)
    freshness_confidence: float = Field(ge=0.0, le=1.0)
    calibration_weight: float = Field(ge=0.0, le=1.0)
    token_efficiency: float = Field(ge=0.0, le=1.0)
    low_fragility: float = Field(ge=0.0, le=1.0)
    quality: float = Field(ge=0.0, le=1.0)
    composite_score: float = Field(ge=0.0, le=1.0)
    axes: dict[str, float]


def empirical_bayes_mean(
    observed_mean: float,
    n: int | float,
    prior_mean: float = DEFAULT_PRIOR_MEAN,
    prior_weight: float = DEFAULT_PRIOR_WEIGHT,
) -> float:
    """Compute Empirical-Bayes posterior mean: (prior_weight * prior_mean + n * observed_mean) / (prior_weight + n)."""
    if n < 0:
        raise ValueError(f"Sample count n must be >= 0, got {n}")
    if prior_weight <= 0.0:
        raise ValueError(f"prior_weight must be > 0, got {prior_weight}")
    obs = max(0.0, min(1.0, float(observed_mean)))
    p_mean = max(0.0, min(1.0, float(prior_mean)))
    count = float(n)
    return (prior_weight * p_mean + count * obs) / (prior_weight + count)


def dirichlet_posterior_probs(
    counts: Mapping[str, float | int],
    prior_alphas: Mapping[str, float] | float = 2.0,
) -> dict[str, float]:
    """Compute multi-category Dirichlet posterior probabilities over K discrete outcomes."""
    if not counts:
        raise ValueError("counts must contain at least one outcome bucket")
    alphas: dict[str, float] = {}
    total = 0.0
    for key, raw_count in counts.items():
        c_val = float(raw_count)
        if c_val < 0.0:
            raise ValueError(f"Outcome count for {key!r} must be >= 0, got {c_val}")
        alpha = (
            float(prior_alphas[key]) if isinstance(prior_alphas, Mapping) else float(prior_alphas)
        )
        if alpha <= 0.0:
            raise ValueError(f"Dirichlet prior alpha for {key!r} must be > 0, got {alpha}")
        post = alpha + c_val
        alphas[key] = post
        total += post
    return {k: v / total for k, v in alphas.items()}


def dirichlet_shrunk_score(
    counts: Mapping[str, float | int],
    outcome_utilities: Mapping[str, float],
    prior_alphas: Mapping[str, float] | float = 2.0,
) -> float:
    """Expected utility in [0.0, 1.0] under a Dirichlet posterior over multinomial outcomes."""
    probs = dirichlet_posterior_probs(counts, prior_alphas=prior_alphas)
    score = 0.0
    for key, prob in probs.items():
        util = max(0.0, min(1.0, float(outcome_utilities.get(key, 0.0))))
        score += prob * util
    return max(0.0, min(1.0, score))


def wilson_interval(
    successes: int | float,
    trials: int | float,
    z: float = DEFAULT_WILSON_Z,
) -> tuple[float, float]:
    """Compute Wilson score confidence interval (lower, upper) for binomial proportion."""
    n = float(trials)
    if n < 0.0:
        raise ValueError(f"trials must be >= 0, got {trials}")
    if n == 0.0:
        return (0.0, 0.0)
    s = float(successes)
    if s < 0.0 or s > n + 1e-9:
        raise ValueError(
            f"successes ({successes}) must satisfy 0 <= successes <= trials ({trials})"
        )
    if z <= 0.0:
        raise ValueError(f"z must be > 0, got {z}")

    p_hat = max(0.0, min(1.0, s / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    radicand = max(0.0, (p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)
    margin = z * math.sqrt(radicand)
    lower = max(0.0, min(1.0, (center - margin) / denom))
    upper = max(0.0, min(1.0, (center + margin) / denom))
    return (lower, upper)


def wilson_lower_bound(
    successes: int | float,
    trials: int | float,
    z: float = DEFAULT_WILSON_Z,
) -> float:
    """Return Wilson lower confidence bound (default 95% two-sided z = 1.96)."""
    lower, _ = wilson_interval(successes, trials, z=z)
    return lower


def compute_category_dimension_score(
    category_id: str,
    dimensions: Mapping[str, float | int | str],
) -> tuple[float, dict[str, float]]:
    """Compute weighted normalized dimension score in [0.0, 1.0] for a category."""
    if category_id not in CATEGORY_REGISTRY:
        # Fallback average of numeric [0, 1] values when uncategorized
        norm: dict[str, float] = {}
        for k, v in dimensions.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fv = max(0.0, min(1.0, float(v)))
                norm[k] = (1.0 - fv) if k in _LOWER_IS_BETTER_AXES else fv
        mean_score = (sum(norm.values()) / len(norm)) if norm else DEFAULT_PRIOR_MEAN
        return (mean_score, norm)

    spec = get_category_spec(category_id)
    norm_dims: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0

    for axis_name, dim_spec in spec.dimensions.items():
        if axis_name not in dimensions:
            continue
        try:
            n_val = dim_spec.normalize_value(dimensions[axis_name])
        except ValueError:
            continue
        norm_dims[axis_name] = n_val
        weighted_sum += dim_spec.default_weight * n_val
        weight_total += dim_spec.default_weight

    if weight_total <= 0.0:
        return (DEFAULT_PRIOR_MEAN, norm_dims)
    return (max(0.0, min(1.0, weighted_sum / weight_total)), norm_dims)


def compute_composite_rank(
    item: Mapping[str, Any] | BaseModel,
    *,
    now_ts: float | int | str | None = None,
    prior_mean: float = DEFAULT_PRIOR_MEAN,
    prior_weight: float = DEFAULT_PRIOR_WEIGHT,
    wilson_z: float = DEFAULT_WILSON_Z,
) -> RankedArtifact:
    """Compute freshness- and calibration-adjusted Empirical-Bayes composite rank."""
    rec = item.model_dump() if isinstance(item, BaseModel) else dict(item)
    item_id = str(rec.get("item_id") or rec.get("id") or "")
    if not item_id:
        raise ValueError("Rankable item requires 'item_id' or 'id'")

    category_id = str(rec.get("category_id") or "noun.code_node")
    dimensions = dict(rec.get("dimensions") or {})

    trials = int(rec.get("trials", rec.get("sample_size", 0)))
    if "successes" in rec and rec["successes"] is not None:
        successes = float(rec["successes"])
    elif "observed_mean" in rec and rec["observed_mean"] is not None:
        successes = float(rec["observed_mean"]) * max(0, trials)
    else:
        successes = float(trials)

    successes = max(0.0, min(float(trials), successes)) if trials > 0 else 0.0
    obs_pass = (successes / trials) if trials > 0 else prior_mean

    eb_pass = empirical_bayes_mean(
        obs_pass, trials, prior_mean=prior_mean, prior_weight=prior_weight
    )
    w_lb = wilson_lower_bound(successes, trials, z=wilson_z) if trials > 0 else 0.0

    raw_dim_score, norm_dims = compute_category_dimension_score(category_id, dimensions)
    eb_dim_score = empirical_bayes_mean(
        raw_dim_score,
        trials,
        prior_mean=prior_mean,
        prior_weight=prior_weight,
    )

    # Resolve freshness confidence directly or via exponential decay
    if "freshness_confidence" in rec and rec["freshness_confidence"] is not None:
        fresh = max(0.0, min(1.0, float(rec["freshness_confidence"])))
    elif "freshness" in rec and rec["freshness"] is not None:
        fresh = max(0.0, min(1.0, float(rec["freshness"])))
    elif now_ts is not None and "last_verified_at" in rec:
        hl = resolve_half_life_hours(
            category_id,
            override_half_life_hours=rec.get("half_life_hours"),
        )
        fresh = freshness_confidence(rec["last_verified_at"], now_ts, hl)
    else:
        fresh = 1.0

    calibration = max(0.0, min(1.0, float(rec.get("calibration_weight", 1.0))))

    # Empirical quality blends shrunk dimension score, shrunk pass rate, and Wilson lower bound
    quality = 0.40 * eb_dim_score + 0.35 * eb_pass + 0.25 * w_lb
    composite = quality * fresh * calibration

    raw_tok_eff = rec.get(
        "token_efficiency",
        norm_dims.get("token_efficiency", dimensions.get("token_efficiency", prior_mean)),
    )
    token_eff = float(prior_mean if raw_tok_eff is None else raw_tok_eff)
    token_eff = max(0.0, min(1.0, token_eff))

    if "low_fragility" in rec and rec["low_fragility"] is not None:
        low_frag = max(0.0, min(1.0, float(rec["low_fragility"])))
    elif "fragility_score" in norm_dims:
        low_frag = max(0.0, min(1.0, norm_dims["fragility_score"]))
    elif "fragility_score" in rec and rec["fragility_score"] is not None:
        low_frag = max(0.0, min(1.0, 1.0 - float(rec["fragility_score"])))
    elif "fragility_score" in dimensions:
        low_frag = max(0.0, min(1.0, 1.0 - float(dimensions["fragility_score"])))
    else:
        low_frag = 0.85

    axes_map: dict[str, float] = {
        **{k: round(v, 6) for k, v in norm_dims.items()},
        "quality": round(quality, 6),
        "token_efficiency": round(token_eff, 6),
        "freshness": round(fresh, 6),
        "low_fragility": round(low_frag, 6),
        "wilson_lower_bound": round(w_lb, 6),
        "composite_score": round(composite, 6),
    }

    return RankedArtifact(
        item_id=item_id,
        category_id=category_id,
        trials=trials,
        successes=round(successes, 6),
        observed_pass_rate=round(obs_pass, 6),
        eb_pass_rate=round(eb_pass, 6),
        wilson_lower_bound=round(w_lb, 6),
        raw_dimension_score=round(raw_dim_score, 6),
        eb_dimension_score=round(eb_dim_score, 6),
        freshness_confidence=round(fresh, 6),
        calibration_weight=round(calibration, 6),
        token_efficiency=round(token_eff, 6),
        low_fragility=round(low_frag, 6),
        quality=round(quality, 6),
        composite_score=round(composite, 6),
        axes=axes_map,
    )


def rank_items(
    items: Sequence[Mapping[str, Any] | BaseModel],
    *,
    sort_axis: str = "composite_score",
    now_ts: float | int | str | None = None,
    prior_mean: float = DEFAULT_PRIOR_MEAN,
    prior_weight: float = DEFAULT_PRIOR_WEIGHT,
    wilson_z: float = DEFAULT_WILSON_Z,
    top_k: int | None = None,
) -> list[RankedArtifact]:
    """Rank items deterministically along sort_axis (default composite_score)."""
    evaluated = [
        compute_composite_rank(
            it,
            now_ts=now_ts,
            prior_mean=prior_mean,
            prior_weight=prior_weight,
            wilson_z=wilson_z,
        )
        for it in items
    ]

    def _sort_key(entry: RankedArtifact) -> tuple[float, float, int, str]:
        primary = entry.axes.get(sort_axis, getattr(entry, sort_axis, entry.composite_score))
        return (
            -float(primary),
            -entry.wilson_lower_bound,
            -entry.trials,
            entry.item_id,
        )

    evaluated.sort(key=_sort_key)
    return evaluated[:top_k] if top_k is not None else evaluated


def build_multi_axis_leaderboards(
    items: Sequence[Mapping[str, Any] | BaseModel],
    axes: Sequence[str] = ("composite_score", *DEFAULT_PARETO_AXES),
    *,
    now_ts: float | int | str | None = None,
    top_k: int | None = None,
) -> dict[str, list[RankedArtifact]]:
    """Construct deterministic per-axis leaderboards from a single evaluation pass."""
    base_ranked = rank_items(items, now_ts=now_ts)
    boards: dict[str, list[RankedArtifact]] = {}
    for axis in axes:
        ordered = sorted(
            base_ranked,
            key=lambda e: (
                -float(e.axes.get(axis, getattr(e, axis, e.composite_score))),
                -e.composite_score,
                -e.trials,
                e.item_id,
            ),
        )
        boards[axis] = ordered[:top_k] if top_k is not None else ordered
    return boards


def _extract_pareto_coordinate(item: Mapping[str, Any] | BaseModel, axis: str) -> float:
    data: Mapping[str, Any] = item.model_dump() if isinstance(item, BaseModel) else item
    raw_axes = data.get("axes")
    axes_sub: Mapping[str, Any] = raw_axes if isinstance(raw_axes, Mapping) else {}
    raw_dims = data.get("dimensions")
    dims_sub: Mapping[str, Any] = raw_dims if isinstance(raw_dims, Mapping) else {}

    raw_val: Any = None
    if axis in data and data[axis] is not None:
        raw_val = data[axis]
    elif axis in axes_sub and axes_sub[axis] is not None:
        raw_val = axes_sub[axis]
    elif axis in dims_sub and dims_sub[axis] is not None:
        raw_val = dims_sub[axis]
    elif axis == "low_fragility":
        frag = data.get("fragility_score", dims_sub.get("fragility_score"))
        if frag is not None:
            return 1.0 - float(frag)
    elif axis == "freshness":
        fc = data.get("freshness_confidence")
        if fc is not None:
            return float(fc)
    elif axis == "quality":
        for fallback in ("composite_score", "eb_dimension_score", "quality_score"):
            if fallback in data and data[fallback] is not None:
                return float(data[fallback])

    if raw_val is None:
        raise KeyError(
            f"Item {data.get('item_id', data.get('id', '<unnamed>'))} missing Pareto axis {axis!r}"
        )

    fval = float(raw_val)
    return -fval if axis in _LOWER_IS_BETTER_AXES else fval


def _extract_item_id(item: Mapping[str, Any] | BaseModel, fallback_idx: int) -> str:
    if isinstance(item, BaseModel):
        dumped = item.model_dump()
        return str(dumped.get("item_id") or dumped.get("id") or f"idx_{fallback_idx:04d}")
    return str(item.get("item_id") or item.get("id") or f"idx_{fallback_idx:04d}")


def pareto_frontier[TItem: Mapping[str, Any] | BaseModel](
    items: Sequence[TItem],
    axes: Sequence[str] = DEFAULT_PARETO_AXES,
) -> list[TItem]:
    """Select the non-dominated Pareto frontier across the specified maximize-oriented axes."""
    if not axes:
        raise ValueError("axes must contain at least one axis name")
    if not items:
        return []

    eps = 1e-12
    coords: list[tuple[float, ...]] = [
        tuple(_extract_pareto_coordinate(item, ax) for ax in axes) for item in items
    ]

    frontier_indices: list[int] = []
    n = len(items)
    for i in range(n):
        vec_i = coords[i]
        dominated = False
        for j in range(n):
            if i == j:
                continue
            vec_j = coords[j]
            all_geq = all(vj >= vi - eps for vj, vi in zip(vec_j, vec_i, strict=False))
            any_gt = any(vj > vi + eps for vj, vi in zip(vec_j, vec_i, strict=False))
            if all_geq and any_gt:
                dominated = True
                break
        if not dominated:
            frontier_indices.append(i)

    frontier_indices.sort(
        key=lambda idx: (
            -sum(coords[idx]) / len(axes),
            _extract_item_id(items[idx], idx),
        )
    )
    return [items[idx] for idx in frontier_indices]
