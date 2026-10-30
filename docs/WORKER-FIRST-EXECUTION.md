# Worker-first execution and private work

Architecture direction adopted October 28, 2026, clarified October 29. This policy describes required design and migration checks. It does not certify that every current endpoint or worker already implements them.

Independently schedulable agentic work must be a **verb that an authorized worker claims**, unless an explicit server exception applies. A noun holds versioned input, context, code, a tool, a model, a recipe, an artifact or evidence. Workers reason, act, check and repair. The server coordinates identity, discovery, leases, storage, validation and receipts without autonomous model calls on the normal path.

## Required boundaries: nouns, verbs, surfaces and infrastructure

The architecture has **nouns, verbs, surfaces and narrow supporting
infrastructure**. Seed packs contain nouns and verbs. These are mandatory design
boundaries, not suggestions to replace with a separate service for each domain.

Today there are exactly two public surfaces: Eranos, the two-sided marketplace,
and one overview site. Tessera is infrastructure, together with a connector that
gives agents context and models. Everything else is nouns and verbs in seed
packs, including agent kits generated for an NGO's agent.

| Boundary | Responsibility | What it must not become |
| --- | --- | --- |
| Nouns | Scoped, versioned knowledge, source observations, tools/code, prompts, profiles, models, data, recipes, configuration, plans, artifacts and evidence | An access grant, execution authority or quality certificate merely because it is stored or hashed |
| Verbs | Independently schedulable work with input references, acceptance checks, authority and recorded outcomes; authorized workers perform ordinary reasoning | A second queue or an unrecorded server agent |
| Surfaces | Websites, apps, dashboards, CLI and HTTP/MCP interfaces for discovering and using nouns and verbs | A separate permission system, autonomous domain brain or work authority |
| Infrastructure | Narrow, explicit identity, authorization, storage, versions, leases, CAS, receipts, transport, health, backpressure and essential management plumbing | A label that exempts domain reasoning from worker execution or permission checks |

A **seed pack bundles nouns and verb templates/seeds**. It is not another entity
type, scheduler or runtime. Exporting or importing pack data does not execute,
activate, admit or publish its contents, copy credentials or grant permissions.
A pack carries no brand name and has no surface of its own; its repository does
not become an independent agent runtime.

**Project is a convenient human label, not an architectural primitive.** It may
name a surface and scoped collection of nouns and verbs. Store a needed
description or profile as an existing noun. Do not add a project service, core
table, separate execution authority or project-only lifecycle for this vocabulary.

Every new capability must identify its nouns, independently schedulable verbs,
surface, existing lifecycle owner and any infrastructure or server exception. Reject an implementation
that hides ordinary domain reasoning inside a server handler, creates a competing
task authority or lets a pack activate itself. Keep deterministic libraries and
compatibility endpoints when they serve workers; packaging is not a reason to
delete tested code or make every helper function a task.

Deterministic infrastructure and essential management can remain server-side:
identity, storage, leases, accounting, credentials, authorized provisioning,
scheduling and recovery. They retain their own authority and audit requirements.
Schedules may enqueue already-authorized work. Calling domain reasoning private,
urgent or management does not create an exception. Model use by a management
service must satisfy the explicit inference rules below.

Server-side assets and work still use nouns and verbs. Server-side describes
execution location and scope, not an exemption from the model or its access
controls. An explicit server exception changes where a verb runs; it never
removes ownership, permission, effect or audit requirements. Protected service
configuration, credentials and management artifacts remain scoped nouns, and
authorized management operations remain verbs at their useful lifecycle boundary.

Enforce the boundary in owning tests: reject implicit model fallback, unauthorized
effects and invalid contracts; prove that exporting a seed pack performs no
network, enqueue, installation or activation work. Documentation inventories and
tests of fixtures do not establish universal runtime coverage.

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

## Requirements, defaults and useful pointers

A precise outcome does not require a fixed method. A task can require an exact output contract while inviting the worker to choose or discover its tools. Conversely, a detailed recipe can be optional when the brief says so. Preserve that distinction when resolving category guidance into the worker's context.

