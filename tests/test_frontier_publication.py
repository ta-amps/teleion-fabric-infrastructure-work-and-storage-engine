"""Publication requires exact, namespace-bound evidence from independent principals."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from tessera import QueueConfig, Store, TaskSpec
from tessera.errors import Forbidden
from tessera.receipts import canonical_json


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Pipeline:
    def __init__(self, path: Path, *, shared_account: bool = False, dependency: bool = True):
        self.store = Store(path)
        self.ids = {}
        for role in ["draft", "verify", "review"]:
            _, grant = self.store.issue_key(
                role,
                queues=["*"],
                scopes=["store", "surface:frontier"],
                account_id="synthetic-shared" if shared_account else "synthetic-" + role,
            )
            self.ids[role] = grant.key_id
        for name in ["repair", "verify", "review"]:
            self.store.define_queue(QueueConfig(name="frontier." + name))
        self.candidate = {
            "receipt_hash": "a" * 64,
            "status": "quarantined",
            "surface": "tessera",
            "tool": "put_record",
            "field": "value",
            "source_sha256": "b" * 64,
            "license": "Apache-2.0",
            "input_schema": {"type": "object"},
            "files": [
                {
                    "path": name,
                    "content": "SYNTHETIC " + name,
                    "sha256": hashlib.sha256(("SYNTHETIC " + name).encode()).hexdigest(),
                }
                for name in ["contract.json", "fixtures.json", "validate.mjs", "SKILL.md"]
            ],
        }
        self.store.put_record(
            "frontier.candidates",
            "candidate",
            self.candidate,
            expected_version=0,
            producer_surface="surface:frontier",
            visibility_scope="surface_only",
            tags=["status:quarantined"],
        )
        self.artifact = {
            "schema_version": 1,
            "connector": "tessera-typed-work-v1",
            "source_sha256": self.candidate["source_sha256"],
            "license": "Apache-2.0",
            "target": {k: self.candidate[k] for k in ["tool", "field", "input_schema"]},
            "completion_schema": {"type": "object"},
            "files": self.candidate["files"],
        }
        self.store.enqueue(
            TaskSpec(queue="frontier.repair", task_id="draft", brief={"synthetic": True})
        )
        draft = self.store.claim("frontier.repair", "synthetic", key_id=self.ids["draft"])
        assert draft
        self.store.submit(
            "draft",
            draft.fencing_token,
            {
                "candidate_key": "candidate",
                "candidate_hash": self.candidate["receipt_hash"],
            },
            idempotency_key="draft",
            completed_by_key=self.ids["draft"],
        )
        self.store.enqueue(
            TaskSpec(
                queue="frontier.verify",
                task_id="verify",
                depends_on=["draft"] if dependency else [],
                brief={"candidate_key": "candidate", "repair_task_id": "draft"},
            )
        )
        lease = self.store.claim("frontier.verify", "synthetic", key_id=self.ids["verify"])
        assert lease
        self.lease = lease
        self.proof = {
            "passed": True,
            "verification_scope": "native_full_loop",
            "verification_task_id": "verify",
            "candidate_key": "candidate",
            "candidate_version": 1,
            "candidate_hash": self.candidate["receipt_hash"],
            "repair_task_id": "draft",
            "artifact": self.artifact,
            "artifact_sha256": digest(self.artifact),
            "evidence_sha256": "c" * 64,
            "drafter_key_id": self.ids["draft"],
            "verifier_key_id": self.ids["verify"],
        }
        self.evidence = {
            "schema_version": 1,
            **{
                k: self.proof[k]
                for k in [
                    "verification_scope",
                    "candidate_key",
                    "candidate_hash",
                    "artifact_sha256",
                ]
            },
            "controls_sha256": "c" * 64,
            "runs": [
                {
                    "harness": "synthetic-fixture",
                    "provider": "synthetic-fixture",
                    "model": "synthetic-fixture",
                    "principal_kind": "anonymous",
                    "passed": True,
                    "receipt_sha256": "e" * 64,
                }
            ],
        }
        self.proof["evidence_sha256"] = digest(self.evidence)
        self.store.put_record(
            "frontier.verifications",
            "native:" + self.proof["evidence_sha256"],
            self.evidence,
            expected_version=0,
            producer_surface="surface:frontier",
            owner_key_id=self.ids["verify"],
            visibility_scope="surface_only",
            tags=["status:quarantined"],
        )

    def verify(self, proof: dict[str, Any] | None = None) -> None:
        receipt = self.store.submit(
            "verify",
            self.lease.fencing_token,
            self.proof if proof is None else proof,
            idempotency_key="verify",
            completed_by_key=self.ids["verify"],
        )
        self.store.enqueue(
            TaskSpec(
                queue="frontier.review",
                task_id="review",
                depends_on=["verify"],
                brief={"candidate_key": "candidate", "verification_task_id": "verify"},
            )
        )
        review = self.store.claim("frontier.review", "synthetic", key_id=self.ids["review"])
        assert review
        self.review_lease = review
        self.approval = {
            k: v
            for k, v in self.proof.items()
            if k not in ["passed", "verification_scope", "artifact"]
        }
        self.approval.update(
            approved=True,
            review_task_id="review",
            reviewer_key_id=self.ids["review"],
            verification_receipt=receipt.receipt_hash,
        )

    def approve(self, approval: dict[str, Any] | None = None) -> None:
        receipt = self.store.submit(
            "review",
            self.review_lease.fencing_token,
            self.approval if approval is None else approval,
            idempotency_key="review",
            completed_by_key=self.ids["review"],
        )
        self.publication = {k: v for k, v in self.approval.items() if k != "approved"}
        self.publication.update(
            schema_version=1, artifact=self.artifact, review_receipt=receipt.receipt_hash
        )

    def publish(self, value: Any = None, *, namespace: str | None = None, version: int = 0) -> Any:
        return self.store.put_record(
            "frontier.catalog",
            self.proof["artifact_sha256"],
            self.publication if value is None else value,
            expected_version=version,
            producer_surface="surface:frontier",
            visibility_scope="public_commons",
            tags=["status:verified"],
            namespace=namespace,
        )


def test_frontier_exact_independent_publication_and_immutable_catalog(tmp_path: Path) -> None:
    p = Pipeline(tmp_path / "proof.db")
    try:
        # The storage gate applies even to trusted in-process/admin writes.
        with pytest.raises(Forbidden):
            p.publish({"forged": True})
        p.verify()
        for receipt in [None, False, {}, ""]:
            with pytest.raises(Forbidden):
                p.approve({**p.approval, "verification_receipt": receipt})
        p.approve()
        for receipt in [None, False, {}, ""]:
            with pytest.raises(Forbidden):
                p.publish({**p.publication, "review_receipt": receipt})
        wrong = copy.deepcopy(p.publication)
        wrong["artifact"]["completion_schema"] = {"type": "string"}
        with pytest.raises(Forbidden):
            p.publish(wrong)
        with pytest.raises(Forbidden):
            p.publish({**p.publication, "extra_secret": "SYNTHETIC forbidden field"})
        with pytest.raises(Forbidden):
            p.publish(namespace="other")
        assert p.publish().version == 1
        with pytest.raises(Forbidden):
            p.publish(version=1)
    finally:
        p.store.close()


def test_frontier_quarantine_cannot_be_widened_or_copied_to_public_resources(
    tmp_path: Path,
) -> None:
    p = Pipeline(tmp_path / "quarantine.db")
    try:
        for visibility, tags in [
            ("public_commons", []),
            ("allowlist_surfaces", ["status:quarantined"]),
            ("surface_only", []),
        ]:
            with pytest.raises(Forbidden):
                p.store.put_record(
                    "frontier.candidates",
                    "candidate",
                    p.candidate,
                    expected_version=1,
                    producer_surface="surface:frontier",
                    visibility_scope=visibility,
                    tags=tags,
                )
        with p.store.scope_context(surface="frontier"):
            assert p.store.get_record("frontier.candidates", "candidate").version == 1
        with pytest.raises(Forbidden):
            p.store.put_record(
                "other",
                "copied",
                p.candidate,
                expected_version=0,
                producer_surface="surface:frontier",
            )
        with pytest.raises(Forbidden):
            p.store.put_blob(b"SYNTHETIC candidate copy", producer_surface="surface:frontier")
        with pytest.raises(Forbidden):
            p.store.publish_feed("copied", p.candidate, producer_surface="surface:frontier")
        with pytest.raises(Forbidden):
            p.store.enqueue(
                TaskSpec(
                    queue="frontier.repair",
                    brief={"synthetic": True},
                    producer_surface="surface:frontier",
                )
            )
    finally:
        p.store.close()


@pytest.mark.parametrize(
    "fault", ["missing_record", "different_candidate", "failed_run", "wrong_owner"]
)
def test_frontier_evidence_must_exist_and_match_verifier(tmp_path: Path, fault: str) -> None:
    p = Pipeline(tmp_path / "evidence.db")
    try:
        evidence = copy.deepcopy(p.evidence)
        evidence["controls_sha256"] = "d" * 64
        if fault == "different_candidate":
            evidence["candidate_key"] = "other"
        if fault == "failed_run":
            evidence["runs"][0]["passed"] = False
        proof = {**p.proof, "evidence_sha256": digest(evidence)}
        if fault != "missing_record":
            p.store.put_record(
                "frontier.verifications",
                "native:" + proof["evidence_sha256"],
                evidence,
                expected_version=0,
                producer_surface="surface:frontier",
                visibility_scope="surface_only",
                tags=["status:quarantined"],
                owner_key_id=p.ids["draft" if fault == "wrong_owner" else "verify"],
            )
        with pytest.raises(Forbidden):
            p.verify(proof)
    finally:
        p.store.close()


@pytest.mark.parametrize(
    "change", ["wrong_hash", "wrong_candidate", "no_full_loop", "spoof_actor", "bad_file"]
)
def test_frontier_verification_rejects_unbound_evidence(tmp_path: Path, change: str) -> None:
    p = Pipeline(tmp_path / "proof.db")
    try:
        proof = copy.deepcopy(p.proof)
        if change == "wrong_hash":
            proof["artifact_sha256"] = "d" * 64
        elif change == "wrong_candidate":
            proof["candidate_hash"] = "d" * 64
        elif change == "no_full_loop":
            proof["verification_scope"] = "schema_fixtures"
        elif change == "spoof_actor":
            proof["verifier_key_id"] = p.ids["draft"]
        else:
            proof["artifact"]["files"][0]["content"] += " changed"
            proof["artifact_sha256"] = digest(proof["artifact"])
        with pytest.raises(Forbidden):
            p.verify(proof)
        assert p.store.task("verify").state == "leased"
        assert not p.store.results("verify")
    finally:
        p.store.close()


@pytest.mark.parametrize("option", ["shared_account", "missing_dependency"])
def test_frontier_requires_distinct_accounts_and_dependency(tmp_path: Path, option: str) -> None:
    p = Pipeline(
        tmp_path / "proof.db",
        shared_account=option == "shared_account",
        dependency=option != "missing_dependency",
    )
    try:
        with pytest.raises(Forbidden):
            p.verify()
    finally:
        p.store.close()


@pytest.mark.parametrize(
    "change", ["wrong_receipt", "different_evidence", "same_reviewer", "failed_verification"]
)
def test_frontier_review_binds_successful_verification(tmp_path: Path, change: str) -> None:
    p = Pipeline(tmp_path / "proof.db")
    try:
        p.verify({**p.proof, "passed": False} if change == "failed_verification" else None)
        approval = copy.deepcopy(p.approval)
        if change == "wrong_receipt":
            approval["verification_receipt"] = "d" * 64
        elif change == "different_evidence":
            approval["evidence_sha256"] = "d" * 64
        elif change == "same_reviewer":
            approval["reviewer_key_id"] = p.ids["draft"]
        with pytest.raises(Forbidden):
            p.approve(approval)
        assert p.store.task("review").state == "leased"
        assert not p.store.results("review")
    finally:
        p.store.close()
