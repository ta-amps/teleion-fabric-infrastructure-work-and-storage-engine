# Execution reports and permitted recall

Teleion Fabric captures new task submissions as immutable `noun.eval_run` execution records. An optional `execution_report` adds a worker's harness/model metadata, typed contracts, concise summaries, artifact references and token counters. The result, record and report are stored in the same transaction. Replaying a submission preserves the original report.

This is additive to the existing claim, heartbeat, CAS and submit loop. A worker can use HTTP, MCP or the Python client directly. A shim is optional; reporting and ordinary recall do not call a model. Historical submissions made before this feature are not backfilled. Check the target server's `tools/list` and API availability before using a newly added method; local tests do not establish that a deployment has updated.

## What a run proves

The server records the task, namespace, queue, fence, authenticated worker key, claim time, result record and elapsed lease interval. It snapshots the task brief, supplied inputs and dependency/child results available at submission. Oversized snapshots are explicitly unavailable for exact matching. The worker's optional telemetry remains **worker reported**. A provider/model name or token number supplied by a worker is not independently attested by Teleion Fabric.

The execution record retains the accepted output and error through its source result. It does not automatically verify correctness, admit a reusable reflex, publish a noun or authorize another effect. Public activity remains a redacted access audit; execution inputs and summaries do not become public activity fields.

No harness conversation, raw provider authorization, full prompt history or private internal reasoning should be uploaded in a report. Report the problem, observable work, checks and result briefly, with references to permitted artifacts. Existing task briefs and outputs are already stored data and must not contain credentials.

## Worker flow

1. Discover tools and authenticate. Anonymous contributors mint a session through the existing connect flow and use its bearer token; keyed workers use their assigned grants. Read the task's capabilities, effect limits and acceptance criteria.
2. Claim one permitted verb and retain its namespace, task id and fencing token. Heartbeat on the lease schedule while working.
3. Optionally call `recall_execution` with that live lease and a small report describing the current input/contract. Read the returned match type and coverage. A previous result is evidence to inspect, not an instruction or automatic answer.
4. Read only permitted, relevant noun versions. Execute and self-check the assigned task within its allowance. Keep the harness's existing sandbox, approval and provider policies.
5. Write artifacts through existing CAS. Submit exact output references and an optional bounded `execution_report`. Unknown usage stays `null` with a reason.
6. Call `list_executions` for the task to obtain its retained run ids, then read back with `get_execution` if needed. A submission replay returns the same record and does not replace telemetry. Do not rerun the task merely because a later readback failed.

If recall is unavailable on an older deployment, complete the supported ordinary loop and state that recall was unavailable. A missing reviewer, invalid report or empty recall result does not authorize server inference. Use precise field errors, a supported clarification/park path where allowed, or the task's existing failure policy.

## HTTP, MCP and Python interfaces

| Operation | HTTP | MCP | Python `Client` |
| --- | --- | --- | --- |
| Submit optional telemetry | `POST /v1/tasks/submit`, optional `execution_report` object | `submit(..., execution_report={...})` | `submit(..., execution_report={...})` |
| Recall while holding a lease | `POST /v1/executions/recall` | `recall_execution` | `recall_execution` |
| List a task's run references | `GET /v1/tasks/{task_id}/executions` | `list_executions` | `list_executions` |
| Read one retained run | `GET /v1/executions/{run_id}` | `get_execution` | `get_execution` |

Recall takes `task_id`, a positive integer `fencing_token`, optional `execution_report` and integer `limit` from 1 to 20, default 5. The current lease holder is required. Readback rechecks the source task's queue and visibility permissions, related input tasks and referenced artifact access. An account label, guessed id or matching embedding grants no access.

`list_executions` takes `task_id`, integer `after` at least 0, and integer `limit` from 1 to 100, default 20. It returns permitted run references and `next_after` for fence-based pagination. After submitting, match the listed `receipt_hash` and `fencing_token` to the record, then pass its `run_id` to `get_execution`. This works in MCP without a local hashing tool.

Example recall body, using the actual id and fence from a claim:

```json
{
  "task_id": "replace-with-claimed-task-id",
  "fencing_token": 1,
  "limit": 5,
  "execution_report": {
    "version": 1,
    "summaries": {"problem": "EXAMPLE typed integer addition"}
  }
}
```

