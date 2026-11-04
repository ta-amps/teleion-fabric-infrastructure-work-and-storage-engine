"""Caller profile policies and adaptive rendering for Nouns and Verb task guidance.

Defines the 8 canonical CallerProfile configurations and adapts identical underlying
Noun records and Verb CategorySpecs for coding harnesses, chat research agents,
NGO caseworkers, human stewards, deterministic verifiers, low-budget donors,
incident responders, and public researchers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .categories import CategorySpec, get_category_spec

ExecutionTier = Literal["0_call_reflex", "hybrid_1_call", "agent_decompose"]
ResponseFormat = Literal[
    "compact_signatures",
    "plain_language_caveats",
    "strict_json_receipts",
]

_CLAMP_SUFFIX = "...[clamped]"


class CallerProfile(BaseModel):
    """Adaptive execution, formatting, cost, and safety policy for a caller cohort."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    title: str
    cohort_persona: str
    clamped_max_chars: int = Field(gt=64)
    preferred_execution_tier: ExecutionTier
    response_format: ResponseFormat
    auto_shim_categories: tuple[str, ...] = ()
    require_human_on_routes: tuple[str, ...] = ()
    max_task_cost_usd: float = Field(ge=0.0)


CALLER_PROFILES: dict[str, CallerProfile] = {
    "autonomous_coding_harness": CallerProfile(
        profile_id="autonomous_coding_harness",
        title="Autonomous Coding Harness",
        cohort_persona="Automated repository engineering harness (e.g. OpenCode)",
        clamped_max_chars=12000,
        preferred_execution_tier="0_call_reflex",
        response_format="compact_signatures",
        auto_shim_categories=(
            "noun.code_node",
            "noun.shim_provision",
            "noun.mcp_server",
            "noun.reflex_procedure",
            "noun.statute_card",
        ),
        require_human_on_routes=("noun.case_bundle", "verb.vote_judge"),
        max_task_cost_usd=0.25,
    ),
    "chat_research_agent": CallerProfile(
        profile_id="chat_research_agent",
        title="Chat Research Agent",
        cohort_persona="Interactive conversational synthesis assistant (e.g. Muse.AI)",
        clamped_max_chars=6000,
        preferred_execution_tier="hybrid_1_call",
        response_format="plain_language_caveats",
        auto_shim_categories=(
            "noun.feed_source",
            "noun.seed_hypothesis",
            "noun.statute_card",
        ),
        require_human_on_routes=("noun.case_bundle", "noun.ngo_kit"),
        max_task_cost_usd=0.15,
    ),
    "ngo_caseworker": CallerProfile(
        profile_id="ngo_caseworker",
        title="NGO Caseworker Surface",
        cohort_persona="Frontline humanitarian and legal-aid intake worker",
        clamped_max_chars=3200,
        preferred_execution_tier="hybrid_1_call",
        response_format="plain_language_caveats",
        auto_shim_categories=(
            "noun.ngo_kit",
            "noun.statute_card",
            "noun.case_bundle",
        ),
        require_human_on_routes=(
            "noun.statute_card",
            "noun.ngo_kit",
            "noun.case_bundle",
            "verb.reconcile_conflict",
            "verb.vote_judge",
        ),
        max_task_cost_usd=0.08,
    ),
    "human_steward": CallerProfile(
        profile_id="human_steward",
        title="Calibrated Human Steward",
        cohort_persona="Domain steward adjudicating conflicts and high-stakes releases",
        clamped_max_chars=16000,
        preferred_execution_tier="agent_decompose",
        response_format="plain_language_caveats",
        auto_shim_categories=(),
        require_human_on_routes=("noun.case_bundle", "verb.vote_judge"),
        max_task_cost_usd=1.00,
    ),
    "deterministic_verifier": CallerProfile(
        profile_id="deterministic_verifier",
        title="Deterministic Gate Verifier",
        cohort_persona="Zero-call CI/sandbox test and hash gate runner",
        clamped_max_chars=8000,
        preferred_execution_tier="0_call_reflex",
        response_format="strict_json_receipts",
        auto_shim_categories=(
            "noun.benchmark_suite",
            "noun.code_node",
            "noun.negative_tombstone",
        ),
        require_human_on_routes=("noun.case_bundle",),
        max_task_cost_usd=0.02,
    ),
    "low_budget_donor": CallerProfile(
        profile_id="low_budget_donor",
        title="Low-Budget Micro-Task Donor",
        cohort_persona="Cost-capped volunteer worker node contributing spare capacity",
        clamped_max_chars=2000,
        preferred_execution_tier="0_call_reflex",
        response_format="strict_json_receipts",
        auto_shim_categories=(
            "noun.reflex_procedure",
            "noun.shim_provision",
        ),
        require_human_on_routes=(
            "noun.statute_card",
            "noun.ngo_kit",
            "noun.case_bundle",
            "verb.vote_judge",
        ),
        max_task_cost_usd=0.01,
    ),
    "incident_responder": CallerProfile(
        profile_id="incident_responder",
        title="Incident & Fault Responder",
        cohort_persona="Low-latency operational fault containment and dead-letter recovery responder",
        clamped_max_chars=6400,
        preferred_execution_tier="0_call_reflex",
        response_format="compact_signatures",
        auto_shim_categories=(
            "noun.recovery_playbook",
            "noun.negative_tombstone",
            "noun.tombstone",
            "verb.recover_fault",
        ),
        require_human_on_routes=(
            "noun.case_bundle",
            "verb.vote_judge",
        ),
        max_task_cost_usd=0.05,
    ),
    "public_researcher": CallerProfile(
        profile_id="public_researcher",
        title="Public Reproducibility Researcher",
        cohort_persona="Open-science evaluator prioritizing benchmark suites, public sources, and provenance citations",
        clamped_max_chars=14000,
        preferred_execution_tier="hybrid_1_call",
        response_format="plain_language_caveats",
        auto_shim_categories=(
            "noun.benchmark_suite",
            "noun.public_source",
            "noun.eval_run",
        ),
        require_human_on_routes=(
            "noun.case_bundle",
            "noun.ngo_kit",
        ),
        max_task_cost_usd=0.20,
    ),
}

