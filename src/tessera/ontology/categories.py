"""Categorized Noun and Verb taxonomy, typed dimensions, relations, and worker guidance.

Provides the 20 canonical Noun categories and 18 canonical Verb categories for shared
knowledge graphs, deterministic worker preflight checks, category-specific domain axes,
value rubrics, recovery playbooks, payload validation, and SQLite catalog persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tessera.models import category_from_tag

CategoryKind = Literal["noun", "verb"]
DimensionDirection = Literal["higher_is_better", "lower_is_better"]
DimensionDType = Literal["float", "int", "categorical"]
FragilityDomain = Literal[
    "static_code",
    "regulatory_statute",
    "mcp_api_schema",
    "live_feed",
]

RelationType = Literal[
    "derives_from",
    "supersedes",
    "contradicts",
    "corroborates",
    "verifies",
    "requires_noun",
    "produces_noun",
    "rebutted_by_tombstone",
    "compiled_into_dag",
    "adapted_for_cohort",
]

ALLOWED_RELATION_TYPES: tuple[RelationType, ...] = (
    "derives_from",
    "supersedes",
    "contradicts",
    "corroborates",
    "verifies",
    "requires_noun",
    "produces_noun",
    "rebutted_by_tombstone",
    "compiled_into_dag",
    "adapted_for_cohort",
)

VERIFICATION_TIERS: tuple[str, ...] = (
    "unverified",
    "deterministic_gate",
    "multi_verifier",
    "steward_attested",
)

DEFAULT_HISTORY_METRICS: tuple[str, ...] = (
    "version_count",
    "drift_rate_30d",
    "reuse_hits",
    "mean_verify_ms",
)

_TAG_CLEAN_RE = re.compile(r"[^a-z0-9._:-]+")
_MULTI_HYPHEN_RE = re.compile(r"-{2,}")


def _default_value_rubric() -> dict[str, str]:
    return {
        "high": "Verified, deterministic, citation-grounded artifact with zero unmitigated caveats.",
        "medium": "Partially verified artifact meeting required dimensions with bounded caveats.",
        "low": "Unverified, stale, or sparse artifact lacking reproducible records.",
    }


class DimensionSpec(BaseModel):
    """Specification of a numeric or categorical measurement axis on a category."""

    model_config = ConfigDict(frozen=True)

    name: str
    dtype: DimensionDType = "float"
    min_val: float = 0.0
    max_val: float = 1.0
    allowed_values: tuple[str, ...] = ()
    unit: str = "ratio"
    direction: DimensionDirection = "higher_is_better"
    default_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    required: bool = True

    @model_validator(mode="after")
    def _check_bounds(self) -> DimensionSpec:
        if self.dtype in ("float", "int"):
            if self.max_val <= self.min_val:
                raise ValueError(
                    f"Dimension {self.name}: max_val ({self.max_val}) must exceed min_val ({self.min_val})"
                )
        elif self.dtype == "categorical" and len(self.allowed_values) < 2:
            raise ValueError(
                f"Categorical dimension {self.name} requires at least 2 allowed_values"
            )
        return self

    def normalize_value(self, raw_value: float | int | str) -> float:
        """Project a valid raw value onto [0.0, 1.0] where 1.0 is always optimal."""
        if self.dtype == "categorical":
            if not isinstance(raw_value, str) or raw_value not in self.allowed_values:
                raise ValueError(
                    f"Invalid categorical value {raw_value!r} for {self.name}; expected one of {self.allowed_values}"
                )
            idx = self.allowed_values.index(raw_value)
            ratio = idx / float(len(self.allowed_values) - 1)
            return ratio if self.direction == "higher_is_better" else (1.0 - ratio)

        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(
                f"Dimension {self.name} requires numeric value, got {type(raw_value).__name__}"
            )
        val = float(raw_value)
        span = self.max_val - self.min_val
        clamped = max(self.min_val, min(self.max_val, val))
        if self.direction == "higher_is_better":
            return (clamped - self.min_val) / span
        return (self.max_val - clamped) / span


class WorkerGuidance(BaseModel):
    """Structured pre-flight context, MCP requirements, 0-call gates, and record rules."""

    model_config = ConfigDict(frozen=True)

    preflight_context: tuple[str, ...]
    required_mcp_tools: tuple[str, ...]
    deterministic_checks_before_llm: tuple[str, ...]
    post_condition_receipt_rules: tuple[str, ...]


class CategorySpec(BaseModel):
    """Complete ontology contract for a canonical Task Object or Task category."""

    model_config = ConfigDict(frozen=True)

    category_id: str
    kind: CategoryKind
    title: str
    summary: str
    fragility_domain: FragilityDomain
    default_half_life_hours: float = Field(gt=0.0)
    human_sensitive: bool = False
    dimensions: dict[str, DimensionSpec]
    allowed_relations: tuple[RelationType, ...]
    worker_guidance: WorkerGuidance
    canonical_tags: tuple[str, ...] = ()
    domain_dimensions: tuple[str, ...] = ()
    value_rubric: dict[str, str] = Field(default_factory=_default_value_rubric)
    history_metrics: tuple[str, ...] = DEFAULT_HISTORY_METRICS
    priority_formula: str = "composite_score * freshness_confidence * calibration_weight"
    recovery_playbook: tuple[str, ...] = (
        "Inspect negative.knowledge tombstones and validation errors.",
        "Re-run deterministic preflight checks before retrying synthesis.",
    )
    suggested_verbs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_spec(self) -> CategorySpec:
        prefix = f"{self.kind}."
        if not self.category_id.startswith(prefix):
            raise ValueError(f"Category {self.category_id} must start with {prefix!r}")
        if not self.dimensions:
            raise ValueError(f"Category {self.category_id} must define at least one dimension")
        total_weight = sum(d.default_weight for d in self.dimensions.values())
        if abs(total_weight - 1.0) > 1e-6:
            raise ValueError(
                f"Category {self.category_id} dimension weights must sum to 1.0, got {total_weight:.6f}"
            )
        if not self.allowed_relations:
            raise ValueError(f"Category {self.category_id} must allow at least one relation")
        return self


class RelationEdge(BaseModel):
    """Typed directed edge connecting two categorized ontology records."""

    model_config = ConfigDict(frozen=True)

    relation: RelationType
    target_id: str
    target_category: str | None = None
    note: str = ""


class CategoryValidationResult(BaseModel):
    """Result of validating a candidate Task Object or Task payload against its CategorySpec."""

    model_config = ConfigDict(frozen=True)

    category_id: str
    valid: bool
    conformance_score: float = Field(ge=0.0, le=1.0)
    quality_score: float = Field(ge=0.0, le=1.0)
    normalized_dimensions: dict[str, float]
    normalized_tags: tuple[str, ...]
    validated_relations: tuple[RelationEdge, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    payload_sha256: str


def _build_dimensions(
    weights: Mapping[str, float],
    *,
    max_latency_ms: float = 30000.0,
    max_cost_usd: float = 5.0,
    required_axes: tuple[str, ...] = (
        "determinism",
        "evidence_grounding",
        "token_efficiency",
        "safety_compliance",
    ),
) -> dict[str, DimensionSpec]:
    """Build standard comparable numeric and categorical dimensions with tailored weights."""
    req = set(required_axes)
    base_defs: dict[str, dict[str, Any]] = {
        "determinism": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "ratio",
            "direction": "higher_is_better",
        },
        "evidence_grounding": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "ratio",
            "direction": "higher_is_better",
        },
        "token_efficiency": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "ratio",
            "direction": "higher_is_better",
        },
        "safety_compliance": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "ratio",
            "direction": "higher_is_better",
        },
        "reusability": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "ratio",
            "direction": "higher_is_better",
        },
        "latency_p95_ms": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": max_latency_ms,
            "unit": "ms",
            "direction": "lower_is_better",
        },
        "cost_usd": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": max_cost_usd,
            "unit": "usd",
            "direction": "lower_is_better",
        },
        "fragility_score": {
            "dtype": "float",
            "min_val": 0.0,
            "max_val": 1.0,
            "unit": "score",
            "direction": "lower_is_better",
        },
        "verification_tier": {
            "dtype": "categorical",
            "min_val": 0.0,
            "max_val": 3.0,
            "allowed_values": VERIFICATION_TIERS,
            "unit": "tier",
            "direction": "higher_is_better",
        },
    }
    specs: dict[str, DimensionSpec] = {}
    for axis_name, weight in weights.items():
        cfg = base_defs[axis_name]
        specs[axis_name] = DimensionSpec(
            name=axis_name,
            dtype=cfg["dtype"],
            min_val=cfg["min_val"],
            max_val=cfg["max_val"],
            allowed_values=cfg.get("allowed_values", ()),
            unit=cfg["unit"],
            direction=cfg["direction"],
            default_weight=weight,
            required=(axis_name in req),
        )
    return specs


# ---------------------------------------------------------------------------
# 20 Canonical Noun Categories
# ---------------------------------------------------------------------------

NOUN_CATEGORIES: dict[str, CategorySpec] = {
    "noun.code_node": CategorySpec(
        category_id="noun.code_node",
        kind="noun",
        title="Verified Code Node",
        summary="Typed function, class, module, or AST unit with deterministic test coverage.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.24,
                "evidence_grounding": 0.14,
                "token_efficiency": 0.14,
                "safety_compliance": 0.14,
                "reusability": 0.14,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.04,
                "fragility_score": 0.06,
                "verification_tier": 0.04,
            },
            max_latency_ms=5000.0,
            max_cost_usd=1.0,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load negative.knowledge tombstones matching symbol signature and package path.",
                "Inspect existing callers, type annotations, and benchmark assertions.",
            ),
            required_mcp_tools=(
                "repo.read_symbol",
                "ast.check_syntax",
                "test.run_pytest",
            ),
            deterministic_checks_before_llm=(
                "Verify exact Digest content match in local store to skip redundant synthesis.",
                "Run static syntax/import parser and type-stub lookup before LLM invocation.",
            ),
            post_condition_receipt_rules=(
                "Attach Digest of normalized source bytes and test suite exit code 0.",
                "Record symbol export list and dependency edges.",
            ),
        ),
        canonical_tags=("code", "deterministic", "ast"),
        domain_dimensions=(
            "ast_complexity",
            "cyclomatic_bound",
            "pure_function_ratio",
            "test_branch_coverage",
        ),
        value_rubric={
            "high": "Pure, strictly typed AST unit with 100% deterministic unit test pass record and zero lint warnings.",
            "medium": "Typed code unit passing core tests with bounded side effects and documented callers.",
            "low": "Untyped or untested snippet with unresolved imports or flaky assertions.",
        },
        history_metrics=("version_count", "test_pass_streak", "reuse_hits", "mean_verify_ms"),
        priority_formula="0.45 * wilson_lower_bound + 0.35 * determinism + 0.20 * reusability",
        recovery_playbook=(
            "Parse syntax/type errors deterministically and check negative.knowledge tombstones for symbol.",
            "Isolate failing test case, apply minimal AST patch, and re-run Task.verify_gate.",
            "Emit negative_tombstone Task Object if candidate regresses existing callers.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.statute_card": CategorySpec(
        category_id="noun.statute_card",
        kind="noun",
        title="Regulatory Statute Card",
        summary="Jurisdiction-scoped statutory or regulatory provision with effective dates and citations.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.16,
                "evidence_grounding": 0.30,
                "token_efficiency": 0.08,
                "safety_compliance": 0.22,
                "reusability": 0.08,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.05,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load official statutory citation URI, jurisdiction code, and effective date window.",
                "Check repeal/amendment negative tombstones for the cited section.",
            ),
            required_mcp_tools=("corpus.lookup_statute", "citation.verify_hash"),
            deterministic_checks_before_llm=(
                "Validate jurisdiction ISO code and statutory section identifier regex.",
                "Verify primary source snapshot Digest before summarizing.",
            ),
            post_condition_receipt_rules=(
                "Include verbatim statutory excerpt hash, jurisdiction tag, and non-legal-advice caveat.",
                "Flag card for human steward review when effective date is ambiguous.",
            ),
        ),
        canonical_tags=("statute", "compliance", "cited"),
        domain_dimensions=(
            "jurisdiction_iso",
            "effective_window_days",
            "primary_citation_verified",
            "amendment_volatility",
        ),
        value_rubric={
            "high": "Primary-source hash-verified provision with explicit jurisdiction ISO, effective window, and caveats.",
            "medium": "Cited provision with valid jurisdiction tag awaiting secondary amendment recheck.",
            "low": "Uncited summary or provision with expired or ambiguous effective dates.",
        },
        history_metrics=(
            "version_count",
            "amendment_checks_90d",
            "citation_reuse_hits",
            "steward_attestations",
        ),
        priority_formula="0.45 * evidence_grounding + 0.30 * safety_compliance + 0.25 * freshness_confidence",
        recovery_playbook=(
            "Re-probe primary statutory registry URI to verify section numbering and effective dates.",
            "Quarantine superseded text and park ambiguous provisions for human_steward review.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.probe_source",
            "verb.verify_gate",
            "verb.audit_profile_parity",
        ),
    ),
    "noun.ngo_kit": CategorySpec(
        category_id="noun.ngo_kit",
        kind="noun",
        title="NGO Field Operations Kit",
        summary="Actionable humanitarian or legal-aid intake checklist, workflow, and safeguard packet.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.14,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.12,
                "safety_compliance": 0.26,
                "reusability": 0.10,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.04,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "corroborates",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load linked statute_card Task Object records and regional language/literacy constraints.",
                "Check brief rules and AI-generated media disclosure requirements.",
            ),
            required_mcp_tools=("corpus.lookup_statute", "safety.pii_redactor"),
            deterministic_checks_before_llm=(
                "Verify zero PII tokens in template placeholders.",
                "Confirm every procedural step links to a grounded statute or agency guideline.",
            ),
            post_condition_receipt_rules=(
                "Emit checklist Digest, plain-language reading grade metric, and human escalation instructions.",
            ),
        ),
        canonical_tags=("ngo", "field-kit", "human-safeguard"),
        domain_dimensions=(
            "reading_grade_score",
            "checklist_completeness",
            "escalation_clarity",
            "pii_zero_leak_score",
        ),
        value_rubric={
            "high": "Plain-language, PII-free intake packet grounded in verified statute cards with clear escalation gates.",
            "medium": "Structured intake checklist with grounded citations needing plain-language tightening.",
            "low": "Unverified checklist missing escalation triggers or statutory grounding.",
        },
        history_metrics=(
            "version_count",
            "field_adoption_hits",
            "drift_rate_30d",
            "steward_signoffs",
        ),
        priority_formula="0.40 * safety_compliance + 0.35 * evidence_grounding + 0.25 * reusability",
        recovery_playbook=(
            "Run deterministic PII redactor over all template fields and strip ungrounded steps.",
            "Re-link steps to current statute_card Task Object versions and route to human_steward.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.audit_profile_parity",
            "verb.verify_gate",
        ),
    ),
    "noun.case_bundle": CategorySpec(
        category_id="noun.case_bundle",
        kind="noun",
        title="Structured Case Bundle",
        summary="PII-redacted case facts, timeline evidence, applicable statute links, and open caveats.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.15,
                "evidence_grounding": 0.28,
                "token_efficiency": 0.08,
                "safety_compliance": 0.28,
                "reusability": 0.05,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.04,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Run mandatory PII/PHI scrubbing rules and check consent/redaction flags.",
                "Load corroborating feed sources and applicable statute_card Task Object citations.",
            ),
            required_mcp_tools=("safety.pii_redactor", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Execute deterministic regex/NER redaction scan before any model call.",
                "Verify every exhibit hash exists in the evidence manifest.",
            ),
            post_condition_receipt_rules=(
                "Record redaction pass record, exhibit Digest list, and mandatory human sign-off requirement.",
            ),
        ),
        canonical_tags=("case-bundle", "redacted", "human-gated"),
        domain_dimensions=(
            "redaction_completeness",
            "exhibit_hash_coverage",
            "timeline_consistency",
            "caveat_disclosure_ratio",
        ),
        value_rubric={
            "high": "100% PII-redacted bundle with manifest-verified exhibit Digest hashes and steward sign-off gate.",
            "medium": "Redacted timeline bundle with partial exhibit corroboration parked for human review.",
            "low": "Incomplete exhibit manifest or unresolved redaction warnings.",
        },
        history_metrics=("version_count", "exhibit_count", "redaction_audits", "steward_reviews"),
        priority_formula="0.45 * safety_compliance + 0.40 * evidence_grounding + 0.15 * determinism",
        recovery_playbook=(
            "Halt downstream propagation immediately and run deterministic PII/PHI scrubber.",
            "Recompute exhibit manifest Digest digests and park task for human_steward sign-off.",
        ),
        suggested_verbs=(
            "verb.ingest",
            "verb.situate",
            "verb.verify_gate",
            "verb.vote_judge",
        ),
    ),
    "noun.shim_provision": CategorySpec(
        category_id="noun.shim_provision",
        kind="noun",
        title="Compatibility Shim Provision",
        summary="Deterministic protocol, schema, or SDK adapter bridging mismatched tool interfaces.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.25,
                "evidence_grounding": 0.12,
                "token_efficiency": 0.16,
                "safety_compliance": 0.12,
                "reusability": 0.16,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.04,
                "fragility_score": 0.05,
                "verification_tier": 0.04,
            },
            max_latency_ms=2000.0,
            max_cost_usd=0.5,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load source and target JSON schemas plus known breaking-change tombstones.",
            ),
            required_mcp_tools=("schema.validate_json", "test.contract_probe"),
            deterministic_checks_before_llm=(
                "Diff source and target field maps deterministically before drafting adapter glue.",
            ),
            post_condition_receipt_rules=(
                "Include round-trip contract test record and upstream version pin.",
            ),
        ),
        canonical_tags=("shim", "adapter", "schema-bridge"),
        domain_dimensions=(
            "roundtrip_fidelity",
            "schema_diff_coverage",
            "backward_compat_score",
            "bridge_overhead_ms",
        ),
        value_rubric={
            "high": "Lossless bidirectional schema adapter pinned to upstream versions with contract test records.",
            "medium": "One-way protocol adapter covering required fields with documented defaults.",
            "low": "Unpinned adapter lacking contract tests or dropping required fields.",
        },
        history_metrics=("version_count", "schema_drift_events", "reuse_hits", "mean_verify_ms"),
        priority_formula="0.35 * determinism + 0.30 * reusability + 0.35 * freshness_confidence",
        recovery_playbook=(
            "Diff upstream JSON Schema snapshot against pinned contract to isolate drifted fields.",
            "Regenerate field coercion map and execute round-trip contract test gate.",
        ),
        suggested_verbs=(
            "verb.probe_source",
            "verb.solve_draft",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.scene_recipe": CategorySpec(
        category_id="noun.scene_recipe",
        kind="noun",
        title="Declarative Scene Recipe",
        summary="Reproducible 2D/3D spatial layout, lighting, camera rig, and asset binding graph.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.20,
                "evidence_grounding": 0.12,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.18,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.04,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Inspect coordinate-system convention, bounding boxes, and linked motion_profile Task Object assets.",
            ),
            required_mcp_tools=("scene.validate_bounds", "asset.resolve_manifest"),
            deterministic_checks_before_llm=(
                "Verify asset URIs, frame budgets, and non-overlapping collision bounds.",
            ),
            post_condition_receipt_rules=(
                "Record deterministic seed, scene graph Digest, and generated-media watermark metadata if rendered.",
            ),
        ),
        canonical_tags=("scene", "spatial", "kinematics"),
        domain_dimensions=(
            "bounding_box_validity",
            "asset_manifest_completeness",
            "frame_budget_adherence",
            "camera_frustum_coverage",
        ),
        value_rubric={
            "high": "Seed-deterministic spatial graph with resolved asset hashes and zero collision violations.",
            "medium": "Valid scene layout with placeholder assets within frame budget.",
            "low": "Unbounded coordinates, broken asset references, or missing RNG seed.",
        },
        history_metrics=("version_count", "render_pass_rate", "reuse_hits", "mean_verify_ms"),
        priority_formula="0.35 * determinism + 0.35 * reusability + 0.30 * token_efficiency",
        recovery_playbook=(
            "Re-validate coordinate system handedness, bounding boxes, and asset Digest URIs.",
            "Clamp out-of-range transforms and re-run scene bounds gate.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.compile_dag",
            "verb.verify_gate",
        ),
    ),
    "noun.motion_profile": CategorySpec(
        category_id="noun.motion_profile",
        kind="noun",
        title="Kinematic Motion Profile",
        summary="Parameterized keyframe curves, velocity/jerk limits, and rig joint constraints.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.24,
                "evidence_grounding": 0.12,
                "token_efficiency": 0.14,
                "safety_compliance": 0.14,
                "reusability": 0.16,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.04,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load joint degree-of-freedom table, frame rate, and maximum acceleration limits.",
            ),
            required_mcp_tools=("motion.check_kinematics", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Check spline continuity (C1/C2) and joint angle bounds analytically.",
            ),
            post_condition_receipt_rules=(
                "Emit trajectory curve digest, peak velocity/jerk metrics, and frame count.",
            ),
        ),
        canonical_tags=("motion", "trajectory", "rig"),
        domain_dimensions=(
            "spline_c2_continuity",
            "joint_limit_margin",
            "peak_jerk_bound",
            "frame_rate_lock",
        ),
        value_rubric={
            "high": "C2-continuous trajectory within all joint torque, velocity, and jerk bounds.",
            "medium": "C1-continuous keyframe curve within joint angle limits.",
            "low": "Discontinuous curve or joint-limit violation.",
        },
        history_metrics=("version_count", "kinematic_pass_rate", "reuse_hits", "mean_verify_ms"),
        priority_formula="0.40 * determinism + 0.30 * safety_compliance + 0.30 * reusability",
        recovery_playbook=(
            "Clamp joint angles to rig degree-of-freedom limits and smooth C1/C2 discontinuities.",
            "Re-verify peak velocity and jerk analytically before committing.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.mcp_server": CategorySpec(
        category_id="noun.mcp_server",
        kind="noun",
        title="MCP Server Capability Card",
        summary="Model Context Protocol server descriptor, tool JSON schemas, auth scope, and transport spec.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.20,
                "evidence_grounding": 0.14,
                "token_efficiency": 0.16,
                "safety_compliance": 0.18,
                "reusability": 0.12,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.03,
                "fragility_score": 0.05,
                "verification_tier": 0.04,
            },
            max_latency_ms=10000.0,
            max_cost_usd=1.0,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load tool input/output JSON schemas, sandbox permission policy, and ETag fingerprint.",
            ),
            required_mcp_tools=("mcp.introspect_tools", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Validate every tool schema against JSON Schema Draft 2020-12 and check least-privilege scopes.",
            ),
            post_condition_receipt_rules=(
                "Record tool list Digest, transport protocol, and probe HTTP status.",
            ),
        ),
        canonical_tags=("mcp", "tool-schema", "capability"),
        domain_dimensions=(
            "schema_completeness",
            "transport_reliability",
            "scope_least_privilege",
            "idempotency_support",
        ),
        value_rubric={
            "high": "Streamable HTTP MCP server with Draft 2020-12 schemas, least-privilege scopes, and high liveness.",
            "medium": "Valid MCP tool list with partial output schemas or higher latency.",
            "low": "Unreachable MCP endpoint or malformed inputSchema definitions.",
        },
        history_metrics=("version_count", "probe_pass_rate_7d", "reuse_hits", "p95_rpc_ms"),
        priority_formula="0.35 * wilson_lower_bound + 0.35 * safety_compliance + 0.30 * freshness_confidence",
        recovery_playbook=(
            "Run tools/list introspection probe and diff tool schemas against cached Digest.",
            "Generate shim_provision Task Object if parameter names drifted across server versions.",
        ),
        suggested_verbs=(
            "verb.probe_source",
            "verb.verify_gate",
            "verb.recheck_fragility",
            "verb.audit_profile_parity",
        ),
    ),
    "noun.model_spec": CategorySpec(
        category_id="noun.model_spec",
        kind="noun",
        title="Model Capability Specification",
        summary="Inference model card covering context window, modalities, token pricing, and empirical calibration.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.14,
                "evidence_grounding": 0.18,
                "token_efficiency": 0.18,
                "safety_compliance": 0.16,
                "reusability": 0.10,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.08,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "corroborates",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load provider pricing table, context-window limits, and benchmark suite scores.",
            ),
            required_mcp_tools=("registry.lookup_model", "benchmark.read_summary"),
            deterministic_checks_before_llm=(
                "Verify max input/output token bounds and non-negative per-million token rates.",
            ),
            post_condition_receipt_rules=(
                "Attach provider model ID, pricing timestamp, and empirical pass-rate summary.",
            ),
        ),
        canonical_tags=("model-card", "routing", "pricing"),
        domain_dimensions=(
            "context_window_tokens",
            "empirical_calibration_ece",
            "cost_per_1m_tokens",
            "tool_call_accuracy",
        ),
        value_rubric={
            "high": "Empirically calibrated model card with verified pricing, context bounds, and benchmark records.",
            "medium": "Provider-declared model specification awaiting multi-arm harness benchmark.",
            "low": "Stale pricing or unverified context window claims.",
        },
        history_metrics=(
            "version_count",
            "pricing_drift_30d",
            "routing_selections",
            "mean_latency_ms",
        ),
        priority_formula="0.35 * token_efficiency + 0.35 * wilson_lower_bound + 0.30 * freshness_confidence",
        recovery_playbook=(
            "Re-fetch provider model metadata and token rate table.",
            "Schedule Task.benchmark_harness run to recalibrate empirical pass rates.",
        ),
        suggested_verbs=(
            "verb.probe_source",
            "verb.benchmark_harness",
            "verb.verify_gate",
            "verb.recheck_fragility",
        ),
    ),
    "noun.benchmark_suite": CategorySpec(
        category_id="noun.benchmark_suite",
        kind="noun",
        title="Deterministic Benchmark Suite",
        summary="Reproducible evaluation harness, golden fixtures, scoring rubric, and anti-contamination hashes.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.10,
                "safety_compliance": 0.12,
                "reusability": 0.14,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.04,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load fixture manifest hashes and deterministic RNG seed configuration.",
            ),
            required_mcp_tools=("test.run_pytest", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Confirm zero network calls and bit-identical repeat scoring on golden fixtures.",
            ),
            post_condition_receipt_rules=(
                "Record fixture manifest Digest, rubric version, and self-test pass count.",
            ),
        ),
        canonical_tags=("benchmark", "eval", "golden-suite"),
        domain_dimensions=(
            "repeat_run_determinism",
            "fixture_hash_integrity",
            "contamination_guard_score",
            "rubric_discriminative_power",
        ),
        value_rubric={
            "high": "Offline-reproducible golden suite with locked Digest manifest and bit-identical repeat scores.",
            "medium": "Deterministic suite covering primary cases with minor fixture gaps.",
            "low": "Network-dependent or non-deterministic evaluation script.",
        },
        history_metrics=("version_count", "eval_runs_executed", "flake_rate_30d", "mean_verify_ms"),
        priority_formula="0.45 * determinism + 0.35 * evidence_grounding + 0.20 * reusability",
        recovery_playbook=(
            "Pin all random seeds, mock external I/O, and recompute golden fixture Digest hashes.",
            "Execute two consecutive self-test passes and verify zero score variance.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.benchmark_harness",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.feed_source": CategorySpec(
        category_id="noun.feed_source",
        kind="noun",
        title="Live External Feed Source",
        summary="Volatile advisory stream, public data endpoint, or live pricing feed tracked via ETag/Digest.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.14,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.12,
                "safety_compliance": 0.14,
                "reusability": 0.10,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.04,
                "fragility_score": 0.10,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load previous HTTP ETag, Last-Modified header, and body digest.",
                "Verify robots/terms-of-service compliance for endpoint polling.",
            ),
            required_mcp_tools=("http.conditional_fetch", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Send conditional If-None-Match / HEAD request first; skip parsing on HTTP 304.",
            ),
            post_condition_receipt_rules=(
                "Record HTTP status, response Digest, ETag, and UTC observation timestamp.",
            ),
        ),
        canonical_tags=("live-feed", "volatile", "etag-tracked"),
        domain_dimensions=(
            "etag_support",
            "update_cadence_hours",
            "parse_reliability",
            "uptime_ratio_7d",
        ),
        value_rubric={
            "high": "HTTPS feed supporting conditional ETag/304 with deterministic schema parsing and fresh Digest.",
            "medium": "Reachable HTTPS feed requiring full-body Digest diffing without ETag headers.",
            "low": "Degraded, rate-limited, or unparseable upstream endpoint.",
        },
        history_metrics=("version_count", "drift_rate_30d", "http_304_hit_ratio", "mean_probe_ms"),
        priority_formula="(1.0 - freshness_confidence) * (0.5 + 0.5 * evidence_grounding)",
        recovery_playbook=(
            "Back off polling cadence on HTTP 429/5xx and preserve prior verified snapshot.",
            "Run Task.probe_source with conditional headers before scheduling full re-ingest.",
        ),
        suggested_verbs=(
            "verb.probe_source",
            "verb.fetch_feed",
            "verb.verify_gate",
            "verb.recheck_fragility",
        ),
    ),
    "noun.seed_hypothesis": CategorySpec(
        category_id="noun.seed_hypothesis",
        kind="noun",
        title="Seed Problem Hypothesis",
        summary="Falsifiable candidate framing, predicted failure mode, and the checks to run.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.12,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.12,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.06,
                "fragility_score": 0.06,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Check negative tombstones to ensure this hypothesis has not already been falsified.",
                "Load linked observations and statutory or technical constraints.",
            ),
            required_mcp_tools=("graph.search_neighbors", "tombstone.lookup"),
            deterministic_checks_before_llm=(
                "Verify presence of explicit falsification criteria and measurable target metric.",
            ),
            post_condition_receipt_rules=(
                "Record falsification predicate, prior confidence, and required verification gate.",
            ),
        ),
        canonical_tags=("hypothesis", "seed", "falsifiable"),
        domain_dimensions=(
            "falsifiability_clarity",
            "novelty_vs_tombstones",
            "expected_information_gain",
            "experiment_cost_bound",
        ),
        value_rubric={
            "high": "Falsifiable hypothesis with measurable pass/fail predicate and zero prior tombstone collisions.",
            "medium": "Plausible hypothesis needing tighter experimental acceptance bounds.",
            "low": "Unfalsifiable claim or duplicate of an existing negative.knowledge tombstone.",
        },
        history_metrics=("version_count", "expansion_count", "promotion_rate", "mean_verify_ms"),
        priority_formula="eb_pass_rate + 0.35 * ucb_exploration_bonus",
        recovery_playbook=(
            "Cross-check negative.knowledge store for prior falsification records.",
            "Refine target metric into a deterministic assertion and decompose via Task.decompose_dag.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.expand_seed",
            "verb.solve_draft",
            "verb.verify_gate",
        ),
    ),
    "noun.reflex_procedure": CategorySpec(
        category_id="noun.reflex_procedure",
        kind="noun",
        title="Distilled Reflex Procedure",
        summary="Zero-call or ultra-low-token deterministic execution recipe distilled from verified trajectories.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.14,
                "token_efficiency": 0.22,
                "safety_compliance": 0.12,
                "reusability": 0.10,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            },
            max_latency_ms=1000.0,
            max_cost_usd=0.05,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Verify input precondition schema and confirm zero active tombstones against reflex ID.",
            ),
            required_mcp_tools=("reflex.match_precondition", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Execute pure 0-call reflex steps directly; invoke LLM only if precondition guard fails.",
            ),
            post_condition_receipt_rules=(
                "Record 0-call execution latency, output Digest, and postcondition pass status.",
            ),
        ),
        canonical_tags=("reflex", "0-call", "distilled"),
        domain_dimensions=(
            "zero_call_hit_rate",
            "precondition_specificity",
            "token_savings_ratio",
            "replay_determinism",
        ),
        value_rubric={
            "high": "100% replay-verified 0-call recipe with strict precondition guards and sub-second latency.",
            "medium": "Hybrid low-token procedure passing replay on primary cohorts.",
            "low": "Over-broad precondition guard or unverified replay steps.",
        },
        history_metrics=("version_count", "reflex_hit_count", "fallback_rate_30d", "p95_exec_ms"),
        priority_formula="0.40 * determinism + 0.35 * token_efficiency + 0.25 * wilson_lower_bound",
        recovery_playbook=(
            "Quarantine reflex tag on precondition mismatch and fall back to Task.solve_draft.",
            "Re-distill from updated verified traces via Task.distill_reflex.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.verify_gate",
            "verb.recheck_fragility",
            "verb.distill_reflex",
        ),
    ),
    "noun.negative_tombstone": CategorySpec(
        category_id="noun.negative_tombstone",
        kind="noun",
        title="Negative Knowledge Tombstone",
        summary="Verified record of a buggy, unsafe, repealed, or falsified candidate preventing repeat failures.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.28,
                "token_efficiency": 0.10,
                "safety_compliance": 0.16,
                "reusability": 0.08,
                "latency_p95_ms": 0.03,
                "cost_usd": 0.02,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            },
            max_latency_ms=2000.0,
            max_cost_usd=0.25,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "contradicts",
            "corroborates",
            "verifies",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load failing reproduction record, rejected candidate Digest, and superseding winner ID.",
            ),
            required_mcp_tools=("evidence.hash_manifest", "tombstone.index_signature"),
            deterministic_checks_before_llm=(
                "Verify reproduction failure trace or adjudication record hash is attached.",
            ),
            post_condition_receipt_rules=(
                "Tag with negative.knowledge and index rejected content digest for preflight blocking.",
            ),
        ),
        canonical_tags=("negative.knowledge", "tombstone", "do-not-repeat"),
        domain_dimensions=(
            "reproduction_determinism",
            "failure_signature_specificity",
            "blast_radius_severity",
            "prevented_retry_count",
        ),
        value_rubric={
            "high": "Hash-indexed failure reproduction with exact error signature and superseding fix reference.",
            "medium": "Dead-letter tombstone with classified failure_class and queue context.",
            "low": "Vague error note without reproduction inputs or candidate digest.",
        },
        history_metrics=("version_count", "blocked_repeats", "linked_recoveries", "mean_verify_ms"),
        priority_formula="0.45 * evidence_grounding + 0.35 * determinism + 0.20 * safety_compliance",
        recovery_playbook=(
            "Attach missing reproduction trace or rejected candidate Digest.",
            "Link tombstone to a verified recovery_playbook Task Object for automated containment.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.recover_fault",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.skill_pack": CategorySpec(
        category_id="noun.skill_pack",
        kind="noun",
        title="Verified Agent Skill Pack",
        summary="Reusable multi-step agent skill bundle with verified tool bindings, guardrails, and records.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.22,
                "evidence_grounding": 0.16,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.16,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.03,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            },
            max_latency_ms=8000.0,
            max_cost_usd=0.5,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "produces_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Inspect required MCP tool names, scope grants, and compatible CallerProfile list.",
                "Check negative.knowledge tombstones for deprecated tool bindings.",
            ),
            required_mcp_tools=("mcp.introspect_tools", "schema.validate_json", "test.run_pytest"),
            deterministic_checks_before_llm=(
                "Validate every referenced MCP tool name and parameter schema before skill execution.",
            ),
            post_condition_receipt_rules=(
                "Emit skill manifest Digest, verified tool binding list, and smoke-test record.",
            ),
        ),
        canonical_tags=("skill-pack", "tool-bindings", "reusable"),
        domain_dimensions=(
            "tool_binding_coverage",
            "step_idempotency_ratio",
            "context_footprint_tokens",
            "cross_profile_portability",
        ),
        value_rubric={
            "high": "Self-contained skill bundle with 100% verified MCP tool schemas and deterministic smoke records.",
            "medium": "Reusable skill workflow with valid tool bindings awaiting cross-profile parity audit.",
            "low": "Unverified prompt instructions referencing missing or drifted tool signatures.",
        },
        history_metrics=("version_count", "invocation_hits", "drift_rate_30d", "mean_verify_ms"),
        priority_formula="0.35 * reusability + 0.35 * wilson_lower_bound + 0.30 * token_efficiency",
        recovery_playbook=(
            "Introspect live MCP tool schemas and bind shim_provision Task Object for renamed arguments.",
            "Re-run skill smoke test via Task.verify_gate before republishing.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.audit_profile_parity",
            "verb.verify_gate",
        ),
    ),
    "noun.prompt_shim": CategorySpec(
        category_id="noun.prompt_shim",
        kind="noun",
        title="Governed Prompt Shim",
        summary="Governed context-injection rule with explicit trigger conditions, safety bounds, and token budget.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.22,
                "evidence_grounding": 0.14,
                "token_efficiency": 0.22,
                "safety_compliance": 0.16,
                "reusability": 0.12,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.04,
                "verification_tier": 0.03,
            },
            max_latency_ms=1500.0,
            max_cost_usd=0.1,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load trigger predicate, target CallerProfile max_chars clamp, and safety boundary rules.",
            ),
            required_mcp_tools=("schema.validate_json", "safety.pii_redactor"),
            deterministic_checks_before_llm=(
                "Enforce hard character/token ceiling and deterministic trigger regex/tag match.",
            ),
            post_condition_receipt_rules=(
                "Record exact token count, trigger predicate hash, and profile parity check.",
            ),
        ),
        canonical_tags=("prompt-shim", "context-rule", "token-bounded"),
        domain_dimensions=(
            "trigger_precision",
            "token_budget_utilization",
            "instruction_conflict_risk",
            "ablation_lift_score",
        ),
        value_rubric={
            "high": "Compact, trigger-scoped shim demonstrating positive ablation lift within strict token budget.",
            "medium": "Bounded context rule passing safety checks without multi-arm ablation data.",
            "low": "Unbounded or always-on prompt fragment causing context bloat or instruction conflicts.",
        },
        history_metrics=("version_count", "injection_hits", "ablation_win_rate", "mean_tokens"),
        priority_formula="0.40 * token_efficiency + 0.35 * safety_compliance + 0.25 * wilson_lower_bound",
        recovery_playbook=(
            "Trim injected text to CallerProfile clamped_max_chars budget and tighten trigger tags.",
            "Validate positive lift via Task.benchmark_harness before re-enabling.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.audit_profile_parity",
            "verb.benchmark_harness",
            "verb.verify_gate",
        ),
    ),
    "noun.eval_run": CategorySpec(
        category_id="noun.eval_run",
        kind="noun",
        title="Fingerprinted Evaluation Run",
        summary="Fingerprinted benchmark or ablation run scorecard with null-honest metrics and digest records.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.10,
                "safety_compliance": 0.12,
                "reusability": 0.10,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.05,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "corroborates",
            "contradicts",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load linked benchmark_suite Task Object manifest hash, arm configurations, and git/environment fingerprint.",
            ),
            required_mcp_tools=("benchmark.read_summary", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Verify skipped arms report null metrics rather than fabricated zeros or estimates.",
            ),
            post_condition_receipt_rules=(
                "Attach per-arm sample counts, Wilson intervals, cost_usd totals, and run record Digest.",
            ),
        ),
        canonical_tags=("eval-run", "scorecard", "null-honest"),
        domain_dimensions=(
            "null_honesty_compliance",
            "sample_size_adequacy",
            "confidence_interval_tightness",
            "environment_reproducibility",
        ),
        value_rubric={
            "high": "Multi-arm scorecard pinned to benchmark Digest with Wilson intervals and strict null-honest unrun cells.",
            "medium": "Single-arm evaluation scorecard with complete artifact hashes and trial counts.",
            "low": "Unfingerprinted run summary or imputed metrics lacking raw trial records.",
        },
        history_metrics=("version_count", "arms_compared", "reuse_hits", "total_eval_cost_usd"),
        priority_formula="0.40 * evidence_grounding + 0.35 * determinism + 0.25 * wilson_lower_bound",
        recovery_playbook=(
            "Replace any unexecuted metric cells with explicit JSON null and attach raw trial logs.",
            "Re-run failed evaluation arm under Task.benchmark_harness with locked seed.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.benchmark_harness",
            "verb.verify_gate",
            "verb.reconcile_conflict",
        ),
    ),
    "noun.recovery_playbook": CategorySpec(
        category_id="noun.recovery_playbook",
        kind="noun",
        title="Deterministic Fault Recovery Playbook",
        summary="Deterministic fault-recovery procedure mapped to failure classes, queue controls, and replay gates.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.12,
                "safety_compliance": 0.18,
                "reusability": 0.10,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.02,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            },
            max_latency_ms=3000.0,
            max_cost_usd=0.25,
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "verifies",
            "requires_noun",
            "produces_noun",
            "rebutted_by_tombstone",
            "compiled_into_dag",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load dead-letter failure_class, queue state, and linked negative_tombstone Task Object records.",
            ),
            required_mcp_tools=("tombstone.lookup", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Match failure_class deterministically before mutating queue pause state or replaying tasks.",
            ),
            post_condition_receipt_rules=(
                "Emit recovery_receipt_sha256, patched brief diff, and post-replay verification gate status.",
            ),
        ),
        canonical_tags=("recovery", "playbook", "fault-containment"),
        domain_dimensions=(
            "failure_class_coverage",
            "containment_latency_ms",
            "idempotent_replay_safety",
            "recovery_success_rate",
        ),
        value_rubric={
            "high": "Idempotent recovery procedure mapped to exact failure_class with verified dead-letter replay records.",
            "medium": "Structured containment checklist covering transient and rate-limit faults.",
            "low": "Unbounded blind retry rule lacking backoff or failure classification.",
        },
        history_metrics=(
            "version_count",
            "recoveries_succeeded",
            "false_replay_rate",
            "mean_containment_ms",
        ),
        priority_formula="0.40 * determinism + 0.35 * safety_compliance + 0.25 * wilson_lower_bound",
        recovery_playbook=(
            "Pause affected queue immediately if cascading upstream_unavailable or rate_limited faults persist.",
            "Patch task brief deterministically and replay single canary task before unpausing queue.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.recover_fault",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "noun.public_source": CategorySpec(
        category_id="noun.public_source",
        kind="noun",
        title="Curated Public Source Index",
        summary="Curated documentation portal, technical specification, open standard, or public dataset index.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.16,
                "evidence_grounding": 0.28,
                "token_efficiency": 0.12,
                "safety_compliance": 0.14,
                "reusability": 0.12,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.03,
                "fragility_score": 0.06,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "corroborates",
            "contradicts",
            "verifies",
            "rebutted_by_tombstone",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load canonical HTTPS URI, license metadata, content ETag, and section anchor map.",
            ),
            required_mcp_tools=("http.conditional_fetch", "citation.verify_hash"),
            deterministic_checks_before_llm=(
                "Verify HTTPS reachability, open license tag, and content Digest before indexing.",
            ),
            post_condition_receipt_rules=(
                "Record canonical URL, license identifier, snapshot Digest, and retrieval timestamp.",
            ),
        ),
        canonical_tags=("public-source", "documentation", "provenance"),
        domain_dimensions=(
            "authority_tier",
            "license_clarity",
            "anchor_link_stability",
            "snapshot_hash_freshness",
        ),
        value_rubric={
            "high": "Primary specification or open dataset index with verified license, stable anchors, and snapshot Digest.",
            "medium": "Secondary documentation portal with reachable HTTPS URLs and dated citations.",
            "low": "Unlicensed, paywalled, or link-rotted external URL.",
        },
        history_metrics=("version_count", "citation_hits", "link_rot_rate_30d", "mean_probe_ms"),
        priority_formula="0.40 * evidence_grounding + 0.30 * freshness_confidence + 0.30 * reusability",
        recovery_playbook=(
            "Probe canonical HTTPS endpoint with conditional headers and resolve permanent redirects.",
            "Fall back to content-addressed snapshot Digest when upstream host is degraded.",
        ),
        suggested_verbs=(
            "verb.probe_source",
            "verb.ingest",
            "verb.situate",
            "verb.recheck_fragility",
        ),
    ),
    "noun.consensus_ballot": CategorySpec(
        category_id="noun.consensus_ballot",
        kind="noun",
        title="Calibrated Consensus Ballot",
        summary="Attested multi-verifier adjudication record with Sybil fencing, calibration weights, and record hash.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.26,
                "token_efficiency": 0.08,
                "safety_compliance": 0.20,
                "reusability": 0.06,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "derives_from",
            "supersedes",
            "corroborates",
            "contradicts",
            "verifies",
            "requires_noun",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load ConflictRecord candidate digests, proposer key IDs, and historical verifier calibration scores.",
            ),
            required_mcp_tools=("reconcile.adjudicate_ballots", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Reject proposer self-votes and duplicate key prefixes deterministically before tallying.",
            ),
            post_condition_receipt_rules=(
                "Attach ReconciliationReceipt Digest, voter calibration weights, and loser tombstone link.",
            ),
        ),
        canonical_tags=("consensus", "ballot", "adjudication"),
        domain_dimensions=(
            "quorum_margin_ratio",
            "voter_calibration_mean",
            "sybil_fence_integrity",
            "citation_completeness",
        ),
        value_rubric={
            "high": "Quorum-backed, Sybil-fenced adjudication record with calibrated verifiers and signed record Digest.",
            "medium": "Sub-quorum ballot tally cleanly parked for human_steward resolution.",
            "low": "Uncalibrated or self-voted ballot lacking verification citations.",
        },
        history_metrics=(
            "version_count",
            "quorum_pass_rate",
            "overturn_rate_90d",
            "steward_escalations",
        ),
        priority_formula="0.40 * evidence_grounding + 0.35 * determinism + 0.25 * calibration_weight",
        recovery_playbook=(
            "Strip uncalibrated or duplicate-prefix ballots and recompute weighted margin.",
            "Park conflict for human_steward when decisive margin falls below threshold.",
        ),
        suggested_verbs=(
            "verb.reconcile_conflict",
            "verb.vote_judge",
            "verb.verify_gate",
            "verb.audit_profile_parity",
        ),
    ),
}


# ---------------------------------------------------------------------------
# 18 Canonical Verb Categories
# ---------------------------------------------------------------------------

VERB_CATEGORIES: dict[str, CategorySpec] = {
    "verb.ingest": CategorySpec(
        category_id="verb.ingest",
        kind="verb",
        title="Normalize and Ingest Artifact",
        summary="Parse raw input bytes, strip unsafe tokens, compute CAS Digest, and emit a typed Task Object.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.14,
                "safety_compliance": 0.16,
                "reusability": 0.08,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.04,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "verifies",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Check CAS store by raw Digest to deduplicate identical submissions.",
            ),
            required_mcp_tools=(
                "cas.lookup_sha256",
                "safety.pii_redactor",
                "schema.validate_json",
            ),
            deterministic_checks_before_llm=(
                "Compute Digest, MIME/schema parse, and PII scan deterministically before any LLM extraction.",
            ),
            post_condition_receipt_rules=(
                "Emit ingested Task Object ID, canonical Digest, and category conformance score.",
            ),
        ),
        canonical_tags=("ingest", "cas", "normalize"),
        domain_dimensions=(
            "dedup_hit_ratio",
            "schema_parse_fidelity",
            "redaction_pass_rate",
            "throughput_bytes_per_sec",
        ),
        value_rubric={
            "high": "Idempotent CAS ingestion with 0-call deduplication, clean PII scan, and validated schema.",
            "medium": "Normalized record stored with minor optional dimension warnings.",
            "low": "Unvalidated raw payload or missing content Digest.",
        },
        history_metrics=("version_count", "dedup_hits", "parse_error_rate", "mean_verify_ms"),
        priority_formula="0.40 * determinism + 0.35 * safety_compliance + 0.25 * token_efficiency",
        recovery_playbook=(
            "Quarantine malformed input bytes in blob store and log schema validation errors.",
            "Apply deterministic encoding/MIME normalization before retrying ingest.",
        ),
        suggested_verbs=("verb.situate", "verb.verify_gate", "verb.distill_reflex"),
    ),
    "verb.fetch_feed": CategorySpec(
        category_id="verb.fetch_feed",
        kind="verb",
        title="Fetch External Feed Conditionally",
        summary="Poll an external feed source using ETag/If-None-Match headers and record provenance.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.22,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.12,
                "reusability": 0.06,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.04,
                "fragility_score": 0.05,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "corroborates",
            "supersedes",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load target feed_source Task Object URI, rate-limit window, and prior ETag/Digest.",
            ),
            required_mcp_tools=("http.conditional_fetch", "cas.lookup_sha256"),
            deterministic_checks_before_llm=(
                "Execute pure HTTP conditional GET; short-circuit with 0 LLM tokens on 304 or unchanged Digest.",
            ),
            post_condition_receipt_rules=(
                "Record HTTP status, ETag, body Digest, and drift classification.",
            ),
        ),
        canonical_tags=("fetch", "feed", "conditional-http"),
        domain_dimensions=(
            "conditional_304_efficiency",
            "header_provenance_completeness",
            "timeout_resilience",
            "drift_detection_accuracy",
        ),
        value_rubric={
            "high": "Bounded-timeout conditional fetch short-circuiting on 304 and logging exact Digest drift.",
            "medium": "Full-body HTTPS fetch with recorded Digest and status.",
            "low": "Unbounded fetch ignoring rate-limit headers or dropping provenance.",
        },
        history_metrics=("version_count", "http_304_rate", "timeout_rate_7d", "mean_probe_ms"),
        priority_formula="(1.0 - freshness_confidence) * (0.5 + 0.5 * composite_score)",
        recovery_playbook=(
            "Honor Retry-After header and classify 429/503 via failure_class.",
            "Pause feed queue if upstream host fails consecutive conditional probes.",
        ),
        suggested_verbs=("verb.ingest", "verb.situate", "verb.verify_gate"),
    ),
    "verb.situate": CategorySpec(
        category_id="verb.situate",
        kind="verb",
        title="Situate in Graph and Jurisdiction",
        summary="Anchor an artifact or signal inside its graph neighborhood, statutory scope, and prior history.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.18,
                "evidence_grounding": 0.26,
                "token_efficiency": 0.14,
                "safety_compliance": 0.16,
                "reusability": 0.10,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.04,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "corroborates",
            "contradicts",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Query 2-hop graph neighborhood, active negative tombstones, and jurisdiction filters.",
            ),
            required_mcp_tools=("graph.search_neighbors", "tombstone.lookup"),
            deterministic_checks_before_llm=(
                "Run exact citation/entity graph traversal before invoking semantic synthesis.",
            ),
            post_condition_receipt_rules=(
                "Attach linked neighbor IDs, tombstones consulted, and cohort/jurisdiction scope.",
            ),
        ),
        canonical_tags=("situate", "context-graph", "jurisdiction"),
        domain_dimensions=(
            "neighborhood_recall",
            "tombstone_coverage",
            "jurisdiction_match_accuracy",
            "hop_budget_efficiency",
        ),
        value_rubric={
            "high": "Complete 2-hop neighborhood anchor surfacing all active tombstones and reusable Task Objects.",
            "medium": "1-hop lexical/tag anchor with basic cohort scoping.",
            "low": "Isolated task execution ignoring existing graph relations and tombstones.",
        },
        history_metrics=("version_count", "tombstones_caught", "reuse_hits", "mean_traverse_ms"),
        priority_formula="0.45 * evidence_grounding + 0.30 * determinism + 0.25 * token_efficiency",
        recovery_playbook=(
            "Widen relation traversal to depth=2 and include L0 scent search fallback.",
            "Attach explicit empty-neighborhood record when seed is genuinely novel.",
        ),
        suggested_verbs=("verb.problemize", "verb.solve_draft", "verb.verify_gate"),
    ),
    "verb.problemize": CategorySpec(
        category_id="verb.problemize",
        kind="verb",
        title="Frame Bounded Problem Statement",
        summary="Convert noisy observations or user requests into testable constraints, acceptance gates, and seeds.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.16,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.18,
                "reusability": 0.10,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.05,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load situated neighborhood, caller budget/profile limits, and historical tombstones.",
            ),
            required_mcp_tools=("graph.search_neighbors", "tombstone.lookup"),
            deterministic_checks_before_llm=(
                "Check if an existing verified reflex procedure already solves the exact problem signature.",
            ),
            post_condition_receipt_rules=(
                "Produce explicit success predicate, hard safety constraints, and candidate seed IDs.",
            ),
        ),
        canonical_tags=("problemize", "framing", "constraints"),
        domain_dimensions=(
            "acceptance_gate_testability",
            "constraint_completeness",
            "reflex_shortcircuit_rate",
            "scope_tightness",
        ),
        value_rubric={
            "high": "Bounded problem spec with machine-checkable acceptance gates and cost ceiling.",
            "medium": "Structured problem framing with qualitative acceptance notes.",
            "low": "Ambiguous or unbounded objective lacking verification criteria.",
        },
        history_metrics=(
            "version_count",
            "downstream_solve_rate",
            "reflex_matches",
            "mean_verify_ms",
        ),
        priority_formula="0.35 * evidence_grounding + 0.35 * safety_compliance + 0.30 * token_efficiency",
        recovery_playbook=(
            "Split over-broad objective into independent sub-problems via Task.decompose_dag.",
            "Add explicit deterministic acceptance check before dispatching to solve queue.",
        ),
        suggested_verbs=("verb.generate_seed", "verb.expand_seed", "verb.solve_draft"),
    ),
    "verb.expand_seed": CategorySpec(
        category_id="verb.expand_seed",
        kind="verb",
        title="Expand Seed Hypothesis",
        summary="Elaborate a seed hypothesis into concrete sub-tasks, required Task Objects, and falsification checks.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.16,
                "evidence_grounding": 0.22,
                "token_efficiency": 0.18,
                "safety_compliance": 0.14,
                "reusability": 0.12,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.05,
                "fragility_score": 0.04,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "compiled_into_dag",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load target seed_hypothesis Task Object, budget ceiling, and available reusable Task Objects.",
            ),
            required_mcp_tools=("graph.search_neighbors", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Prune branches matching known negative.knowledge tombstones before expansion.",
            ),
            post_condition_receipt_rules=(
                "Emit acyclic dependency list, estimated token/USD budget, and verification gate bindings.",
            ),
        ),
        canonical_tags=("expand", "planning", "hypothesis"),
        domain_dimensions=(
            "branch_prune_efficiency",
            "dag_acyclicity",
            "noun_reuse_ratio",
            "budget_adherence",
        ),
        value_rubric={
            "high": "Tombstone-pruned expansion reusing existing verified Task Objects within caller cost cap.",
            "medium": "Acyclic sub-task plan with valid category bindings.",
            "low": "Redundant expansion re-exploring falsified branches or exceeding depth cap.",
        },
        history_metrics=("version_count", "pruned_branches", "promotion_yield", "mean_expand_ms"),
        priority_formula="0.35 * evidence_grounding + 0.35 * token_efficiency + 0.30 * reusability",
        recovery_playbook=(
            "Prune cyclic or depth-exceeding child specs and bind existing CAS Task Objects.",
            "Fallback to 4-step deterministic wave via Task.decompose_dag.",
        ),
        suggested_verbs=("verb.decompose_dag", "verb.solve_draft", "verb.verify_gate"),
    ),
    "verb.solve_draft": CategorySpec(
        category_id="verb.solve_draft",
        kind="verb",
        title="Synthesize Candidate Solution Draft",
        summary="Construct a candidate code node, shim, scene recipe, or brief satisfying problem constraints.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.18,
                "evidence_grounding": 0.22,
                "token_efficiency": 0.16,
                "safety_compliance": 0.18,
                "reusability": 0.10,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.05,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "supersedes",
            "adapted_for_cohort",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load problem spec, target CategorySpec dimensions, and all matching tombstones.",
            ),
            required_mcp_tools=(
                "repo.read_symbol",
                "schema.validate_json",
                "safety.pii_redactor",
            ),
            deterministic_checks_before_llm=(
                "Assemble reusable verified Task Objects and auto-shims first to minimize fresh generation.",
            ),
            post_condition_receipt_rules=(
                "Emit candidate artifact Digest, cited input Task Objects, and draft status pending verify_gate.",
            ),
        ),
        canonical_tags=("solve", "draft", "synthesis"),
        domain_dimensions=(
            "first_pass_gate_rate",
            "constraint_satisfaction",
            "shim_reuse_ratio",
            "synthesis_token_efficiency",
        ),
        value_rubric={
            "high": "Schema-valid candidate draft citing input Task Objects and passing preflight static checks on attempt 1.",
            "medium": "Candidate draft requiring minor deterministic normalization before verify_gate.",
            "low": "Draft violating target CategorySpec bounds or repeating a tombstoned pattern.",
        },
        history_metrics=("version_count", "first_pass_accept_rate", "mean_tokens", "mean_solve_ms"),
        priority_formula="0.35 * wilson_lower_bound + 0.35 * evidence_grounding + 0.30 * safety_compliance",
        recovery_playbook=(
            "Inspect verify_gate failure trace and load matching recovery_playbook Task Object.",
            "Patch violating fields deterministically and re-submit to Task.verify_gate.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.audit_profile_parity", "verb.distill_reflex"),
    ),
    "verb.compile_dag": CategorySpec(
        category_id="verb.compile_dag",
        kind="verb",
        title="Compile Deterministic Execution DAG",
        summary="Lower expanded plans and candidate steps into a topologically sorted, typed execution graph.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.16,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.10,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "compiled_into_dag",
            "verifies",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load expanded step specs, input/output Task Object categories, and cost caps.",
            ),
            required_mcp_tools=("dag.toposort_validate", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Run cycle detection, type compatibility check, and budget sum strictly in 0-call code.",
            ),
            post_condition_receipt_rules=(
                "Emit topological node order, critical-path latency bound, and DAG manifest Digest.",
            ),
        ),
        canonical_tags=("dag", "compiler", "deterministic"),
        domain_dimensions=(
            "topological_validity",
            "critical_path_compression",
            "type_edge_soundness",
            "idempotency_key_coverage",
        ),
        value_rubric={
            "high": "0-call cycle-free DAG with 100% typed edges, idempotency keys, and bounded critical path.",
            "medium": "Valid linear task chain within depth and dependency limits.",
            "low": "Cyclic dependencies, broader child effect classes, or missing idempotency keys.",
        },
        history_metrics=("version_count", "dags_compiled", "cycle_rejections", "mean_compile_ms"),
        priority_formula="0.45 * determinism + 0.30 * token_efficiency + 0.25 * safety_compliance",
        recovery_playbook=(
            "Break dependency cycles and clamp child effect_class to parent effect_class.",
            "Enforce MAX_DEPTH and MAX_CHILDREN bounds before splitting.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.distill_reflex"),
    ),
    "verb.verify_gate": CategorySpec(
        category_id="verb.verify_gate",
        kind="verb",
        title="Execute Deterministic Verification Gate",
        summary="Run unit tests, benchmark assertions, schema validators, and safety checks on a candidate.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.30,
                "evidence_grounding": 0.22,
                "token_efficiency": 0.12,
                "safety_compliance": 0.16,
                "reusability": 0.06,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.03,
                "fragility_score": 0.02,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "corroborates",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load candidate artifact bytes, target CategorySpec, and benchmark suite.",
            ),
            required_mcp_tools=(
                "test.run_pytest",
                "schema.validate_json",
                "safety.pii_redactor",
            ),
            deterministic_checks_before_llm=(
                "Execute 100% of test, schema, and leak checks deterministically without LLM calls.",
            ),
            post_condition_receipt_rules=(
                "Emit pass/fail counts, stdout/stderr digest, and negative_tombstone Task Object on deterministic failure.",
            ),
        ),
        canonical_tags=("verify", "gate", "0-call"),
        domain_dimensions=(
            "zero_flake_determinism",
            "assertion_density",
            "leak_scan_coverage",
            "gate_latency_ms",
        ),
        value_rubric={
            "high": "Zero-call sandboxed test + schema + leak gate emitting cryptographic record Digest.",
            "medium": "Deterministic schema validation pass awaiting full integration suite.",
            "low": "Skipped assertions or unverified self-reported success.",
        },
        history_metrics=("version_count", "gates_executed", "defect_catch_rate", "mean_verify_ms"),
        priority_formula="0.50 * determinism + 0.30 * evidence_grounding + 0.20 * safety_compliance",
        recovery_playbook=(
            "Record deterministic failure signature as negative_tombstone Task Object.",
            "Route failing candidate to Task.recover_fault with exact assertion diff.",
        ),
        suggested_verbs=("verb.distill_reflex", "verb.recover_fault", "verb.reconcile_conflict"),
    ),
    "verb.recheck_fragility": CategorySpec(
        category_id="verb.recheck_fragility",
        kind="verb",
        title="Recheck Stale or Fragile Task Object",
        summary="Re-probe decayed artifacts against live endpoints, statutes, or schemas to refresh confidence.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.24,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.12,
                "reusability": 0.06,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.04,
                "fragility_score": 0.04,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "supersedes",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load artifact prev_sha256, prev_etag, half_life_hours, and dependent graph edges.",
            ),
            required_mcp_tools=("http.conditional_fetch", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Compare HTTP status, ETag, and Digest first; invoke re-ingestion only when status == drifted.",
            ),
            post_condition_receipt_rules=(
                "Record unchanged | drifted | unreachable_degraded status and updated last_verified_at.",
            ),
        ),
        canonical_tags=("fragility", "recheck", "freshness"),
        domain_dimensions=(
            "staleness_reduction",
            "drift_precision",
            "dependent_invalidation_speed",
            "probe_budget_efficiency",
        ),
        value_rubric={
            "high": "Conditional recheck refreshing last_verified_at on unchanged Digest or flagging exact drift.",
            "medium": "Reachability probe confirming HTTP 200 status without ETag header.",
            "low": "Unbounded recheck overwriting valid snapshots with 5xx error pages.",
        },
        history_metrics=("version_count", "rechecks_run", "drifts_caught", "mean_probe_ms"),
        priority_formula="importance * (1.0 - freshness_confidence)",
        recovery_playbook=(
            "Mark observation unreachable_degraded on 4xx/5xx/timeout without clobbering last good version.",
            "Publish delta alert to fragility.alerts feed when drift_detected is true.",
        ),
        suggested_verbs=("verb.probe_source", "verb.ingest", "verb.verify_gate"),
    ),
    "verb.reconcile_conflict": CategorySpec(
        category_id="verb.reconcile_conflict",
        kind="verb",
        title="Reconcile 3-Way Context/CAS Conflict",
        summary="Merge disjoint edits, monotonic tag sets, and commutative counters or isolate collisions.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.30,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.06,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "supersedes",
            "contradicts",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load common ancestor base snapshot plus concurrent ours and theirs revisions.",
            ),
            required_mcp_tools=("cas.lookup_sha256", "reconcile.three_way"),
            deterministic_checks_before_llm=(
                "Execute 0-call structural 3-way merge; never invoke LLM when status == auto_merged.",
            ),
            post_condition_receipt_rules=(
                "Emit merged CAS digest or structured ConflictRecord listing exact conflicting_paths.",
            ),
        ),
        canonical_tags=("reconcile", "3-way-merge", "cas"),
        domain_dimensions=(
            "auto_merge_ratio",
            "crdt_commutativity",
            "conflict_path_minimality",
            "ancestor_fidelity",
        ),
        value_rubric={
            "high": "0-call structural 3-way merge preserving monotonic sets and counters or isolating minimal conflict paths.",
            "medium": "Conflict isolation packet ready for calibrated ballot adjudication.",
            "low": "Last-writer-wins overwrite discarding concurrent verified edits.",
        },
        history_metrics=("version_count", "auto_merges", "escalated_conflicts", "mean_merge_ms"),
        priority_formula="0.45 * determinism + 0.35 * evidence_grounding + 0.20 * safety_compliance",
        recovery_playbook=(
            "Reload head CAS version and common ancestor from record_history.",
            "Escalate isolated conflicting_paths to Task.vote_judge when auto-merge is not clean.",
        ),
        suggested_verbs=("verb.vote_judge", "verb.verify_gate"),
    ),
    "verb.vote_judge": CategorySpec(
        category_id="verb.vote_judge",
        kind="verb",
        title="Adjudicate Conflict via Calibrated Ballots",
        summary="Tally Sybil-fenced, calibration-weighted verifier votes and route winner or park for human.",
        fragility_domain="regulatory_statute",
        default_half_life_hours=2160.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.26,
                "token_efficiency": 0.10,
                "safety_compliance": 0.20,
                "reusability": 0.04,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "supersedes",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load ConflictRecord, proposer_key_ids, voter calibration history, and human_only flag.",
            ),
            required_mcp_tools=(
                "reconcile.adjudicate_ballots",
                "tombstone.index_signature",
            ),
            deterministic_checks_before_llm=(
                "Enforce self-vote block, duplicate-key block, and quorum/margin math strictly in deterministic code.",
            ),
            post_condition_receipt_rules=(
                "Emit Digest ReconciliationReceipt and negative_tombstone Task Object for rejected buggy loser.",
            ),
        ),
        canonical_tags=("voting", "judging", "sybil-fenced"),
        domain_dimensions=(
            "sybil_rejection_accuracy",
            "calibration_weighting_fidelity",
            "quorum_attainment",
            "tombstone_emission_rate",
        ),
        value_rubric={
            "high": "Sybil-fenced calibrated tally promoting verified winner and recording loser tombstone.",
            "medium": "Deadlocked or human_only conflict safely parked for human_steward.",
            "low": "Unfenced majority tally permitting proposer self-votes.",
        },
        history_metrics=(
            "version_count",
            "adjudications_total",
            "parked_for_human_rate",
            "mean_tally_ms",
        ),
        priority_formula="0.40 * determinism + 0.35 * safety_compliance + 0.25 * evidence_grounding",
        recovery_playbook=(
            "Park task immediately for human_steward when quorum_met is false or human_only is true.",
            "Request independent calibrated_verifier ballots from distinct key prefixes.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.distill_reflex"),
    ),
    "verb.distill_reflex": CategorySpec(
        category_id="verb.distill_reflex",
        kind="verb",
        title="Distill Verified Trace into Reflex",
        summary="Compress repeated high-confidence execution traces into a 0-call reflex_procedure Task Object.",
        fragility_domain="mcp_api_schema",
        default_half_life_hours=168.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.20,
                "safety_compliance": 0.12,
                "reusability": 0.10,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.02,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "verifies",
            "compiled_into_dag",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load >= 3 corroborated execution traces and check absence of contradictory tombstones.",
            ),
            required_mcp_tools=("trace.extract_invariants", "test.run_pytest"),
            deterministic_checks_before_llm=(
                "Replay candidate reflex procedure deterministically across historical trace inputs.",
            ),
            post_condition_receipt_rules=(
                "Emit reflex_procedure Task Object digest, replay pass rate = 1.0, and token savings ratio.",
            ),
        ),
        canonical_tags=("distill", "reflex", "token-saving"),
        domain_dimensions=(
            "trace_corroboration_count",
            "replay_pass_ratio",
            "compression_ratio",
            "precondition_soundness",
        ),
        value_rubric={
            "high": "0-call reflex distilled from >= 3 traces with 100% replay pass rate and >= 80% token savings.",
            "medium": "Parameterized template reducing model calls on recurring inputs.",
            "low": "Premature distillation from a single uncorroborated trace.",
        },
        history_metrics=("version_count", "reflexes_minted", "token_saved_total", "mean_replay_ms"),
        priority_formula="0.40 * token_efficiency + 0.35 * determinism + 0.25 * reusability",
        recovery_playbook=(
            "Require additional corroborated traces before minting reflex when replay < 1.0.",
            "Narrow precondition signature to exclude edge-case failures.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.audit_profile_parity"),
    ),
    "verb.generate_seed": CategorySpec(
        category_id="verb.generate_seed",
        kind="verb",
        title="Synthesize Structured Seed Task Objects",
        summary="Synthesize structured, falsifiable seed Task Objects from raw domain observations and delta feeds.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.18,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.10,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.05,
                "fragility_score": 0.04,
                "verification_tier": 0.03,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "corroborates",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load raw feed delta entries, L0 Task Object scents, and negative.knowledge tombstones.",
            ),
            required_mcp_tools=("search_nouns", "schema.validate_json", "tombstone.lookup"),
            deterministic_checks_before_llm=(
                "Deduplicate against existing seed hashes and filter tombstoned signatures in 0-call code.",
            ),
            post_condition_receipt_rules=(
                "Emit validated seed_hypothesis Task Object record key, provenance citations, and seed Digest.",
            ),
        ),
        canonical_tags=("seed", "synthesize", "discovery"),
        domain_dimensions=(
            "dedup_novelty_ratio",
            "provenance_link_density",
            "falsification_predicate_score",
            "seed_to_canonical_yield",
        ),
        value_rubric={
            "high": "Novel, deduplicated seed Task Object with grounded source citations and deterministic acceptance check.",
            "medium": "Structured seed candidate needing neighborhood linking.",
            "low": "Duplicate or ungrounded seed lacking falsification criteria.",
        },
        history_metrics=(
            "version_count",
            "seeds_emitted",
            "canonical_promotion_rate",
            "mean_verify_ms",
        ),
        priority_formula="eb_pass_rate + 0.35 * ucb_exploration_bonus",
        recovery_playbook=(
            "Run L0 scent search to merge near-duplicate seeds rather than minting redundant records.",
            "Attach primary observation URI and re-validate against seed_hypothesis Task Object schema.",
        ),
        suggested_verbs=("verb.situate", "verb.decompose_dag", "verb.expand_seed"),
    ),
    "verb.decompose_dag": CategorySpec(
        category_id="verb.decompose_dag",
        kind="verb",
        title="Decompose Goal into Dependency Wave",
        summary="Compile a high-level goal or seed category into a typed dependency wave of child Tasks.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.26,
                "evidence_grounding": 0.18,
                "token_efficiency": 0.16,
                "safety_compliance": 0.14,
                "reusability": 0.10,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "compiled_into_dag",
            "derives_from",
            "verifies",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load target CategorySpec suggested_verbs, CallerProfile budget ceiling, and queue limits.",
            ),
            required_mcp_tools=("generate_category_verbs", "dag.toposort_validate"),
            deterministic_checks_before_llm=(
                "Synthesize canonical situate -> solve -> verify -> distill wave in 0-call deterministic code first.",
            ),
            post_condition_receipt_rules=(
                "Emit wave_sha256, deterministic idempotency_keys, and topological depends_on chain.",
            ),
        ),
        canonical_tags=("decompose", "wave", "dag"),
        domain_dimensions=(
            "wave_determinism",
            "step_granularity_balance",
            "dependency_chain_soundness",
            "idempotent_enqueue_safety",
        ),
        value_rubric={
            "high": "Deterministic, idempotent situate-solve-verify-distill wave respecting depth and effect caps.",
            "medium": "Valid multi-step child task list with explicit dependencies.",
            "low": "Unbounded fan-out or missing verification step.",
        },
        history_metrics=(
            "version_count",
            "waves_generated",
            "wave_completion_rate",
            "mean_decompose_ms",
        ),
        priority_formula="0.45 * determinism + 0.30 * token_efficiency + 0.25 * reusability",
        recovery_playbook=(
            "Clamp max_verbs to [2, 8] and ensure every synthesis step is followed by Task.verify_gate.",
            "Use deterministic genverb idempotency keys to prevent duplicate task creation on replay.",
        ),
        suggested_verbs=(
            "verb.situate",
            "verb.solve_draft",
            "verb.verify_gate",
            "verb.distill_reflex",
        ),
    ),
    "verb.probe_source": CategorySpec(
        category_id="verb.probe_source",
        kind="verb",
        title="Probe Source Liveness and Drift",
        summary="Execute bounded conditional HTTP/ETag/Digest liveness, latency, and drift check on an endpoint.",
        fragility_domain="live_feed",
        default_half_life_hours=12.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.24,
                "evidence_grounding": 0.24,
                "token_efficiency": 0.16,
                "safety_compliance": 0.12,
                "reusability": 0.06,
                "latency_p95_ms": 0.08,
                "cost_usd": 0.03,
                "fragility_score": 0.04,
                "verification_tier": 0.03,
            },
            max_latency_ms=5000.0,
            max_cost_usd=0.05,
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "corroborates",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load HTTPS source_uri, expected_sha256, and conditional If-None-Match / If-Modified-Since headers.",
            ),
            required_mcp_tools=("http.conditional_fetch", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Enforce HTTPS-only scheme and hard 3500ms timeout; send HEAD then fallback GET with 0 LLM calls.",
            ),
            post_condition_receipt_rules=(
                "Record HTTP status, latency_ms, observed_sha256, drift_detected boolean, and sweep record.",
            ),
        ),
        canonical_tags=("probe", "liveness", "sha256-drift"),
        domain_dimensions=(
            "timeout_bound_adherence",
            "conditional_header_hit_rate",
            "sha256_drift_accuracy",
            "https_safety_enforcement",
        ),
        value_rubric={
            "high": "Sub-3.5s HTTPS conditional probe returning exact status, latency_ms, and Digest drift verdict.",
            "medium": "Bounded HTTP status check without prior ETag baseline.",
            "low": "Unbounded network call or non-HTTPS probe.",
        },
        history_metrics=("version_count", "probes_total", "drift_rate_30d", "p95_probe_ms"),
        priority_formula="importance * (1.0 - freshness_confidence)",
        recovery_playbook=(
            "Fallback from HEAD to bounded GET on HTTP 405/501 while keeping 3500ms timeout.",
            "Classify network errors as upstream_unavailable and preserve existing verified record.",
        ),
        suggested_verbs=("verb.recheck_fragility", "verb.ingest", "verb.recover_fault"),
    ),
    "verb.recover_fault": CategorySpec(
        category_id="verb.recover_fault",
        kind="verb",
        title="Diagnose and Replay Faulted Task",
        summary="Classify dead-letter or transient failure, apply deterministic recovery playbook, and replay safely.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.20,
                "token_efficiency": 0.12,
                "safety_compliance": 0.18,
                "reusability": 0.08,
                "latency_p95_ms": 0.05,
                "cost_usd": 0.03,
                "fragility_score": 0.03,
                "verification_tier": 0.03,
            },
            max_latency_ms=5000.0,
            max_cost_usd=0.25,
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "derives_from",
            "verifies",
            "supersedes",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load dead-letter task result history, negative.knowledge tombstone, and queue pause state.",
            ),
            required_mcp_tools=("tombstone.lookup", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Classify error into canonical failure_class and check if queue pause is warranted before replay.",
            ),
            post_condition_receipt_rules=(
                "Emit failure_class, recovery_receipt_sha256, and task.recovered audit event.",
            ),
        ),
        canonical_tags=("recover", "dead-letter", "replay"),
        domain_dimensions=(
            "failure_classification_accuracy",
            "replay_idempotency",
            "post_recovery_pass_rate",
            "containment_speed_ms",
        ),
        value_rubric={
            "high": "Deterministic fault classification, patched brief replay, and verified post-recovery completion.",
            "medium": "Contained fault with queue paused and tombstone indexed for operator review.",
            "low": "Unmodified blind replay of a deterministic schema_invalid or gate_rejected task.",
        },
        history_metrics=(
            "version_count",
            "tasks_recovered",
            "post_replay_done_rate",
            "mean_recover_ms",
        ),
        priority_formula="0.45 * safety_compliance + 0.35 * determinism + 0.20 * evidence_grounding",
        recovery_playbook=(
            "Do not blind-replay schema_invalid or gate_rejected tasks without a non-empty patch_brief.",
            "Pause queue on cascading rate_limited or upstream_unavailable errors before recovering dead tasks.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.distill_reflex"),
    ),
    "verb.audit_profile_parity": CategorySpec(
        category_id="verb.audit_profile_parity",
        kind="verb",
        title="Audit Caller Profile Safety and Budget Parity",
        summary="Verify output clamping, human-review gates, shims, and USD budgets across all CallerProfiles.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=True,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.18,
                "token_efficiency": 0.14,
                "safety_compliance": 0.22,
                "reusability": 0.06,
                "latency_p95_ms": 0.04,
                "cost_usd": 0.02,
                "fragility_score": 0.02,
                "verification_tier": 0.04,
            },
            max_latency_ms=2000.0,
            max_cost_usd=0.05,
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "adapted_for_cohort",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load candidate Task Object/Task record and all 8 canonical CallerProfile definitions.",
            ),
            required_mcp_tools=(
                "list_categories",
                "validate_category_payload",
                "safety.pii_redactor",
            ),
            deterministic_checks_before_llm=(
                "Render adapt_noun_view across all 8 profiles in 0-call code and assert clamped_max_chars and human_gate.",
            ),
            post_condition_receipt_rules=(
                "Record per-profile char length, requires_human_review flag, cost clamp status, and parity Digest.",
            ),
        ),
        canonical_tags=("profile-parity", "audit", "governance"),
        domain_dimensions=(
            "char_clamp_adherence",
            "human_gate_consistency",
            "budget_stepdown_accuracy",
            "caveat_preservation_ratio",
        ),
        value_rubric={
            "high": "Zero-call parity pass across all 8 CallerProfiles enforcing char caps, cost clamps, and human gates.",
            "medium": "Parity verified across primary coding and research profiles.",
            "low": "Profile projection exceeding clamped_max_chars or dropping human_sensitive caveats.",
        },
        history_metrics=(
            "version_count",
            "profiles_audited",
            "clamp_violations_caught",
            "mean_audit_ms",
        ),
        priority_formula="0.45 * safety_compliance + 0.35 * determinism + 0.20 * token_efficiency",
        recovery_playbook=(
            "Enforce _clamp_text / _clamp_json_receipt ceiling and restore missing human-oversight caveats.",
            "Mark human_sensitive categories into require_human_on_routes before release.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.vote_judge"),
    ),
    "verb.benchmark_harness": CategorySpec(
        category_id="verb.benchmark_harness",
        kind="verb",
        title="Execute Multi-Arm Benchmark Harness",
        summary="Execute reproducible multi-arm harness evaluation with locked seeds, Wilson bounds, and Digest record.",
        fragility_domain="static_code",
        default_half_life_hours=8760.0,
        human_sensitive=False,
        dimensions=_build_dimensions(
            {
                "determinism": 0.28,
                "evidence_grounding": 0.22,
                "token_efficiency": 0.10,
                "safety_compliance": 0.14,
                "reusability": 0.08,
                "latency_p95_ms": 0.06,
                "cost_usd": 0.05,
                "fragility_score": 0.03,
                "verification_tier": 0.04,
            }
        ),
        allowed_relations=(
            "requires_noun",
            "produces_noun",
            "verifies",
            "corroborates",
            "contradicts",
            "rebutted_by_tombstone",
        ),
        worker_guidance=WorkerGuidance(
            preflight_context=(
                "Load benchmark_suite Task Object golden fixtures, arm matrix, and cost budget.",
            ),
            required_mcp_tools=("test.run_pytest", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Verify fixture Digest lock and deterministic scoring harness before executing model arms.",
            ),
            post_condition_receipt_rules=(
                "Emit eval_run Task Object scorecard with null-honest unrun cells, Wilson bounds, and record Digest.",
            ),
        ),
        canonical_tags=("benchmark", "harness", "multi-arm"),
        domain_dimensions=(
            "arm_isolation_integrity",
            "seed_reproducibility",
            "statistical_power",
            "cost_accounting_precision",
        ),
        value_rubric={
            "high": "Seed-locked multi-arm evaluation emitting null-honest eval_run Task Object scorecard and Digest record.",
            "medium": "Reproducible baseline vs treatment run with complete token and latency telemetry.",
            "low": "Contaminated fixtures, missing control arm, or unrecorded model costs.",
        },
        history_metrics=("version_count", "harness_runs", "arms_evaluated", "mean_harness_ms"),
        priority_formula="0.40 * determinism + 0.35 * evidence_grounding + 0.25 * wilson_lower_bound",
        recovery_playbook=(
            "Isolate failing arm, record exact error without imputing scores, and verify fixture manifest hash.",
            "Replay failed arm with identical seed after resolving transient_timeout or rate_limited faults.",
        ),
        suggested_verbs=("verb.verify_gate", "verb.reconcile_conflict", "verb.distill_reflex"),
    ),
}

CATEGORY_REGISTRY: dict[str, CategorySpec] = {**NOUN_CATEGORIES, **VERB_CATEGORIES}


def normalize_tag(raw_tag: str) -> str:
    """Normalize a tag string to deterministic lowercase ASCII token form."""
    if category := category_from_tag(raw_tag):
        return f"category:{category}"
    cleaned = raw_tag.strip().lower().replace("_", "-").replace(" ", "-")
    cleaned = _TAG_CLEAN_RE.sub("", cleaned)
    cleaned = _MULTI_HYPHEN_RE.sub("-", cleaned).strip("-")
    return cleaned


def normalize_tags(tags: Sequence[str], include_canonical: Sequence[str] = ()) -> tuple[str, ...]:
    """Normalize, deduplicate, and deterministically sort tag strings."""
    merged: set[str] = set()
    for raw in (*include_canonical, *tags):
        if isinstance(raw, str):
            norm = normalize_tag(raw)
            if norm:
                merged.add(norm)
    return tuple(sorted(merged))


def get_category_spec(category_id: str) -> CategorySpec:
    """Lookup a canonical Task Object or Task CategorySpec by ID."""
    if category_id not in CATEGORY_REGISTRY:
        raise KeyError(
            f"Unknown category_id {category_id!r}; expected one of {sorted(CATEGORY_REGISTRY)}"
        )
    return CATEGORY_REGISTRY[category_id]


def _coerce_relation_edge(
    item: str | Mapping[str, Any] | RelationEdge, idx: int
) -> tuple[RelationEdge | None, str | None]:
    if isinstance(item, RelationEdge):
        return item, None
    if isinstance(item, str):
        if item not in ALLOWED_RELATION_TYPES:
            return None, f"relations[{idx}]: unknown relation type {item!r}"
        return RelationEdge(relation=item, target_id=f"unspecified:{idx}"), None
    if isinstance(item, Mapping):
        rel = item.get("relation") or item.get("rel") or item.get("type")
        target = item.get("target_id") or item.get("target") or ""
        target_cat = item.get("target_category")
        note = str(item.get("note", ""))
        if rel not in ALLOWED_RELATION_TYPES:
            return None, f"relations[{idx}]: unknown relation type {rel!r}"
        if not isinstance(target, str) or not target.strip():
            return (
                None,
                f"relations[{idx}]: relation {rel!r} requires non-empty target_id",
            )
        if target_cat is not None and target_cat not in CATEGORY_REGISTRY:
            return None, f"relations[{idx}]: unknown target_category {target_cat!r}"
        return (
            RelationEdge(
                relation=rel,
                target_id=target.strip(),
                target_category=target_cat,
                note=note,
            ),
            None,
        )
    return None, f"relations[{idx}]: unsupported relation entry {type(item).__name__}"


def validate_category_payload(
    category_id: str,
    dimensions: Mapping[str, float | int | str],
    relations: Sequence[str | Mapping[str, Any] | RelationEdge] = (),
    tags: Sequence[str] = (),
) -> CategoryValidationResult:
    """Validate category dimensions, typed relations, and tags; compute conformance score.

    Returns a deterministic CategoryValidationResult with normalized tags, normalized
    [0.0, 1.0] dimension values, weighted quality score, and overall conformance score.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if category_id not in CATEGORY_REGISTRY:
        canonical_payload = json.dumps(
            {
                "category_id": category_id,
                "dimensions": dict(dimensions),
                "tags": list(tags),
            },
            sort_keys=True,
            default=str,
        )
        return CategoryValidationResult(
            category_id=category_id,
            valid=False,
            conformance_score=0.0,
            quality_score=0.0,
            normalized_dimensions={},
            normalized_tags=normalize_tags(tags),
            validated_relations=(),
            errors=(f"Unknown category_id {category_id!r}",),
            warnings=(),
            payload_sha256=hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
        )

    spec = CATEGORY_REGISTRY[category_id]
    normalized_dims: dict[str, float] = {}
    weighted_sum = 0.0
    present_weight_sum = 0.0

    # Check required and provided dimensions
    for axis_name, dim_spec in spec.dimensions.items():
        if axis_name not in dimensions:
            if dim_spec.required:
                errors.append(f"Missing required dimension {axis_name!r}")
            continue

        raw_val = dimensions[axis_name]
        if dim_spec.dtype == "categorical":
            if not isinstance(raw_val, str) or raw_val not in dim_spec.allowed_values:
                errors.append(
                    f"Dimension {axis_name!r} value {raw_val!r} not in {dim_spec.allowed_values}"
                )
                continue
            norm_val = dim_spec.normalize_value(raw_val)
        else:
            if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
                errors.append(
                    f"Dimension {axis_name!r} expected numeric {dim_spec.dtype}, got {type(raw_val).__name__}"
                )
                continue
            if dim_spec.dtype == "int" and not isinstance(raw_val, int):
                errors.append(f"Dimension {axis_name!r} expected int, got float")
                continue
            fval = float(raw_val)
            if fval < dim_spec.min_val or fval > dim_spec.max_val:
                errors.append(
                    f"Dimension {axis_name!r} value {fval} outside bounds [{dim_spec.min_val}, {dim_spec.max_val}]"
                )
                continue
            norm_val = dim_spec.normalize_value(fval)

        normalized_dims[axis_name] = round(norm_val, 6)
        weighted_sum += dim_spec.default_weight * norm_val
        present_weight_sum += dim_spec.default_weight

    known_domain_axes = set(spec.domain_dimensions)
    for extra_axis in sorted(set(dimensions) - set(spec.dimensions)):
        if extra_axis in known_domain_axes:
            continue
        warnings.append(f"Unrecognized dimension {extra_axis!r} ignored for {category_id}")

    # Validate relations
    validated_edges: list[RelationEdge] = []
    allowed_set = set(spec.allowed_relations)
    for idx, raw_rel in enumerate(relations):
        edge, err = _coerce_relation_edge(raw_rel, idx)
        if err is not None or edge is None:
            errors.append(err or f"relations[{idx}]: invalid relation")
            continue
        if edge.relation not in allowed_set:
            errors.append(
                f"Relation {edge.relation!r} is not permitted for {category_id}; allowed={spec.allowed_relations}"
            )
            continue
        validated_edges.append(edge)

    # Normalize tags and include canonical category tags
    norm_tags = normalize_tags(tags, include_canonical=spec.canonical_tags)
    for raw_t in tags:
        if not isinstance(raw_t, str) or not normalize_tag(raw_t):
            warnings.append(f"Empty or non-string tag {raw_t!r} dropped during normalization")

    quality_score = (weighted_sum / present_weight_sum) if present_weight_sum > 0.0 else 0.0

    required_count = sum(1 for d in spec.dimensions.values() if d.required)
    satisfied_required = sum(
        1 for name, d in spec.dimensions.items() if d.required and name in normalized_dims
    )
    required_coverage = (satisfied_required / required_count) if required_count > 0 else 1.0

    # Structural conformance penalizes errors sharply and blends coverage + quality
    penalty = min(1.0, 0.25 * len(errors) + 0.03 * len(warnings))
    raw_conformance = (0.65 * required_coverage + 0.35 * quality_score) * (1.0 - penalty)
    conformance_score = round(max(0.0, min(1.0, raw_conformance)), 6)

    canonical_digest_doc = {
        "category_id": category_id,
        "normalized_dimensions": normalized_dims,
        "normalized_tags": norm_tags,
        "relations": [e.model_dump() for e in validated_edges],
    }
    payload_sha256 = hashlib.sha256(
        json.dumps(canonical_digest_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return CategoryValidationResult(
        category_id=category_id,
        valid=(len(errors) == 0),
        conformance_score=conformance_score,
        quality_score=round(quality_score, 6),
        normalized_dimensions=normalized_dims,
        normalized_tags=norm_tags,
        validated_relations=tuple(validated_edges),
        errors=tuple(errors),
        warnings=tuple(warnings),
        payload_sha256=payload_sha256,
    )


def init_ontology_sqlite_schema(conn: sqlite3.Connection) -> None:
    """Initialize SQLite tables for categorized ontology records and typed relation edges."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_records (
            record_id TEXT PRIMARY KEY,
            category_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            conformance_score REAL NOT NULL,
            quality_score REAL NOT NULL,
            dimensions_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            last_verified_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ontology_relations (
            source_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_category TEXT,
            note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (source_id, relation, target_id)
        );
        """
    )
    conn.commit()


def store_validated_record(
    conn: sqlite3.Connection,
    record_id: str,
    validation: CategoryValidationResult,
    *,
    last_verified_at: float = 0.0,
) -> None:
    """Persist a validated ontology record and its outgoing edges into SQLite."""
    if not validation.valid:
        raise ValueError(f"Cannot store invalid payload for {record_id}: {validation.errors}")
    spec = get_category_spec(validation.category_id)
    conn.execute(
        """
        INSERT OR REPLACE INTO ontology_records (
            record_id, category_id, kind, payload_sha256,
            conformance_score, quality_score, dimensions_json, tags_json, last_verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            validation.category_id,
            spec.kind,
            validation.payload_sha256,
            validation.conformance_score,
            validation.quality_score,
            json.dumps(validation.normalized_dimensions, sort_keys=True),
            json.dumps(validation.normalized_tags),
            float(last_verified_at),
        ),
    )
    for edge in validation.validated_relations:
        conn.execute(
            """
            INSERT OR REPLACE INTO ontology_relations (
                source_id, relation, target_id, target_category, note
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (record_id, edge.relation, edge.target_id, edge.target_category, edge.note),
        )
    conn.commit()
