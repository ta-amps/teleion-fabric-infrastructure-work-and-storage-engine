# Worker-first execution and private work

Architecture direction adopted October 28, 2026. This policy describes required design and migration checks. It does not certify that every current endpoint or worker already implements them.

Agentic work should normally be a **verb that an authorized worker claims**. A noun holds versioned input, context, an artifact or evidence. Workers reason, act, check and repair. The server coordinates identity, discovery, leases, storage, validation and receipts without autonomous model calls on the normal path.

Private, management and infrastructure work can use the same pattern through private workers. A worker can run in a customer harness, an operator environment or an approved cloud service. Its location does not determine its permissions, model provider or payer. Moving work to an operator-owned worker does not make its inference or hosting free.

## Policy rules

1. Represent independently schedulable agentic outcomes as verbs. Reuse existing queue, record and execution contracts. Do not create another runtime, queue authority or policy registry.
2. Keep authentication, authorization, schema checks, lease maintenance, ordinary search, status reads and receipt validation deterministic. Do not turn every database read, helper function, render frame or arithmetic operation into another verb or model call.
3. Workers use permitted tools and existing code first, run the task's checks and repair their own failures. Pin acceptance criteria independently of the proposed result; a producer cannot silently weaken its own check to pass.
4. Additional judgment becomes a review verb for a suitably capable agent. Review only when the task, evidence or applicable admission policy requires it. Do not add a reviewer to every successful action or recursively review every review.
5. Privacy is enforced by authenticated identity, ownership, scope and effect authorization. A label such as `private` is descriptive metadata, never an access grant.
6. Missing capacity, an invalid payload or an empty review queue does not authorize server inference. Return an actionable error, keep work pending, or park it with one concrete question.
7. Direct server-initiated inference is a last resort or an explicitly authorized server capability. It needs a recorded purpose, allowance, data scope, provider policy, deadline, retry policy and outcome. Existing authority still controls effects and spending.
8. Measure the actual origin, payer, location and outcome of work. A queued wrapper around a server call does not erase its cost or establish that an external worker performed it.

## One ordinary work cycle

```text
Discover permitted work and its small contract
  -> Claim a bounded verb with a current lease
  -> Load only permitted, relevant noun versions
  -> Execute, self-check and repair within the allowance
  -> Submit exact artifact references and check evidence
  -> Deterministic acceptance checks and durable receipt
  -> Finish, or enqueue a policy-required review or follow-up verb
```

A verb brief identifies the intended outcome, input references, output contract, acceptance checks, required capabilities, authorized effects, dependencies, deadline and resource ceiling. Use the fields already defined by the owning API. This list is a design checklist, not a new wire schema.

Keep artifact bytes in the owning store and return compact references with versions or digests. A hash establishes byte identity, not usefulness or truthful execution. Distinguish worker-reported checks, independently reproduced results and public acceptance. Task completion does not automatically publish a noun or grant permission for an external effect.

Split work when a subtask has a useful independent outcome, different permissions, separate equipment or a meaningful recovery boundary. Batch deterministic operations inside the owning verb when that saves coordination cost. A worker may propose a dependency graph; the server validates permissions, limits and supported contracts before accepting it.

## Private workers and private verbs

Use the same verb lifecycle with narrower access, rather than a separate private reasoning framework:

| Work class | Eligible claimant | Required boundary |
| --- | --- | --- |
| Public-safe contribution | An admitted worker permitted for that queue | Only explicitly publishable input and declared effects |
| Customer-private project | A worker authorized by that customer in that product | Customer ownership, isolated storage and permitted model/data destinations |
| Operator-private management | A worker issued the required operator role and task scope | Private queue, minimal evidence access and separate action authority |
| Restricted infrastructure execution | An approved worker in the required environment | Lease-bound capability, approved destination and existing change controls |

Filter discovery, claims, noun reads, historical versions, blobs, events, search, logs and result delivery consistently. Do not reveal private task titles or arguments in public queue statistics. Dependency children and follow-up review tasks inherit restrictions; a split or retry cannot widen access. Check current authority again before protected reads and effects, not only when registering a worker. Loss of scope or lease stops further access and effects.

Keep secrets in the existing secret store or execution environment. A task may name an authorized secret reference, not carry a provider key in its brief, result or log. Resolve the reference only for the allowed worker, action and destination. A common infrastructure operator does not authorize sharing customer identities, billing, credentials or private histories across products.

For example: a deterministic alert enqueues a private incident diagnosis. An operations worker reads only the approved logs, runs checks and writes a proposed repair. An authorized execution worker applies the permitted change, then verifies and records the result. Diagnosis does not grant production write authority. A public status summary is a separate deliberate projection of approved fields.

## Keep reasoning and review economical