| Guidance | Meaning for the worker |
| --- | --- |
| Required | Satisfy the stated outcome, check, boundary or explicitly required procedure. Report a conflict or unavailable requirement instead of silently changing it. |
| Preferred | Start with this method or tool when suitable. A better compatible approach is allowed within the task's authority; record a material change of approach and its reason. |
| Suggested | These resources or tools may help. They are optional, non-exhaustive starting points; the worker may search elsewhere and choose a suitable alternative. |
| Discovery requested | Investigate beyond the supplied pointers and compare relevant options within the allowance. Stop when the outcome and evidence are sufficient, rather than exploring indefinitely. |

These are authoring distinctions, not new public API enum values. Retain the source and strength of each instruction. Do not turn an optional tool list into a required tool list, an exhaustive search boundary or an execution allowlist. Suggested tools do not grant new permissions. Retrieved pages, tool output and imported packs do not acquire task authority by calling themselves required instructions.

A useful open-method brief can say: "You may find useful tools in these references. They are suggestions, not an exhaustive list. Search further if useful, reuse suitable existing work and choose the approach that meets the outcome and checks within the permitted effects and resource limits." A procedural brief should explicitly identify which steps or tool versions are mandatory. A requirement to use a particular tool is separate from the suggestion that it may help.

## Preparing and running tools in a harness workspace

A worker may be asked to obtain, install, configure and run a tool as part of an outcome. Setup can be an independent preparation verb when it has a reusable deliverable, different permissions or a separate recovery boundary. Otherwise it stays inside the owning task. Installing a dependency and starting a process do not each require another agent or model call.

Give the worker a concrete execution recipe:

1. **Inspect the environment.** Identify the assigned workspace, runtime, platform, existing tools and permitted effects. Reuse a compatible verified installation when available. Name the required tool or mark candidate tools as suggestions.
2. **Resolve the tool.** Record its source, exact release or revision, artifact digest, licence, runtime requirements and dependency lock where applicable. For an open-ended search, select and record these before execution. Verify the downloaded bytes against the expected source evidence. A matching digest alone does not establish that code is trustworthy.
3. **Prepare a task-owned location.** Use an explicitly assigned directory, such as `.tools/<tool>/<version>/` under the workspace, with separate inputs, outputs and logs. Confine extraction and writes to authorized paths, preserving unrelated files. Use a project environment, approved container or existing tool adapter as appropriate. Package installation may execute build or install scripts, so its isolation and permissions matter before the final tool starts.
4. **Define the invocation.** Supply the executable and argument vector, working directory, input references, expected outputs, permitted environment variables, network destinations and time/resource limits. Resolve only the needed secret references at the authorized boundary. Do not concatenate untrusted inputs into a shell command or inherit unrelated credentials.
5. **Check readiness and supervise execution.** Run a meaningful capability check, then the real command through the existing harness or process adapter. A separate process is useful for a command-line tool but is not a security sandbox. Use the isolation the tool and inputs require. Capture bounded, appropriately redacted output, errors and exit status; handle startup failure, timeout, cancellation and lease loss. A service needs an observed readiness check and a declared lifetime, not just a process identifier.
6. **Verify the result.** Check actual artifact contents and task-specific acceptance criteria. Installation success, a version string and exit code zero are separate from useful completed work. Preserve the source/version, invocation, environment manifest and check evidence with compact artifact references.
7. **Retain or clean up deliberately.** Stop only the processes owned by the task, confirm termination and preserve required outputs and failure evidence. Keep reusable verified installations when permitted. Remove only task-owned temporary material under the existing cleanup policy; do not delete another task's environment or a shared service.

Search, download, install, execution and external effects remain subject to their existing grants. A missing capability becomes an actionable failure, permitted alternative or preparation task, without silently enabling server inference. The recipe specifies how to perform authorized work; it does not create another approval layer or expand authority.

The documented subprocess interface provides concrete examples of argument vectors, working directories, explicit environments, timeouts and return-code handling. Those are process-control mechanisms; use the owning runtime's isolation and lifecycle controls as well. [Python subprocess documentation](https://docs.python.org/3/library/subprocess.html), checked October 28, 2026.

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