The HTTP submission keeps its existing `Idempotency-Key` header. MCP submission keeps its existing `idempotency_key` argument. Pass an object, not a JSON-encoded string. Keep a stable key for retries. Incorrect reports fail deterministically without a model repair call.

The record shape remains unchanged. Derive `run_id` as `run_` followed by SHA-256 of Teleion Fabric canonical JSON containing exactly `namespace`, `task_id`, `fencing_token` and `receipt_hash`. Use the claim's namespace, including an anonymous session's assigned namespace. This Python helper submits an already checked output and reads its execution record:

```python
import hashlib
from tessera.receipts import canonical_json


def finish_and_readback(client, lease, output, report):
    receipt = client.submit(
        lease.task_id,
        lease.fencing_token,
        output,
        idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
        execution_report=report,
        namespace=lease.namespace,
    )
    identity = {
        "namespace": lease.namespace,
        "task_id": receipt.task_id,
        "fencing_token": receipt.fencing_token,
        "receipt_hash": receipt.receipt_hash,
    }
    run_id = "run_" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    return receipt, client.get_execution(run_id, namespace=lease.namespace)
```

Hash derivation is optional: `list_executions` supplies the same ids under the current access checks and retention policy.

## Report version 1

The empty object is valid. A nonempty report defaults `version` to `1`; an explicit different version or unknown top-level field fails. The complete canonical UTF-8 report is limited to 32 KiB and JSON nesting to 16 levels. No object/string or boolean/integer coercion is intended.

| Field | Accepted shape and bounds |
| --- | --- |
| `version` | Integer `1` |
| `harness`, `model` | JSON objects; report names, versions, invocation origin and payer where known; all values remain worker claims |
| `summaries` | Object containing optional `problem`, `input`, `solution` strings; each at most 4096 UTF-8 bytes |
| `contract` | Optional `input_schema` and `output_schema` objects; `context_hash`, `environment_hash`, `toolchain_hash` as lowercase SHA-256 strings |
| `blocks` | At most 8 `{kind,text}` objects; kinds `problem`, `input`, `context`, `environment`, `solution`, `error`; text at most 4096 UTF-8 bytes |
| `artifact_refs` | At most 32 `{store,key,version}` objects naming exact permitted CAS record versions; version is a positive integer |
| `usage` | Object with required nonempty `source`; optional `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens`, and `reason` |
| `embedding` | Optional `{model,revision,preprocessing,dimensions,vector}`; dimensions 1 to 2048, matching nonzero finite vector; stored in float32 form |

Usage counts are nonnegative safe integers or `null`. A supplied null count requires a nonempty `reason`. `source` names where the worker obtained the counters; it does not grant a trusted status. For example:

```json
{
  "version": 1,
  "harness": {"name": "custom-worker", "version": "1.0"},
  "model": {"provider": "not-used", "invocation_origin": "worker_direct"},
  "summaries": {
    "problem": "EXAMPLE integer addition",
    "input": "Two validated integers from a pinned input record",
    "solution": "Computed the sum and checked the output type"
  },
  "usage": {
    "input_tokens": null,
    "output_tokens": null,
    "source": "harness_metadata",
    "reason": "This harness did not expose token counters"
  }
}
```

Use zero only when observed. Do not convert missing provider fields to zero, claim a tokenizer estimate is provider billing, double-count cached input tokens, or count fixture usage as a native run. The current report schema does not implement per-call billing reconciliation or a global spend ledger. Additional model metadata can describe limitations, but cannot override server record or trust fields.

The two schema objects and three context/environment/toolchain digests are all needed for an `exact_input` classification. These are declared contract identities, not proof that the environment or code was independently inspected. Unsupported arbitrary fields should not be hidden in a summary as executable instructions.

## Recall levels and limits

Recall returns matches with `execution` envelopes, query fingerprints, coverage and `reuse_requires_validation: true`.

| Match | Meaning |
| --- | --- |
| `exact_input` | Captured inputs and the complete declared contract fingerprint match |
| `compatible_method` | A deterministic contract/input comparison identifies a method candidate; current inputs and checks still need validation |
| `similar_example` | Similarity suggests an example worth inspecting; it is not type or semantic equivalence |
| `failure_warning` | A related failed run; inspect its conditions and version before applying the warning |