Start with a small contract card, useful examples and the relevant prior result. Retrieve larger source or context only when needed. Prefer an executable check or existing implementation over another natural-language explanation. Reuse unchanged accepted artifacts and applicable evidence under their recorded versions and validity conditions.

For ordinary review escalation, request one capable reviewer with the exact artifact, criteria, relevant evidence and disputed question. Independent admission rules may require additional evidence or distinct model families; satisfy those rules at the applicable boundary without imposing them on all intermediate work. Select reviewers by relevant demonstrated capability and permissions, not a self-asserted label or expensive model name alone.

A malformed tool payload should receive a precise field error and the expected shape. Do not silently stringify objects, substitute canned data or ask the server model to repair routine types. A missing input becomes a typed clarification or parked verb. A recurring failure can create a bounded diagnostic or contract-improvement verb; it must not create an endless chain of new tasks.

Use backpressure, bounded concurrency and queue age to plan capacity. Prefer event-driven notification or supported long-polling; otherwise back off with jitter and honor server retry guidance. Heartbeat according to the lease, not by calling a model. Avoid constant empty polling and hot retry loops. Queueing reduces request-path inference; it does not remove storage and request charges.

## Server inference exceptions

Try an authorized private or managed worker before direct server inference when the work can wait. Legitimate exceptions can include an explicitly offered interactive server capability, a narrowly scoped infrastructure diagnosis, or recovery when the normal work fabric is unavailable. Privacy alone is not an exception: a private verb may be sufficient. Urgency alone grants no extra authority.

Before an exception call, record why the normal worker path is unsuitable, the responsible principal, data and effect scope, model/provider, payer, call/token/cost limits and deadline. Reserve allowance before the call. Use a stable request identity, persist the result, and reconcile an uncertain outcome before retrying. A provider timeout is not proof that no usage occurred. Stop or park when the allowance is exhausted.

An explicitly requested model proxy is different from an autonomous server decision to call a model. Preserve the proxy's existing permissions and caller funding. Track worker-direct inference, worker-requested proxy inference, operator-worker inference and direct server fallback separately. These are reporting categories to map onto existing usage records, not new public API fields.

A server-owned worker should claim an ordinary scoped verb and retain the same receipt and budget controls. It must not become an unbounded scheduler that spends on behalf of every waiting task. If the queue itself is unavailable, use the existing authorized incident procedure, keep an independent incident record and reconcile it after recovery.

## Qualification and honest coverage

The target is nearly all eligible agentic work through claimable verbs, not a claim of measured 100 percent coverage. Fix the unit of work before measuring it. Report eligible work received, claimed, completed, failed, cancelled and pending; direct server exceptions; time waiting; self-repair attempts; review frequency; and provider/hosting usage. Keep operator-managed workers visible rather than counting them as free donated work. Do not inflate coverage by splitting one result into thousands of trivial verbs or excluding failures and private jobs.

For each workflow migration, demonstrate:

- A normal claim, execution, self-check and completion makes zero autonomous server model calls.
- Invalid types, stale fences, denied access and failed checks return errors without spending on a server model.
- An unqualified or unauthorized worker cannot discover or claim private work, including through history, feeds or derived artifacts.
- A missing reviewer leaves the task pending; an eligible review worker can complete the applicable gate.
- Retrying identical work does not repeat committed external effects or duplicate paid inference; uncertain outcomes remain explicit.
- Changed artifact or contract versions invalidate affected acceptance evidence.
- The exceptional server path refuses missing authority or allowance and records its complete usage when enabled.

Run these checks against the exact implementation and environment. Documentation, a signed receipt, a successful HTTP response and a green unrelated test suite are not substitutes for the corresponding behavioral proof.

## Design references

Cloudflare documents HTTP pull consumers for workers outside its edge runtime and at-least-once queue delivery. Those support separating queue coordination from worker placement and require application-level deduplication. These are reference patterns, not a requirement to replace the existing queue backend. [Pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/), [delivery guarantees](https://developers.cloudflare.com/queues/reference/delivery-guarantees/), checked October 28, 2026.

Temporal similarly separates durable activity lifecycle coordination from tasks executed by workers. Its model is a useful comparison, not an adopted dependency or an additional execution runtime here. [Activity execution](https://docs.temporal.io/activity-execution), checked October 28, 2026.

## Application in this repository

Use the existing namespace, principal grants, governance envelope, task leases, CAS and receipts. This adds no second task store. Private work needs ownership and visibility checks on every access path; the presence of a category or private tag is not authorization.

The policy preserves this product's existing scope, public identity and access rules. Unimplemented routing, private worker provisioning and exception enforcement remain engineering work until their owning checks pass.

See [work examples and a private incident walkthrough](WORKER-VERB-EXAMPLES.md) for concrete applications.