_PROFILE_ALIASES: dict[str, str] = {
    "swarm_worker": "autonomous_coding_harness",
}


class AdaptedNounView(BaseModel):
    """Profile-adapted projection of a canonical Task Object record."""

    model_config = ConfigDict(frozen=True)

    noun_id: str
    category_id: str
    profile_id: str
    response_format: ResponseFormat
    preferred_execution_tier: ExecutionTier
    requires_human_review: bool
    auto_shim_applied: bool
    shim_descriptor: dict[str, Any] | None = None
    max_task_cost_usd: float
    clamped_max_chars: int
    was_clamped: bool
    rendered_text: str
    structured_view: dict[str, Any]


class AdaptedTaskGuidance(BaseModel):
    """Profile-adapted execution contract and worker guidance for a Task or Task Object task."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    category_id: str
    profile_id: str
    response_format: ResponseFormat
    effective_execution_tier: ExecutionTier
    cost_clamped: bool
    effective_budget_usd: float
    requires_human_gate: bool
    auto_shim_active: bool
    required_mcp_tools: tuple[str, ...]
    deterministic_checks_before_llm: tuple[str, ...]
    post_condition_receipt_rules: tuple[str, ...]
    clamped_max_chars: int
    was_clamped: bool
    rendered_guidance: str


def get_caller_profile(profile: CallerProfile | str) -> CallerProfile:
    """Resolve a CallerProfile instance from a profile ID, alias, or model."""
    if isinstance(profile, CallerProfile):
        return profile
    resolved = _PROFILE_ALIASES.get(profile, profile)
    if resolved not in CALLER_PROFILES:
        raise KeyError(
            f"Unknown caller profile {profile!r}; expected one of {sorted(CALLER_PROFILES)}"
        )
    return CALLER_PROFILES[resolved]


def _clamp_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return (text, False)
    keep = max(0, max_chars - len(_CLAMP_SUFFIX))
    return (text[:keep] + _CLAMP_SUFFIX, True)


def _clamp_json_receipt(payload: dict[str, Any], max_chars: int) -> tuple[str, bool]:
    full = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(full) <= max_chars:
        return (full, False)
    # Trim verbose fields deterministically while preserving valid JSON
    compact_payload = {
        "id": payload.get("noun_id") or payload.get("task_id"),
        "category_id": payload.get("category_id"),
        "profile_id": payload.get("profile_id"),
        "sha256": payload.get("sha256"),
        "tier": payload.get("execution_tier"),
        "human_gate": payload.get("requires_human_review") or payload.get("requires_human_gate"),
        "budget_usd": payload.get("max_task_cost_usd") or payload.get("effective_budget_usd"),
        "clamped": True,
    }
    encoded = json.dumps(compact_payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return (encoded, True)
    return _clamp_text(encoded, max_chars)


def adapt_noun_view(
    record: Mapping[str, Any] | BaseModel,
    profile: CallerProfile | str,
) -> AdaptedNounView:
    """Render the same underlying Task Object record according to the caller's profile policy."""
    prof = get_caller_profile(profile)
    rec = record.model_dump() if isinstance(record, BaseModel) else dict(record)

    noun_id = str(rec.get("noun_id") or rec.get("item_id") or rec.get("id") or "unnamed_noun")
    category_id = str(rec.get("category_id") or "noun.code_node")
    title = str(rec.get("title") or noun_id)
    summary = str(rec.get("summary") or "")
    signature = str(rec.get("signature") or rec.get("code_signature") or f"{category_id}:{noun_id}")
    jurisdiction = str(rec.get("jurisdiction") or "global")
    citations = tuple(str(c) for c in (rec.get("citations") or ()))
    caveats = tuple(str(c) for c in (rec.get("caveats") or ()))
    checklist = tuple(str(s) for s in (rec.get("checklist") or ()))
    tombstones = tuple(str(t) for t in (rec.get("tombstone_warnings") or ()))
    raw_fresh = rec.get("freshness_confidence")
    freshness = 1.0 if raw_fresh is None else float(raw_fresh)
    raw_frag = rec.get("fragility_score")
    fragility = 0.15 if raw_frag is None else float(raw_frag)

    content_sha = str(
        rec.get("sha256")
        or hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    )

    requires_human = category_id in prof.require_human_on_routes or bool(rec.get("human_only"))
    auto_shim = category_id in prof.auto_shim_categories
    shim_desc: dict[str, Any] | None = None
    if auto_shim:
        shim_desc = {
            "shim_mode": "auto_provisioned",
            "target_category": category_id,
            "target_id": noun_id,
            "adapter_entrypoint": f"shim.bind('{category_id}', '{noun_id}')",
        }

    structured: dict[str, Any] = {
        "noun_id": noun_id,
        "category_id": category_id,
        "profile_id": prof.profile_id,
        "title": title,
        "sha256": content_sha,
        "freshness_confidence": round(freshness, 4),
        "fragility_score": round(fragility, 4),
        "execution_tier": prof.preferred_execution_tier,
        "requires_human_review": requires_human,
        "auto_shim_applied": auto_shim,
        "max_task_cost_usd": prof.max_task_cost_usd,
        "citations": list(citations),
    }

    if prof.response_format == "compact_signatures":
        lines = [
            f"[{category_id}] id={noun_id} sha256={content_sha[:16]} tier={prof.preferred_execution_tier}",
            f"sig: {signature}",
            f"metrics: freshness={freshness:.2f} fragility={fragility:.2f} budget_usd<={prof.max_task_cost_usd:.2f}",
        ]
        if auto_shim and shim_desc is not None:
            lines.append(f"shim: {shim_desc['adapter_entrypoint']}")
        if tombstones:
            lines.append(f"tombstone_guards: {', '.join(tombstones)}")
        if requires_human:
            lines.append("gate: HUMAN_REVIEW_REQUIRED")
        if citations:
            lines.append(f"refs: {', '.join(citations)}")
        raw_text = "\n".join(lines)
        rendered, was_clamped = _clamp_text(raw_text, prof.clamped_max_chars)

    elif prof.response_format == "plain_language_caveats":
        sections: list[str] = []
        if prof.profile_id == "ngo_caseworker":
            sections.append(f"FIELD CASEWORK BRIEF: {title} (Jurisdiction: {jurisdiction})")
            sections.append(
                "SAFEGUARD NOTICE: Operational reference only - not formal legal representation. "
                + (
                    "Mandatory human supervisor sign-off required before client action."
                    if requires_human
                    else "Verify local eligibility rules."
                )
            )
            if summary:
                sections.append(f"Plain Summary: {summary}")
            if checklist:
                steps_str = "; ".join(f"{idx + 1}) {step}" for idx, step in enumerate(checklist))
                sections.append(f"Intake Steps: {steps_str}")
        elif prof.profile_id == "human_steward":
            sections.append(
                f"STEWARD REVIEW PACKET: {title} [{category_id} / {noun_id}] (digest={content_sha[:16]})"
            )
            if summary:
                sections.append(f"Overview: {summary}")
            sections.append(
                f"Audit Telemetry: freshness={freshness:.0%}, fragility={fragility:.2f}, jurisdiction={jurisdiction}"
            )
        else:
            # chat_research_agent and general plain_language_caveats cohorts
            sections.append(f"Research Summary - {title} ({jurisdiction}): {summary or signature}")
            sections.append(
                f"Confidence & Currency: {freshness:.0%} freshness confidence "
                f"(fragility={fragility:.2f})."
            )

        if caveats:
            sections.append("Caveats: " + " | ".join(caveats))
        elif freshness < 0.70:
            sections.append(
                "Caveats: Artifact freshness has partially decayed; verify against primary source before relying on details."
            )
        if tombstones:
            sections.append("Known Pitfalls to Avoid: " + "; ".join(tombstones))
        if citations:
            sections.append("Grounded Citations: " + ", ".join(citations))
        if requires_human and prof.profile_id != "ngo_caseworker":
            sections.append("Action Gate: Route requires human review prior to external release.")

        raw_text = "\n".join(sections)
        rendered, was_clamped = _clamp_text(raw_text, prof.clamped_max_chars)

    else:
        # strict_json_receipts
        rendered, was_clamped = _clamp_json_receipt(structured, prof.clamped_max_chars)

    return AdaptedNounView(
        noun_id=noun_id,
        category_id=category_id,
        profile_id=prof.profile_id,
        response_format=prof.response_format,
        preferred_execution_tier=prof.preferred_execution_tier,
        requires_human_review=requires_human,
        auto_shim_applied=auto_shim,
        shim_descriptor=shim_desc,
        max_task_cost_usd=prof.max_task_cost_usd,
        clamped_max_chars=prof.clamped_max_chars,
        was_clamped=was_clamped,
        rendered_text=rendered,
        structured_view=structured,
    )