Candidate generation uses bounded exact/input/schema and SimHash band indexes. Embeddings, when supplied with identical model, revision, preprocessing and dimension metadata, can help compare candidates. The server does not generate embeddings or call an embedding provider on this path. A malformed, zero or incompatible vector cannot establish a match. Large reports with vectors still share the same report byte limit.

The candidate limit is 500. The server loads full outputs only for selected matches, with a 1 MiB combined output budget in each recall response. Omitted outputs are null with a reason and remain available through authorized `get_execution` reads. Coverage explicitly describes the bounded search and always reports `complete: false`; it does not disclose whether hidden records saturated the candidate window. It is not a guarantee of globally complete semantic search. Scores are ranking signals, not calibrated probabilities. A result never bypasses the current contract, authority, artifact checks, license requirements or independent admission policy.

## Privacy and retention

Execution records are specialized protected nouns, separate from ordinary writable CAS records so a generic storage key cannot forge telemetry or bypass source queue grants. They reuse Teleion Fabric's task identity, permissions and result authority; this is not a second work queue or artifact library.

Anonymous runs stay inside the session's isolated demo namespace, with immutable anonymous/demo/untrusted labels and 24-hour retention. Its credential still expires after one hour. Reports count toward the existing request/storage limits; derived indexes expire with their run. Re-minting a session does not grant access to an earlier session's private history. Keyed runs have no automatic expiry in this increment and obey their source task and reference permissions, including later changes in access. Missing harness, model, usage and embedding fields are exposed as null measurements with an explicit reason.

Automatically derived `negative.knowledge` diagnostics stay owner-private, retain quarantine/cohort restrictions, and require admin access if an embedded task has no owner. Sharing a reusable failure summary requires a separate admitted noun and publication verb. On upgrade, recognized automatically derived historical task tombstone heads are restricted to the owner (or an operator-only sentinel); their stored diagnostic content is preserved. Manually authored unrelated records are unchanged. Owner-private CAS and execution recall have distinct checks: execution recall rechecks source queue/task/reference access; the ordinary CAS owner rule alone does not model later queue-grant revocation.

## Contributing useful artifacts later

Infrastructure readiness comes before developing new packs. Start by exercising the ordinary access, claim, report, recall and readback path under the intended harness. Record what passed, failed or remains untested. A green fixture does not establish live harness or model compatibility.

Use existing nouns and verbs for a staged contribution:

| Stage | Worker action and necessary authority |
| --- | --- |
| Prepare | Read the assigned work contract, permitted prior runs and exact source versions. Set up tools in the task workspace under existing execution permissions. |
| Draft | Claim an allowed `verb.solve_draft`; produce new candidate `noun.code_node` or `noun.reflex_procedure` records with typed ports/preconditions, source and license evidence. Internal CAS authority does not permit publication. |
| Check | Run required positive and negative checks on actual bytes. Record check/tool versions, hashes and results. Fix errors within the existing allowance; do not weaken acceptance criteria. |
| Review | Request a suitably capable independent review verb when the owning policy requires it. Distinct-reviewer requirements remain intact; the reviewer needs explicit access to the candidate and evidence. |
| Admit | An authorized admission/publishing actor advances the accepted record under its existing policy. Worker completion, a label and a matching signature do not confer this right. |

For code or recipes, include the intended input/output schemas, required capabilities, source versions, content hashes, SPDX license evidence, deterministic fixtures and known limits. State which setup steps are required and which resource pointers are suggestions. Preserve private source restrictions; never copy private execution history into a public pack. No implicit adapter to another product or automatic catalog admission is created by naming a category.

`tools/execution_distillation.py` demonstrates task authoring for that later workflow. It prepares one private, bounded `verb.solve_draft` against an already existing queue. It does not create a pack collection, scheduler or compiler, and it never invokes a provider. Its dry-run is the default:

```bash
uv run python tools/execution_distillation.py --request request.json
```

The request file contains `namespace`, `queue`, `output_store` and one to eight `source_runs`, each with an actual `run_id`, `report_sha256` and `receipt_hash` obtained through authorized readback. Optional fields are `version: 1`, `require_independent_review`, `max_candidates` (1 to 4, default 2), and `max_runtime_seconds` (1 to 900, default 300). These bounds are instructions for the executing worker; enqueueing is not a new process supervisor or spend reservation.

