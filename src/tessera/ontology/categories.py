"""Categorized Noun and Verb taxonomy, typed dimensions, relations, and worker guidance.

Provides the 14 canonical Noun categories and 12 canonical Verb categories for shared
knowledge graphs, deterministic worker preflight checks, payload validation, and
SQLite catalog persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

_TAG_CLEAN_RE = re.compile(r"[^a-z0-9._:-]+")
_MULTI_HYPHEN_RE = re.compile(r"-{2,}")


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
    """Structured pre-flight context, MCP requirements, 0-call gates, and receipt rules."""

    model_config = ConfigDict(frozen=True)

    preflight_context: tuple[str, ...]
    required_mcp_tools: tuple[str, ...]
    deterministic_checks_before_llm: tuple[str, ...]
    post_condition_receipt_rules: tuple[str, ...]


class CategorySpec(BaseModel):
    """Complete ontology contract for a canonical Noun or Verb category."""

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
    """Result of validating a candidate Noun or Verb payload against its CategorySpec."""

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
# 14 Canonical Noun Categories
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
                "Verify exact SHA-256 content match in local store to skip redundant synthesis.",
                "Run static syntax/import parser and type-stub lookup before LLM invocation.",
            ),
            post_condition_receipt_rules=(
                "Attach SHA-256 of normalized source bytes and test suite exit code 0.",
                "Record symbol export list and dependency edges.",
            ),
        ),
        canonical_tags=("code", "deterministic", "ast"),
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
                "Verify primary source snapshot SHA-256 before summarizing.",
            ),
            post_condition_receipt_rules=(
                "Include verbatim statutory excerpt hash, jurisdiction tag, and non-legal-advice caveat.",
                "Flag card for human steward review when effective date is ambiguous.",
            ),
        ),
        canonical_tags=("statute", "compliance", "cited"),
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
                "Load linked noun.statute_card records and regional language/literacy constraints.",
                "Check brief rules and SYNTHETIC disclosure requirements.",
            ),
            required_mcp_tools=("corpus.lookup_statute", "safety.pii_redactor"),
            deterministic_checks_before_llm=(
                "Verify zero PII tokens in template placeholders.",
                "Confirm every procedural step links to a grounded statute or agency guideline.",
            ),
            post_condition_receipt_rules=(
                "Emit checklist SHA-256, plain-language reading grade metric, and human escalation instructions.",
            ),
        ),
        canonical_tags=("ngo", "field-kit", "human-safeguard"),
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
                "Load corroborating feed sources and applicable noun.statute_card citations.",
            ),
            required_mcp_tools=("safety.pii_redactor", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Execute deterministic regex/NER redaction scan before any model call.",
                "Verify every exhibit hash exists in the evidence manifest.",
            ),
            post_condition_receipt_rules=(
                "Record redaction pass receipt, exhibit SHA-256 list, and mandatory human sign-off requirement.",
            ),
        ),
        canonical_tags=("case-bundle", "redacted", "human-gated"),
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
                "Include round-trip contract test receipt and upstream version pin.",
            ),
        ),
        canonical_tags=("shim", "adapter", "schema-bridge"),
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
                "Inspect coordinate-system convention, bounding boxes, and linked noun.motion_profile assets.",
            ),
            required_mcp_tools=("scene.validate_bounds", "asset.resolve_manifest"),
            deterministic_checks_before_llm=(
                "Verify asset URIs, frame budgets, and non-overlapping collision bounds.",
            ),
            post_condition_receipt_rules=(
                "Record deterministic seed, scene graph SHA-256, and SYNTHETIC watermark metadata if rendered.",
            ),
        ),
        canonical_tags=("scene", "spatial", "kinematics"),
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
                "Record tool list SHA-256, transport protocol, and probe HTTP status.",
            ),
        ),
        canonical_tags=("mcp", "tool-schema", "capability"),
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
                "Record fixture manifest SHA-256, rubric version, and self-test pass count.",
            ),
        ),
        canonical_tags=("benchmark", "eval", "golden-suite"),
    ),
    "noun.feed_source": CategorySpec(
        category_id="noun.feed_source",
        kind="noun",
        title="Live External Feed Source",
        summary="Volatile advisory stream, public data endpoint, or live pricing feed tracked via ETag/SHA-256.",
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
                "Load previous HTTP ETag, Last-Modified header, and body SHA-256 digest.",
                "Verify robots/terms-of-service compliance for endpoint polling.",
            ),
            required_mcp_tools=("http.conditional_fetch", "evidence.hash_manifest"),
            deterministic_checks_before_llm=(
                "Send conditional If-None-Match / HEAD request first; skip parsing on HTTP 304.",
            ),
            post_condition_receipt_rules=(
                "Record HTTP status, response SHA-256, ETag, and UTC observation timestamp.",
            ),
        ),
        canonical_tags=("live-feed", "volatile", "etag-tracked"),
    ),
    "noun.seed_hypothesis": CategorySpec(
        category_id="noun.seed_hypothesis",
        kind="noun",
        title="Seed Problem Hypothesis",
        summary="Falsifiable candidate framing, predicted failure mode, and planned verification experiments.",
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
                "Record 0-call execution latency, output SHA-256, and postcondition pass status.",
            ),
        ),
        canonical_tags=("reflex", "0-call", "distilled"),
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
                "Load failing reproduction receipt, rejected candidate SHA-256, and superseding winner ID.",
            ),
            required_mcp_tools=("evidence.hash_manifest", "tombstone.index_signature"),
            deterministic_checks_before_llm=(
                "Verify reproduction failure trace or adjudication receipt hash is attached.",
            ),
            post_condition_receipt_rules=(
                "Tag with negative.knowledge and index rejected content digest for preflight blocking.",
            ),
        ),
        canonical_tags=("negative.knowledge", "tombstone", "do-not-repeat"),
    ),
}


# ---------------------------------------------------------------------------
# 12 Canonical Verb Categories
# ---------------------------------------------------------------------------

VERB_CATEGORIES: dict[str, CategorySpec] = {
    "verb.ingest": CategorySpec(
        category_id="verb.ingest",
        kind="verb",
        title="Normalize and Ingest Artifact",
        summary="Parse raw input bytes, strip unsafe tokens, compute CAS SHA-256, and emit a typed Noun.",
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
                "Check CAS store by raw SHA-256 to deduplicate identical submissions.",
            ),
            required_mcp_tools=(
                "cas.lookup_sha256",
                "safety.pii_redactor",
                "schema.validate_json",
            ),
            deterministic_checks_before_llm=(
                "Compute SHA-256, MIME/schema parse, and PII scan deterministically before any LLM extraction.",
            ),
            post_condition_receipt_rules=(
                "Emit ingested Noun ID, canonical SHA-256, and category conformance score.",
            ),
        ),
        canonical_tags=("ingest", "cas", "normalize"),
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
                "Load target noun.feed_source URI, rate-limit window, and prior ETag/SHA-256.",
            ),
            required_mcp_tools=("http.conditional_fetch", "cas.lookup_sha256"),
            deterministic_checks_before_llm=(
                "Execute pure HTTP conditional GET; short-circuit with 0 LLM tokens on 304 or unchanged SHA-256.",
            ),
            post_condition_receipt_rules=(
                "Record HTTP status, ETag, body SHA-256, and drift classification.",
            ),
        ),
        canonical_tags=("fetch", "feed", "conditional-http"),
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
    ),
    "verb.expand_seed": CategorySpec(
        category_id="verb.expand_seed",
        kind="verb",
        title="Expand Seed Hypothesis",
        summary="Elaborate a seed hypothesis into concrete sub-tasks, required nouns, and falsification checks.",
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
                "Load target noun.seed_hypothesis, budget ceiling, and available reusable nouns.",
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
                "Assemble reusable verified nouns and auto-shims first to minimize fresh generation.",
            ),
            post_condition_receipt_rules=(
                "Emit candidate artifact SHA-256, cited input nouns, and draft status pending verify_gate.",
            ),
        ),
        canonical_tags=("solve", "draft", "synthesis"),
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
                "Load expanded step specs, input/output noun categories, and cost caps.",
            ),
            required_mcp_tools=("dag.toposort_validate", "schema.validate_json"),
            deterministic_checks_before_llm=(
                "Run cycle detection, type compatibility check, and budget sum strictly in 0-call code.",
            ),
            post_condition_receipt_rules=(
                "Emit topological node order, critical-path latency bound, and DAG manifest SHA-256.",
            ),
        ),
        canonical_tags=("dag", "compiler", "deterministic"),
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
                "Emit pass/fail counts, stdout/stderr digest, and noun.negative_tombstone on deterministic failure.",
            ),
        ),
        canonical_tags=("verify", "gate", "0-call"),
    ),
    "verb.recheck_fragility": CategorySpec(
        category_id="verb.recheck_fragility",
        kind="verb",
        title="Recheck Stale or Fragile Noun",
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
                "Compare HTTP status, ETag, and SHA-256 first; invoke re-ingestion only when status == drifted.",
            ),
            post_condition_receipt_rules=(
                "Record unchanged | drifted | unreachable_degraded status and updated last_verified_at.",
            ),
        ),
        canonical_tags=("fragility", "recheck", "freshness"),
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
                "Emit SHA-256 ReconciliationReceipt and noun.negative_tombstone for rejected buggy loser.",
            ),
        ),
        canonical_tags=("voting", "judging", "sybil-fenced"),
    ),
    "verb.distill_reflex": CategorySpec(
        category_id="verb.distill_reflex",
        kind="verb",
        title="Distill Verified Trace into Reflex",
        summary="Compress repeated high-confidence execution traces into a 0-call noun.reflex_procedure.",
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
                "Emit noun.reflex_procedure digest, replay pass rate = 1.0, and token savings ratio.",
            ),
        ),
        canonical_tags=("distill", "reflex", "token-saving"),
    ),
}

CATEGORY_REGISTRY: dict[str, CategorySpec] = {**NOUN_CATEGORIES, **VERB_CATEGORIES}


def normalize_tag(raw_tag: str) -> str:
    """Normalize a tag string to deterministic lowercase ASCII token form."""
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
    """Lookup a canonical Noun or Verb CategorySpec by ID."""
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

    for extra_axis in sorted(set(dimensions) - set(spec.dimensions)):
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