def _resolve_effective_tier(
    preferred: ExecutionTier,
    requested_cost_usd: float,
    max_cost_usd: float,
) -> tuple[ExecutionTier, float, bool]:
    if requested_cost_usd <= max_cost_usd:
        return (preferred, round(requested_cost_usd, 6), False)
    # Budget exceeded: clamp USD and step down execution tier toward 0_call_reflex
    if max_cost_usd <= 0.02:
        return ("0_call_reflex", max_cost_usd, True)
    if preferred == "agent_decompose":
        return ("hybrid_1_call", max_cost_usd, True)
    return ("0_call_reflex", max_cost_usd, True)


def adapt_task_guidance(
    task: Mapping[str, Any] | BaseModel,
    category_spec: CategorySpec | str,
    profile: CallerProfile | str,
) -> AdaptedTaskGuidance:
    """Adapt Task/Task Object worker guidance to the caller profile's tier, cost, and safety rules."""
    spec = (
        category_spec
        if isinstance(category_spec, CategorySpec)
        else get_category_spec(category_spec)
    )
    prof = get_caller_profile(profile)
    task_map = task.model_dump() if isinstance(task, BaseModel) else dict(task)

    task_id = str(task_map.get("task_id") or task_map.get("id") or "task_unspecified")
    target_cat = str(task_map.get("target_category_id") or spec.category_id)
    objective = str(task_map.get("objective") or task_map.get("prompt") or spec.summary)
    requested_cost = float(task_map.get("estimated_cost_usd", prof.max_task_cost_usd))

    eff_tier, eff_budget, cost_clamped = _resolve_effective_tier(
        prof.preferred_execution_tier,
        requested_cost,
        prof.max_task_cost_usd,
    )

    requires_human = (
        spec.category_id in prof.require_human_on_routes
        or target_cat in prof.require_human_on_routes
        or bool(task_map.get("human_only"))
    )
    auto_shim = (
        spec.category_id in prof.auto_shim_categories or target_cat in prof.auto_shim_categories
    )

    wg = spec.worker_guidance

    if prof.response_format == "compact_signatures":
        lines = [
            f"TASK {task_id} [{spec.category_id} -> {target_cat}] tier={eff_tier} budget_usd={eff_budget:.4f}",
            f"obj: {objective}",
            f"mcp: {', '.join(wg.required_mcp_tools)}",
            f"0call_gates: {' | '.join(wg.deterministic_checks_before_llm)}",
            f"receipts: {' | '.join(wg.post_condition_receipt_rules)}",
        ]
        if auto_shim:
            lines.append(f"auto_shim: enabled for {target_cat}")
        if requires_human:
            lines.append("human_gate: REQUIRED_BEFORE_COMMIT")
        rendered, was_clamped = _clamp_text("\n".join(lines), prof.clamped_max_chars)

    elif prof.response_format == "plain_language_caveats":
        lines = [
            f"Task Guidance ({prof.title}): {spec.title} [{task_id}]",
            f"Goal: {objective}",
            f"Execution Mode: {eff_tier} (budget cap ${eff_budget:.2f} USD).",
            "Before Calling Any Model: " + " ".join(wg.deterministic_checks_before_llm),
            "Context & Safeguards: " + " ".join(wg.preflight_context),
            "Required Verification Record: " + " ".join(wg.post_condition_receipt_rules),
        ]
        if requires_human:
            lines.append(
                "Human Oversight Gate: Park output for qualified human review prior to publication or client delivery."
            )
        rendered, was_clamped = _clamp_text("\n".join(lines), prof.clamped_max_chars)

    else:
        receipt_contract = {
            "task_id": task_id,
            "category_id": spec.category_id,
            "target_category_id": target_cat,
            "profile_id": prof.profile_id,
            "execution_tier": eff_tier,
            "effective_budget_usd": eff_budget,
            "cost_clamped": cost_clamped,
            "requires_human_gate": requires_human,
            "auto_shim_active": auto_shim,
            "required_mcp_tools": list(wg.required_mcp_tools),
            "deterministic_checks_before_llm": list(wg.deterministic_checks_before_llm),
            "post_condition_receipt_rules": list(wg.post_condition_receipt_rules),
            "sha256": hashlib.sha256(
                f"{task_id}:{spec.category_id}:{prof.profile_id}:{eff_tier}".encode()
            ).hexdigest(),
        }
        rendered, was_clamped = _clamp_json_receipt(receipt_contract, prof.clamped_max_chars)

    return AdaptedTaskGuidance(
        task_id=task_id,
        category_id=spec.category_id,
        profile_id=prof.profile_id,
        response_format=prof.response_format,
        effective_execution_tier=eff_tier,
        cost_clamped=cost_clamped,
        effective_budget_usd=eff_budget,
        requires_human_gate=requires_human,
        auto_shim_active=auto_shim,
        required_mcp_tools=wg.required_mcp_tools,
        deterministic_checks_before_llm=wg.deterministic_checks_before_llm,
        post_condition_receipt_rules=wg.post_condition_receipt_rules,
        clamped_max_chars=prof.clamped_max_chars,
        was_clamped=was_clamped,
        rendered_guidance=rendered,
    )