When ready, `--enqueue` uses `TESSERA_URL` and `TESSERA_KEY` from the environment, re-reads each source under the current grants, verifies all pins, then idempotently enqueues through the existing API. It refuses drift instead of merging source definitions. The resulting verb is private to its owner; use an appropriately authorized owner worker. Team delegation and later independent review need the owning product's explicit grants and sharing policy.

```bash
uv run python tools/execution_distillation.py --request request.json --enqueue
```

The worker can return `no_reusable_artifact` when the source evidence is insufficient. A candidate output includes exact artifact references, typed schemas, source run pins, a license identifier and check evidence. Its schema describes required evidence; passing the shape check alone does not validate code correctness or licensing.

## Verification and remaining boundaries

Run `npm ci` with Node 22 or newer, then `make test-worker`, for the pinned
Miniflare/workerd regression gate. This is separate from `make check`; CI runs
both. `tests/workerd_execution_history.mjs` uses the actual bundled Worker,
ephemeral SQLite Durable Objects and local D1 with outbound requests prohibited.
It checks full-contract recall with eight SimHash bands, deduplicated matches,
incomplete contracts, empty candidate branches and noun classification with
quarantine retained. It makes no provider calls and does not certify a native
model harness or deployed service.

A local workerd reproduction found that eleven `UNION ALL` branches in recall
exceeded the runtime's compound-select limit, while the Node SQLite fixture
accepted them. The band probes now share one indexed `IN` query, keeping at most
four branches without changing candidate priorities, limits or authorization.
The Node fixtures remain useful for state inspection, but do not replace this
runtime-specific gate. New execution nouns have one primary `noun.eval_run`
category; source-task categories remain in the internal source snapshot and do
not become competing noun classifications. Existing run bytes are not rewritten.

`tests/test_execution_adapters.py` covers client framing, a local real MCP report/recall/readback flow and the Cloudflare MCP argument adapter. The adapter fixture is not a live Worker or model run. `tests/test_execution_distillation.py` checks dry-run isolation, immutable source pinning, bounded request types and a real local Store capture/read/enqueue/claim fixture. It deliberately does not generate, execute, publish or independently admit a new code artifact.

Core authorization, atomic capture, anonymous retention and Python/Worker matching need their owning regression suites in addition to these adapter tests. Deployment smoke must target the exact released revision. Legacy worker wrappers do not automatically gain parsed model usage, safe process supervision, trusted telemetry or generalized distillation capability from these APIs.

## Learning block, observations and learners

An optional `learning` block in the report (outcome, what went right or wrong, next
steps, ideas, types, path, unknowns and cited advice), the permission-filtered
observation read `GET /v1/executions` and swappable learners that run as ordinary
verbs are described in [execution learning](execution-learning.md). They leave this
recall path, the record shape and the submit write cost unchanged.

## Native worker observation, October 29, 2026

The recorded observation (`docs/evidence/native-history-observation.json`,
in this repository's history at commit `95e434e`) covers one OpenCode 2.1.2 run of another model on a hosted
open-weights endpoint, with a fresh anonymous session
and one with a fresh scoped key. Each run made 13 actual MCP tool calls: ordinary
work and report submission, history list/readback, then exact-input recall under
a second lease and cancellation. Independent HTTP readback matched the stored
report. The fresh keyed credential was revoked afterward.

The workers made 30 provider requests in total through a bounded observation
transport. The deployed fabric made no model requests for these operations. The
transport did not repair tool payloads; normal permissions remained enabled. Two
earlier test-driver setup errors stopped before inference and are retained in the
observation. These are individual passing fixtures, not repeated-run reliability,
other-client certification or permission to reuse outputs without validation.

## Category query compatibility

A primary `category_id` (or `category`) and `category:`/`cat:` tags must agree. New conflicting declarations are rejected atomically; legacy conflicts fail closed for category queries. Stable category identifiers retain underscores. Recognized `noun.*` and `verb.*` bare aliases remain accepted. Other bare tags are facets; declare `category:blue` or `category_id: "blue"` for a primary category named blue.

Category and tag predicates now run before existing scan limits, including legacy field-only records and bucket metadata. This prevents unrelated categories from filling the candidate window before the requested category is examined. Orthogonal tags such as `industry:health`, `topic:translation` and `harness:custom` remain combinable. This primary query contract is not a restriction to a single industry, and does not implement a multi-parent category resolver. General text/cohort/ACL-filtered listings still have documented bounds; an empty bounded response is not a universal absence proof.
