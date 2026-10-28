// Publication is checked against immutable, actor-bound queue results inside SQL transactions.
import { sha256Hex } from "./ontology_engine.js";
import { canonicalJson } from "./cloud_buckets.js";

const HASH = /^[a-f0-9]{64}$/;
const PUBLIC_FIELDS = ["schema_version", "artifact", "artifact_sha256", "candidate_key", "candidate_version", "candidate_hash", "repair_task_id", "verification_task_id", "verification_receipt", "evidence_sha256", "review_task_id", "review_receipt", "drafter_key_id", "verifier_key_id", "reviewer_key_id"];
const BOUND_FIELDS = ["candidate_key", "candidate_version", "candidate_hash", "repair_task_id", "artifact_sha256", "evidence_sha256"];
const same = (a, b) => a !== undefined && b !== undefined && canonicalJson(a) === canonicalJson(b);
const object = value => value && typeof value === "object" && !Array.isArray(value);

function completed(c, id, queue, deny, receipt = null) {
  const row = c._one("SELECT r.*,t.brief FROM results r JOIN tasks t ON t.namespace=r.namespace AND t.id=r.task_id WHERE r.namespace=? AND r.task_id=? AND t.queue=? AND t.state='done' AND r.ok=1 AND r.state_after='done' ORDER BY r.created_at DESC LIMIT 1", c.namespace, typeof id === "string" ? id : "", queue);
  if (!row || !row.completed_by_key || (receipt !== null && row.receipt_hash !== receipt)) throw deny("completed actor-bound stage receipt required");
  const output = JSON.parse(row.output);
  if (!object(output)) throw deny("completed stage must contain structured evidence");
  return { row, output, brief: JSON.parse(row.brief) };
}
function dependency(c, task, parent, deny) {
  if (!c._one("SELECT 1 FROM task_deps WHERE namespace=? AND task_id=? AND depends_on=?", c.namespace, task, parent)) throw deny("stage must depend on the referenced completed task");
}
function independent(c, actors, deny) {
  const accounts = actors.map(actor => {
    if (typeof actor !== "string" || !actor) throw deny("account-tracked stage principals required");
    const row = c._one("SELECT account_id,principal_kind FROM access_principals WHERE key_id=?", actor);
    if (!row?.account_id || row.principal_kind === "anonymous") throw deny("account-tracked stage principals required");
    return row.account_id;
  });
  if (new Set(actors).size !== actors.length || new Set(accounts).size !== accounts.length) throw deny("draft, verification and review require distinct keys and accounts");
}
function candidateArtifact(c, proof, deny) {
  if (proof.candidate_version !== 1 || typeof proof.candidate_key !== "string" || !HASH.test(proof.candidate_hash || "") || !HASH.test(proof.artifact_sha256 || "") || !HASH.test(proof.evidence_sha256 || "")) throw deny("versioned artifact and evidence digests required");
  const record = c._one("SELECT value FROM records WHERE namespace=? AND store='frontier.candidates' AND key=? AND version=?", c.namespace, proof.candidate_key, proof.candidate_version);
  if (!record) throw deny("candidate version not found in this namespace");
  const candidate = JSON.parse(record.value), artifact = proof.artifact;
  if (!object(candidate) || !object(artifact) || artifact.schema_version !== 1 || artifact.connector !== "tessera-typed-work-v1" ||
      !same(Object.keys(artifact).sort(), ["schema_version", "connector", "source_sha256", "target", "completion_schema", "files", "license"].sort()) ||
      candidate.receipt_hash !== proof.candidate_hash || candidate.status !== "quarantined" || candidate.surface !== "tessera" ||
      artifact.source_sha256 !== candidate.source_sha256 || artifact.license !== "Apache-2.0" || candidate.license !== artifact.license ||
      !same(artifact.target, { tool: candidate.tool, field: candidate.field, input_schema: candidate.input_schema }) ||
      !same(artifact.files, candidate.files) || !object(artifact.completion_schema) || sha256Hex(canonicalJson(artifact)) !== proof.artifact_sha256) throw deny("artifact differs from its versioned candidate");
  if (!Array.isArray(artifact.files) || artifact.files.length !== 4 ||
      artifact.files.some(f => !object(f) || typeof f.path !== "string") ||
      !same(artifact.files.map(f => f.path).sort(), ["SKILL.md", "contract.json", "fixtures.json", "validate.mjs"]) ||
      artifact.files.some(f => typeof f.content !== "string" || f.content.length > 65536 || sha256Hex(f.content) !== f.sha256)) throw deny("candidate file integrity check failed");
}

