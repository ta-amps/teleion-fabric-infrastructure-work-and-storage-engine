// Completed queue evidence is checked in the same transaction as promotion.
import { sha256Hex } from "./ontology_engine.js";
import { canonicalJson } from "./cloud_buckets.js";
export function reviewProof(c, taskId, actor, output, deny) {
  if (!output || output.approved !== true) return;
  const dep = c._one("SELECT r.* FROM task_deps d JOIN tasks t ON t.namespace=d.namespace AND t.id=d.depends_on JOIN results r ON r.namespace=t.namespace AND r.task_id=t.id WHERE d.namespace=? AND d.task_id=? AND t.id=? AND t.queue='eq02' AND r.ok=1 AND r.state_after='done' ORDER BY r.created_at DESC LIMIT 1", c.namespace, taskId, output.draft_task_id || "");
  if (!dep) throw deny("review must name its successfully completed draft dependency");
  const draft = JSON.parse(dep.output);
  const drafter = dep.completed_by_key;
  if (!drafter || drafter === actor) throw deny("an independent authenticated reviewer is required");
  if (c._one("SELECT principal_kind FROM access_principals WHERE key_id=?", actor)?.principal_kind === "anonymous") throw deny("an anonymous session never approves; a keyed reviewer or a person must");
  if (output.review_task_id !== taskId || output.drafter_key_id !== drafter || output.reviewer_key_id !== actor || output.draft_sha256 !== draft.blob_sha256 || output.kit_id !== draft.draft?.kit_id || draft.verification?.passed !== true) throw deny("review approval does not match its authenticated draft evidence");
}
export function publicationProof(c, store, value, deny) {
  if (!["eranos.catalog", "eranos.index"].includes(store)) return;
  if (!value || typeof value !== "object") throw deny("catalog publication requires review evidence");
  const result = c._one("SELECT r.* FROM results r JOIN tasks t ON t.namespace=r.namespace AND t.id=r.task_id WHERE r.namespace=? AND r.task_id=? AND r.receipt_hash=? AND t.queue='eq03' AND r.ok=1 AND r.state_after='done'", c.namespace, value.review_task_id || "", value.review_receipt || "");
  if (!result) throw deny("a completed independent review receipt is required");
  const proof = JSON.parse(result.output), actor = result.completed_by_key;
  if (!actor || proof.approved !== true) throw deny("review receipt is not an authenticated approval");
  reviewProof(c, result.task_id, actor, proof, deny);
  for (const key of ["draft_task_id", "draft_sha256", "drafter_key_id", "reviewer_key_id", "publication_sha256"]) if (value[key] !== proof[key]) throw deny("publication does not match the approved artifact");
  if (store === "eranos.catalog") {
    const doc = { ...value, review_receipt: null, catalog_version: 1 };
    delete doc.publication_sha256;
    if (sha256Hex(canonicalJson(doc)) !== proof.publication_sha256 || value.kit?.kit_id !== proof.kit_id) throw deny("publication content digest does not match review");
  }
}
