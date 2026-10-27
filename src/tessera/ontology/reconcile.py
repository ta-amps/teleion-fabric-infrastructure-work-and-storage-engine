"""Deterministic 0-call 3-way CAS merge and Sybil-fenced calibration-weighted voting.

Auto-merges disjoint field edits, monotonic set/tag unions, and commutative counter
increments in 0 LLM calls; isolates true field collisions into a ConflictRecord and
adjudicates ballots with self-vote blocking, duplicate-key blocking, calibration
weighting, SHA-256 receipts, and negative.knowledge tombstones.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MergeStatus = Literal["auto_merged", "needs_voting_or_steward"]
AdjudicationStatus = Literal["winner_promoted", "park_for_human"]
BallotChoice = Literal["candidate_a", "candidate_b", "park_for_human"]

DEFAULT_MONOTONIC_SET_FIELDS: frozenset[str] = frozenset(
    {
        "tags",
        "labels",
        "citations",
        "evidence_ids",
        "corroborated_by",
        "verified_by",
        "aliases",
        "capabilities",
    }
)

DEFAULT_COUNTER_FIELDS: frozenset[str] = frozenset(
    {
        "count",
        "pass_count",
        "fail_count",
        "trial_count",
        "trials",
        "successes",
        "verification_count",
        "recheck_count",
        "citation_count",
        "usage_count",
        "hit_count",
        "token_count",
    }
)

_MISSING = object()
_PROVENANCE_META_KEYS: frozenset[str] = frozenset({"_proposer_key_id"})


def _canonical_json(obj: Any) -> str:
    def _default(val: Any) -> Any:
        if isinstance(val, (set, frozenset)):
            return sorted(val, key=str)
        if isinstance(val, BaseModel):
            return val.model_dump()
        raise TypeError(f"Non-serializable type {type(val).__name__}")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def canonical_sha256(obj: Any) -> str:
    """Compute deterministic SHA-256 hex digest over canonical JSON representation."""
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


class ConflictCandidate(BaseModel):
    """Competing candidate revision inside a 3-way conflict."""

    model_config = ConfigDict(frozen=True)

    candidate_id: Literal["candidate_a", "candidate_b"]
    proposer_key_id: str | None = None
    conflicting_values: dict[str, Any]
    document: dict[str, Any]
    sha256: str


class ConflictRecord(BaseModel):
    """Structured record of field-level collisions isolated by reconcile_three_way."""

    model_config = ConfigDict(frozen=True)

    conflict_id: str
    status: MergeStatus
    merged: dict[str, Any] | None = None
    merged_sha256: str | None = None
    partial_merged: dict[str, Any] = Field(default_factory=dict)
    conflicting_paths: tuple[str, ...] = ()
    candidate_a: ConflictCandidate | None = None
    candidate_b: ConflictCandidate | None = None
    proposer_key_ids: tuple[str, ...] = ()
    applied_operations: tuple[str, ...] = ()


class WorkerCalibration(BaseModel):
    """Historical verification accuracy ledger for a worker key."""

    model_config = ConfigDict(frozen=True)

    worker_key_id: str
    verified_correct: float = Field(ge=0.0)
    verified_total: float = Field(ge=0.0)
    prior: float = Field(default=5.0, gt=0.0)

    @property
    def calibration_score(self) -> float:
        return compute_worker_calibration(
            self.verified_correct,
            self.verified_total,
            prior=self.prior,
        )


class Ballot(BaseModel):
    """Signed/keyed verifier vote on a ConflictRecord."""

    model_config = ConfigDict(frozen=True)

    voter_key_id: str
    choice: BallotChoice
    citations: tuple[str, ...] = ()
    verification_receipt_sha256: str | None = None
    rationale: str = ""
    defect_summary: str = ""


class EvaluatedBallot(BaseModel):
    """Accepted or rejected ballot audit entry included in ReconciliationReceipt."""

    model_config = ConfigDict(frozen=True)

    voter_key_id: str
    choice: str
    accepted: bool
    rejection_reason: str | None = None
    calibration_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    effective_weight: float = Field(default=0.0, ge=0.0)


class NegativeTombstoneSpec(BaseModel):
    """Negative knowledge tombstone spec emitted for rejected buggy candidates."""

    model_config = ConfigDict(frozen=True)

    tombstone_id: str
    category_id: Literal["noun.negative_tombstone"] = "noun.negative_tombstone"
    tags: tuple[str, ...] = ("negative.knowledge", "tombstone", "conflict-rejected")
    conflict_id: str
    rejected_candidate_id: Literal["candidate_a", "candidate_b"]
    rejected_proposer_key_id: str | None = None
    rejected_sha256: str
    superseded_by_candidate_id: Literal["candidate_a", "candidate_b"]
    superseded_by_sha256: str
    conflicting_paths: tuple[str, ...]
    rejected_values: dict[str, Any]
    defect_summary: str
    tombstone_sha256: str


class ReconciliationReceipt(BaseModel):
    """Deterministic SHA-256 receipt of multi-worker conflict adjudication."""

    model_config = ConfigDict(frozen=True)

    conflict_id: str
    status: AdjudicationStatus
    human_only: bool
    quorum_met: bool
    winning_candidate_id: Literal["candidate_a", "candidate_b"] | None = None
    promoted_document: dict[str, Any] | None = None
    promoted_sha256: str | None = None
    weight_candidate_a: float = Field(ge=0.0)
    weight_candidate_b: float = Field(ge=0.0)
    weight_park_for_human: float = Field(ge=0.0)
    margin: float = Field(ge=0.0, le=1.0)
    valid_ballot_count: int = Field(ge=0)
    calibrated_verifier_count: int = Field(ge=0)
    accepted_ballots: tuple[EvaluatedBallot, ...]
    rejected_ballots: tuple[EvaluatedBallot, ...]
    negative_tombstone: NegativeTombstoneSpec | None = None
    receipt_sha256: str


def _is_int_not_bool(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _is_hashable_scalar(val: Any) -> bool:
    return isinstance(val, (str, int, float, bool))


def _is_set_field(path: str, leaf: str, custom_set_fields: set[str]) -> bool:
    if path in custom_set_fields or leaf in custom_set_fields:
        return True
    if leaf in DEFAULT_MONOTONIC_SET_FIELDS:
        return True
    return leaf.endswith(("_tags", "_set", "_ids"))


def _is_counter_field(path: str, leaf: str, custom_counter_fields: set[str]) -> bool:
    if path in custom_counter_fields or leaf in custom_counter_fields:
        return True
    if leaf in DEFAULT_COUNTER_FIELDS:
        return True
    if leaf.endswith(("_count", "_counter")):
        return True
    return path.startswith("counters.")


def _try_monotonic_set_union(
    base_val: Any,
    ours_val: Any,
    theirs_val: Any,
    is_named_set_field: bool,
) -> tuple[bool, Any]:
    """Attempt monotonic G-Set union for Python sets or tagged scalar lists."""
    # Case 1: Native Python set/frozenset
    if isinstance(ours_val, (set, frozenset)) and isinstance(theirs_val, (set, frozenset)):
        b_set = set(base_val) if isinstance(base_val, (set, frozenset, list, tuple)) else set()
        if b_set.issubset(ours_val) and b_set.issubset(theirs_val):
            merged_elems = sorted(set(ours_val) | set(theirs_val), key=str)
            return (True, merged_elems)
        return (False, None)

    # Case 2: JSON list on a designated monotonic set/tag field
    if not is_named_set_field:
        return (False, None)
    if not (isinstance(ours_val, list) and isinstance(theirs_val, list)):
        return (False, None)
    b_list = [] if base_val is _MISSING else (base_val if isinstance(base_val, list) else None)
    if b_list is None:
        return (False, None)
    if not (
        all(_is_hashable_scalar(x) for x in b_list)
        and all(_is_hashable_scalar(x) for x in ours_val)
        and all(_is_hashable_scalar(x) for x in theirs_val)
    ):
        return (False, None)

    b_set = set(b_list)
    o_set = set(ours_val)
    t_set = set(theirs_val)
    # Monotonicity guard: neither side deleted an element present in base
    if not (b_set.issubset(o_set) and b_set.issubset(t_set)):
        return (False, None)

    merged_list = sorted(o_set | t_set, key=lambda x: (type(x).__name__, str(x)))
    return (True, merged_list)


def reconcile_three_way(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    *,
    ours_proposer_key_id: str | None = None,
    theirs_proposer_key_id: str | None = None,
    set_fields: Sequence[str] = (),
    counter_fields: Sequence[str] = (),
) -> ConflictRecord:
    """Perform deterministic 0-call 3-way merge over structured context/CAS dictionaries.

    Auto-merges disjoint field edits, monotonic set/tag unions, and commutative counter
    increments; isolates true field-level value collisions into a ConflictRecord.
    """
    custom_sets = set(set_fields)
    custom_counters = set(counter_fields)

    prop_a = ours_proposer_key_id or (
        str(ours["_proposer_key_id"]) if "_proposer_key_id" in ours else None
    )
    prop_b = theirs_proposer_key_id or (
        str(theirs["_proposer_key_id"]) if "_proposer_key_id" in theirs else None
    )

    clean_base = {k: v for k, v in base.items() if k not in _PROVENANCE_META_KEYS}
    clean_ours = {k: v for k, v in ours.items() if k not in _PROVENANCE_META_KEYS}
    clean_theirs = {k: v for k, v in theirs.items() if k not in _PROVENANCE_META_KEYS}

    conflicting_paths: list[str] = []
    cand_a_vals: dict[str, Any] = {}
    cand_b_vals: dict[str, Any] = {}
    operations: list[str] = []

    def _merge_dict(
        b_map: Mapping[str, Any],
        o_map: Mapping[str, Any],
        t_map: Mapping[str, Any],
        prefix: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        partial_out: dict[str, Any] = {}
        doc_a_out: dict[str, Any] = {}
        doc_b_out: dict[str, Any] = {}

        all_keys = sorted(set(b_map.keys()) | set(o_map.keys()) | set(t_map.keys()))
        for key in all_keys:
            path = f"{prefix}.{key}" if prefix else key
            b_val = b_map.get(key, _MISSING)
            o_val = o_map.get(key, _MISSING)
            t_val = t_map.get(key, _MISSING)

            # 1. Both sides identical (unchanged or convergent edit/deletion)
            if o_val == t_val:
                if o_val is not _MISSING:
                    val_copy = copy.deepcopy(o_val)
                    if isinstance(val_copy, (set, frozenset)):
                        val_copy = sorted(val_copy, key=str)
                    partial_out[key] = val_copy
                    doc_a_out[key] = copy.deepcopy(val_copy)
                    doc_b_out[key] = copy.deepcopy(val_copy)
                if b_val != o_val:
                    operations.append(f"convergent_edit:{path}")
                continue

            # 2. Ours unchanged from base -> take theirs (edit, addition, or deletion)
            if o_val == b_val:
                if t_val is not _MISSING:
                    val_copy = copy.deepcopy(t_val)
                    if isinstance(val_copy, (set, frozenset)):
                        val_copy = sorted(val_copy, key=str)
                    partial_out[key] = val_copy
                    doc_a_out[key] = copy.deepcopy(val_copy)
                    doc_b_out[key] = copy.deepcopy(val_copy)
                    operations.append(f"theirs_edit:{path}")
                else:
                    operations.append(f"theirs_delete:{path}")
                continue

            # 3. Theirs unchanged from base -> take ours (edit, addition, or deletion)
            if t_val == b_val:
                if o_val is not _MISSING:
                    val_copy = copy.deepcopy(o_val)
                    if isinstance(val_copy, (set, frozenset)):
                        val_copy = sorted(val_copy, key=str)
                    partial_out[key] = val_copy
                    doc_a_out[key] = copy.deepcopy(val_copy)
                    doc_b_out[key] = copy.deepcopy(val_copy)
                    operations.append(f"ours_edit:{path}")
                else:
                    operations.append(f"ours_delete:{path}")
                continue

            # Both sides diverged from base and differ from each other:
            # 4. Recursive nested dictionary merge
            if isinstance(o_val, Mapping) and isinstance(t_val, Mapping):
                b_sub = b_val if isinstance(b_val, Mapping) else {}
                sub_p, sub_a, sub_b = _merge_dict(b_sub, o_val, t_val, path)
                partial_out[key] = sub_p
                doc_a_out[key] = sub_a
                doc_b_out[key] = sub_b
                continue

            # 5. Monotonic set/tag union
            ok_set, union_val = _try_monotonic_set_union(
                b_val,
                o_val,
                t_val,
                _is_set_field(path, key, custom_sets),
            )
            if ok_set:
                partial_out[key] = union_val
                doc_a_out[key] = copy.deepcopy(union_val)
                doc_b_out[key] = copy.deepcopy(union_val)
                operations.append(f"monotonic_set_union:{path}")
                continue

            # 6. Commutative integer counter increment
            if (
                _is_counter_field(path, key, custom_counters)
                and _is_int_not_bool(o_val)
                and _is_int_not_bool(t_val)
                and (b_val is _MISSING or _is_int_not_bool(b_val))
            ):
                b_int = 0 if b_val is _MISSING else int(b_val)
                merged_counter = b_int + (int(o_val) - b_int) + (int(t_val) - b_int)
                partial_out[key] = merged_counter
                doc_a_out[key] = merged_counter
                doc_b_out[key] = merged_counter
                operations.append(f"commutative_counter:{path}")
                continue

            # 7. True field-level value collision
            conflicting_paths.append(path)
            a_repr = None if o_val is _MISSING else copy.deepcopy(o_val)
            b_repr = None if t_val is _MISSING else copy.deepcopy(t_val)
            cand_a_vals[path] = a_repr
            cand_b_vals[path] = b_repr
            if o_val is not _MISSING:
                doc_a_out[key] = copy.deepcopy(o_val)
            if t_val is not _MISSING:
                doc_b_out[key] = copy.deepcopy(t_val)
            if b_val is not _MISSING:
                partial_out[key] = copy.deepcopy(b_val)

        return (partial_out, doc_a_out, doc_b_out)

    partial_merged, doc_a, doc_b = _merge_dict(clean_base, clean_ours, clean_theirs, "")
    proposers = tuple(sorted({p for p in (prop_a, prop_b) if p}))

    if not conflicting_paths:
        merged_digest = canonical_sha256(partial_merged)
        conflict_id = "merge_" + merged_digest[:16]
        return ConflictRecord(
            conflict_id=conflict_id,
            status="auto_merged",
            merged=partial_merged,
            merged_sha256=merged_digest,
            partial_merged=partial_merged,
            conflicting_paths=(),
            candidate_a=None,
            candidate_b=None,
            proposer_key_ids=proposers,
            applied_operations=tuple(operations),
        )

    sha_a = canonical_sha256(doc_a)
    sha_b = canonical_sha256(doc_b)
    conflict_seed = {
        "base_sha256": canonical_sha256(clean_base),
        "candidate_a_sha256": sha_a,
        "candidate_b_sha256": sha_b,
        "conflicting_paths": conflicting_paths,
    }
    conflict_id = "conflict_" + canonical_sha256(conflict_seed)[:16]

    return ConflictRecord(
        conflict_id=conflict_id,
        status="needs_voting_or_steward",
        merged=None,
        merged_sha256=None,
        partial_merged=partial_merged,
        conflicting_paths=tuple(conflicting_paths),
        candidate_a=ConflictCandidate(
            candidate_id="candidate_a",
            proposer_key_id=prop_a,
            conflicting_values=cand_a_vals,
            document=doc_a,
            sha256=sha_a,
        ),
        candidate_b=ConflictCandidate(
            candidate_id="candidate_b",
            proposer_key_id=prop_b,
            conflicting_values=cand_b_vals,
            document=doc_b,
            sha256=sha_b,
        ),
        proposer_key_ids=proposers,
        applied_operations=tuple(operations),
    )


def compute_worker_calibration(
    verified_correct: float | int,
    verified_total: float | int,
    prior: float = 5.0,
) -> float:
    """Compute Sybil-resistant worker calibration score: verified_correct / (verified_total + prior)."""
    if prior <= 0.0:
        raise ValueError(f"prior must be > 0, got {prior}")
    correct = max(0.0, float(verified_correct))
    total = max(correct, float(verified_total))
    return max(0.0, min(1.0, correct / (total + float(prior))))


def _resolve_calibration_score(
    voter_key_id: str,
    worker_calibrations: Mapping[str, WorkerCalibration | Mapping[str, Any] | float | int],
    default_prior: float = 5.0,
) -> float:
    if voter_key_id not in worker_calibrations:
        return 0.0
    entry = worker_calibrations[voter_key_id]
    if isinstance(entry, WorkerCalibration):
        return entry.calibration_score
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return max(0.0, min(1.0, float(entry)))
    if isinstance(entry, Mapping):
        if "calibration_score" in entry and entry["calibration_score"] is not None:
            return max(0.0, min(1.0, float(entry["calibration_score"])))
        correct = float(entry.get("verified_correct", 0.0))
        total = float(entry.get("verified_total", 0.0))
        prior = float(entry.get("prior", default_prior))
        return compute_worker_calibration(correct, total, prior=prior)
    return 0.0


def compute_evidence_completeness(ballot: Ballot) -> float:
    """Score evidence-citation completeness in [0.15, 1.0] from citations, receipt hash, and rationale."""
    unique_cites = {c.strip() for c in ballot.citations if isinstance(c, str) and c.strip()}
    cite_ratio = min(1.0, len(unique_cites) / 2.0)
    has_receipt = bool(
        ballot.verification_receipt_sha256 and ballot.verification_receipt_sha256.strip()
    )
    has_rationale = len(ballot.rationale.strip()) >= 12

    score = 0.60 * cite_ratio + (0.25 if has_receipt else 0.0) + (0.15 if has_rationale else 0.0)
    return round(max(0.15, min(1.0, score)), 6)


def _coerce_ballot(raw: Ballot | Mapping[str, Any]) -> Ballot:
    if isinstance(raw, Ballot):
        return raw
    data = dict(raw)
    cites = data.get("citations") or ()
    return Ballot(
        voter_key_id=str(data.get("voter_key_id") or "").strip(),
        choice=data.get("choice", "park_for_human"),
        citations=tuple(cites),
        verification_receipt_sha256=data.get("verification_receipt_sha256"),
        rationale=str(data.get("rationale") or ""),
        defect_summary=str(data.get("defect_summary") or ""),
    )


def adjudicate_ballots(
    conflict: ConflictRecord | Mapping[str, Any],
    ballots: Sequence[Ballot | Mapping[str, Any]],
    worker_calibrations: Mapping[str, WorkerCalibration | Mapping[str, Any] | float | int],
    human_only: bool = False,
    *,
    default_prior: float = 5.0,
    min_quorum: int = 3,
    calibrated_fast_quorum: int = 2,
    min_calibrated_score: float = 0.65,
    margin_threshold: float = 0.15,
) -> ReconciliationReceipt:
    """Adjudicate competing conflict candidates via Sybil-fenced calibration-weighted voting."""
    conf = (
        conflict
        if isinstance(conflict, ConflictRecord)
        else ConflictRecord.model_validate(conflict)
    )
    if (
        conf.status != "needs_voting_or_steward"
        or conf.candidate_a is None
        or conf.candidate_b is None
    ):
        raise ValueError(
            "adjudicate_ballots requires a ConflictRecord with status='needs_voting_or_steward'"
        )

    proposer_ids: set[str] = set(conf.proposer_key_ids)
    if conf.candidate_a.proposer_key_id:
        proposer_ids.add(conf.candidate_a.proposer_key_id)
    if conf.candidate_b.proposer_key_id:
        proposer_ids.add(conf.candidate_b.proposer_key_id)

    seen_voters: set[str] = set()
    accepted: list[EvaluatedBallot] = []
    rejected: list[EvaluatedBallot] = []

    weight_a = 0.0
    weight_b = 0.0
    weight_park = 0.0
    calibrated_voters_a = 0
    calibrated_voters_b = 0
    defect_notes_against_a: list[str] = []
    defect_notes_against_b: list[str] = []

    for raw_b in ballots:
        voter_id_raw = (
            raw_b.voter_key_id
            if isinstance(raw_b, Ballot)
            else str(raw_b.get("voter_key_id") or "").strip()
        )
        choice_raw = raw_b.choice if isinstance(raw_b, Ballot) else str(raw_b.get("choice") or "")

        if not voter_id_raw:
            rejected.append(
                EvaluatedBallot(
                    voter_key_id="",
                    choice=choice_raw,
                    accepted=False,
                    rejection_reason="missing_voter_key",
                )
            )
            continue

        if voter_id_raw in proposer_ids:
            rejected.append(
                EvaluatedBallot(
                    voter_key_id=voter_id_raw,
                    choice=choice_raw,
                    accepted=False,
                    rejection_reason="self_vote_forbidden",
                )
            )
            continue

        if voter_id_raw in seen_voters:
            rejected.append(
                EvaluatedBallot(
                    voter_key_id=voter_id_raw,
                    choice=choice_raw,
                    accepted=False,
                    rejection_reason="duplicate_voter_key",
                )
            )
            continue

        if choice_raw not in ("candidate_a", "candidate_b", "park_for_human"):
            rejected.append(
                EvaluatedBallot(
                    voter_key_id=voter_id_raw,
                    choice=choice_raw,
                    accepted=False,
                    rejection_reason="invalid_choice",
                )
            )
            continue

        seen_voters.add(voter_id_raw)
        ballot = _coerce_ballot(raw_b)
        cal_score = _resolve_calibration_score(
            ballot.voter_key_id,
            worker_calibrations,
            default_prior=default_prior,
        )
        if cal_score <= 0.0:
            rejected.append(
                EvaluatedBallot(
                    voter_key_id=ballot.voter_key_id,
                    choice=ballot.choice,
                    accepted=False,
                    rejection_reason="uncalibrated_zero_weight",
                    calibration_score=0.0,
                    evidence_completeness=compute_evidence_completeness(ballot),
                    effective_weight=0.0,
                )
            )
            continue

        ev_comp = compute_evidence_completeness(ballot)
        eff_weight = round(cal_score * ev_comp, 6)

        accepted.append(
            EvaluatedBallot(
                voter_key_id=ballot.voter_key_id,
                choice=ballot.choice,
                accepted=True,
                rejection_reason=None,
                calibration_score=round(cal_score, 6),
                evidence_completeness=ev_comp,
                effective_weight=eff_weight,
            )
        )

        if ballot.choice == "candidate_a":
            weight_a += eff_weight
            if cal_score >= min_calibrated_score:
                calibrated_voters_a += 1
            if ballot.defect_summary.strip():
                defect_notes_against_b.append(ballot.defect_summary.strip())
        elif ballot.choice == "candidate_b":
            weight_b += eff_weight
            if cal_score >= min_calibrated_score:
                calibrated_voters_b += 1
            if ballot.defect_summary.strip():
                defect_notes_against_a.append(ballot.defect_summary.strip())
        else:
            weight_park += eff_weight

    decisive_weight = weight_a + weight_b
    margin = (abs(weight_a - weight_b) / decisive_weight) if decisive_weight > 0.0 else 0.0

    decisive_voters = sum(1 for b in accepted if b.choice in ("candidate_a", "candidate_b"))
    total_calibrated = calibrated_voters_a + calibrated_voters_b
    leading_calibrated = max(calibrated_voters_a, calibrated_voters_b)

    quorum_met = decisive_voters >= min_quorum or leading_calibrated >= calibrated_fast_quorum

    park_majority = weight_park >= decisive_weight and (weight_park > 0.0 or decisive_weight == 0.0)

    status: AdjudicationStatus
    winner_id: Literal["candidate_a", "candidate_b"] | None = None
    promoted_doc: dict[str, Any] | None = None
    promoted_sha: str | None = None
    tombstone: NegativeTombstoneSpec | None = None

    if (
        not human_only
        and not park_majority
        and quorum_met
        and margin >= margin_threshold
        and weight_a != weight_b
    ):
        status = "winner_promoted"
        if weight_a > weight_b:
            winner_cand = conf.candidate_a
            loser_cand = conf.candidate_b
            defect_notes = defect_notes_against_b
        else:
            winner_cand = conf.candidate_b
            loser_cand = conf.candidate_a
            defect_notes = defect_notes_against_a

        winner_id = winner_cand.candidate_id
        promoted_doc = winner_cand.document
        promoted_sha = winner_cand.sha256

        summary_text = (
            "; ".join(defect_notes)
            if defect_notes
            else (
                f"Rejected {loser_cand.candidate_id} on paths {list(conf.conflicting_paths)} "
                f"in favor of calibrated winner {winner_cand.candidate_id} (margin={margin:.4f})."
            )
        )
        tombstone_seed = {
            "conflict_id": conf.conflict_id,
            "rejected_candidate_id": loser_cand.candidate_id,
            "rejected_sha256": loser_cand.sha256,
            "superseded_by_sha256": winner_cand.sha256,
            "conflicting_paths": conf.conflicting_paths,
        }
        tombstone_sha = canonical_sha256(tombstone_seed)
        tombstone = NegativeTombstoneSpec(
            tombstone_id="tombstone_" + tombstone_sha[:16],
            conflict_id=conf.conflict_id,
            rejected_candidate_id=loser_cand.candidate_id,
            rejected_proposer_key_id=loser_cand.proposer_key_id,
            rejected_sha256=loser_cand.sha256,
            superseded_by_candidate_id=winner_cand.candidate_id,
            superseded_by_sha256=winner_cand.sha256,
            conflicting_paths=conf.conflicting_paths,
            rejected_values=loser_cand.conflicting_values,
            defect_summary=summary_text,
            tombstone_sha256=tombstone_sha,
        )
    else:
        status = "park_for_human"

    receipt_payload = {
        "conflict_id": conf.conflict_id,
        "status": status,
        "human_only": human_only,
        "quorum_met": quorum_met,
        "winning_candidate_id": winner_id,
        "promoted_sha256": promoted_sha,
        "weight_candidate_a": round(weight_a, 6),
        "weight_candidate_b": round(weight_b, 6),
        "weight_park_for_human": round(weight_park, 6),
        "margin": round(margin, 6),
        "accepted_ballots": [b.model_dump() for b in accepted],
        "rejected_ballots": [b.model_dump() for b in rejected],
        "tombstone_sha256": tombstone.tombstone_sha256 if tombstone else None,
    }
    receipt_digest = canonical_sha256(receipt_payload)

    return ReconciliationReceipt(
        conflict_id=conf.conflict_id,
        status=status,
        human_only=human_only,
        quorum_met=quorum_met,
        winning_candidate_id=winner_id,
        promoted_document=promoted_doc,
        promoted_sha256=promoted_sha,
        weight_candidate_a=round(weight_a, 6),
        weight_candidate_b=round(weight_b, 6),
        weight_park_for_human=round(weight_park, 6),
        margin=round(margin, 6),
        valid_ballot_count=len(accepted),
        calibrated_verifier_count=total_calibrated,
        accepted_ballots=tuple(accepted),
        rejected_ballots=tuple(rejected),
        negative_tombstone=tombstone,
        receipt_sha256=receipt_digest,
    )