function evidence(c, actor, proof, deny) {
  const row = c._one("SELECT * FROM records WHERE namespace=? AND store='frontier.verifications' AND key=? AND version=1", c.namespace, "native:" + proof.evidence_sha256);
  if (!row || row.owner_key_id !== actor || row.producer_surface !== "surface:frontier" || !["private", "surface_only"].includes(row.visibility_scope) || !JSON.parse(row.tags).includes("status:quarantined")) throw deny("versioned verifier-owned evidence noun required");
  const value = JSON.parse(row.value);
  if (!object(value) || !same(Object.keys(value).sort(), ["schema_version", "verification_scope", "candidate_key", "candidate_hash", "artifact_sha256", "runs", "controls_sha256"].sort()) || value.schema_version !== 1 ||
      ["verification_scope", "candidate_key", "candidate_hash", "artifact_sha256"].some(k => value[k] !== proof[k]) || sha256Hex(canonicalJson(value)) !== proof.evidence_sha256 ||
      typeof value.controls_sha256 !== "string" || !HASH.test(value.controls_sha256) || !Array.isArray(value.runs) || !value.runs.length || value.runs.length > 32) throw deny("evidence identity or digest mismatch");
  for (const run of value.runs) {
    if (!object(run) || !same(Object.keys(run).sort(), ["harness", "provider", "model", "principal_kind", "passed", "receipt_sha256"].sort()) || run.passed !== true || !["anonymous", "keyed"].includes(run.principal_kind) ||
        ["harness", "provider", "model"].some(k => typeof run[k] !== "string" || !run[k].length || run[k].length > 128) || typeof run.receipt_sha256 !== "string" || !HASH.test(run.receipt_sha256)) throw deny("native run evidence must contain a passing bounded manifest");
  }
}

export function frontierVerificationProof(c, taskId, actor, proof, deny) {
  if (!object(proof) || typeof proof.passed !== "boolean") throw deny("verification must declare a boolean passed outcome");
  if (!proof.passed) return;
  if (proof.verification_scope !== "native_full_loop" || proof.verification_task_id !== taskId) throw deny("native full-loop evidence required");
  const draft = completed(c, proof.repair_task_id, "frontier.repair", deny);
  dependency(c, taskId, proof.repair_task_id, deny);
  const task = c._one("SELECT brief FROM tasks WHERE namespace=? AND id=?", c.namespace, taskId);
  const brief = JSON.parse(task.brief);
  if (brief.candidate_key !== proof.candidate_key || brief.repair_task_id !== proof.repair_task_id ||
      draft.output.candidate_key !== proof.candidate_key || draft.output.candidate_hash !== proof.candidate_hash ||
      proof.drafter_key_id !== draft.row.completed_by_key || proof.verifier_key_id !== actor) throw deny("verification does not match authenticated repair evidence");
  independent(c, [draft.row.completed_by_key, actor], deny);
  candidateArtifact(c, proof, deny);
  evidence(c, actor, proof, deny);
}

export function frontierReviewProof(c, taskId, actor, proof, deny) {
  if (!object(proof) || typeof proof.approved !== "boolean") throw deny("review must declare a boolean approved outcome");
  if (!proof.approved) return;
  if (typeof proof.verification_receipt !== "string" || !HASH.test(proof.verification_receipt)) throw deny("exact verification receipt required");
  if (proof.review_task_id !== taskId || proof.reviewer_key_id !== actor) throw deny("review actor does not match its authenticated principal");
  const verified = completed(c, proof.verification_task_id, "frontier.verify", deny, proof.verification_receipt);
  dependency(c, taskId, proof.verification_task_id, deny);
  const task = c._one("SELECT brief FROM tasks WHERE namespace=? AND id=?", c.namespace, taskId), brief = JSON.parse(task.brief);
  if (brief.candidate_key !== proof.candidate_key || brief.verification_task_id !== proof.verification_task_id || verified.output.passed !== true ||
      BOUND_FIELDS.some(field => proof[field] !== verified.output[field]) || proof.drafter_key_id !== verified.output.drafter_key_id || proof.verifier_key_id !== verified.row.completed_by_key) throw deny("review does not match completed verification evidence");
  frontierVerificationProof(c, verified.row.task_id, verified.row.completed_by_key, verified.output, deny);
  independent(c, [proof.drafter_key_id, proof.verifier_key_id, actor], deny);
}

export function frontierPublicationProof(c, write, deny) {
  if (write.store !== "frontier.catalog") {
    if (write.store.startsWith("frontier.")) {
      if (write.producer_surface !== "surface:frontier" || !["private", "surface_only"].includes(write.visibility_scope)) throw deny("Frontier staging stores remain nonpublic");
      if (["frontier.candidates", "frontier.verifications", "frontier.inputs", "frontier.reasoning_requests", "frontier.reasoning_receipts"].includes(write.store) && !write.tags?.includes("status:quarantined")) throw deny("Frontier evidence and candidates retain quarantine");
    }
    return;
  }
  const value = write.value;
  if (!object(value) || !same(Object.keys(value).sort(), [...PUBLIC_FIELDS].sort()) || value.schema_version !== 1 ||
      write.expected_version !== 0 || write.key !== value.artifact_sha256 || write.producer_surface !== "surface:frontier" ||
      write.visibility_scope !== "public_commons" || !write.tags?.includes("status:verified") || write.tags.includes("status:quarantined") ||
      typeof value.review_receipt !== "string" || !HASH.test(value.review_receipt)) throw deny("catalog writes require an immutable reviewed public artifact");
  const reviewed = completed(c, value.review_task_id, "frontier.review", deny, value.review_receipt);
  if (reviewed.output.approved !== true) throw deny("independent approval required");
  frontierReviewProof(c, reviewed.row.task_id, reviewed.row.completed_by_key, reviewed.output, deny);
  for (const field of [...BOUND_FIELDS, "verification_task_id", "verification_receipt", "drafter_key_id", "verifier_key_id", "reviewer_key_id"]) if (value[field] !== reviewed.output[field]) throw deny("catalog fields differ from approved review");
  const verified = completed(c, value.verification_task_id, "frontier.verify", deny, value.verification_receipt);
  if (!same(value.artifact, verified.output.artifact)) throw deny("published bytes differ from verified artifact");
}
