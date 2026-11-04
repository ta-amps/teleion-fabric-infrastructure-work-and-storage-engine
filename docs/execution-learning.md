# Execution learning: observations, fingerprints and swappable learners

First increment, October 29, 2026. Workers are encouraged, never forced, to describe
how a verb went. Core Teleion Fabric stores that report with the existing execution record,
serves a bounded observation of each permitted run and ships pure fingerprint
functions. Everything that decides what to learn is a separate, swappable learner
that runs as an ordinary verb and writes ordinary versioned nouns.

Status in this increment:

| Part | Status |
| --- | --- |
| Optional `execution_report.learning` block, Python and Worker | Implemented, tested |
| Observation read: Python store, HTTP, MCP and client | Implemented, tested |
| Observation read in the Worker | Module function tested on workerd; routing pending (see below) |
| Exact and MinHash fingerprints, Python and Worker, shared vectors | Implemented, tested |
| Learner-spec, config and run-verb contract | Implemented, tested |
| Memory learner (`minhash-lsh` v1): index verb and consumer recall | Implemented, tested |
| Path-statistics learner (`wilson-grouping` v1) | Implemented, tested |
| Routing-weights learner (`beta-bandit` v1) | Implemented, tested |
| Phrasing, searchability, shim, decomposition, preference, training export | Proposed learner specs only |
| Recurring schedules from schedule nouns | Proposed design only |
| Hosted deployment | Not deployed. Nothing here was pushed or released |

Local tests are not evidence that any learner improves real work. That needs the
evaluations each spec names, on real permitted runs.

## Nouns, verbs, surfaces and infrastructure

| Element | What it is here | Existing owner |
| --- | --- | --- |
| Noun | Execution run (`noun.eval_run`), with the optional learning block inside its report | Execution capture |
| Noun | Learner spec (`learn.specs`, tagged `noun.skill_pack`) and learner config | CAS records |
| Noun | Memory index head and segments, path scorecards (`noun.eval_run`) | CAS records |
| Noun | Routing weights (`noun.reflex_procedure`) | CAS records |
| Verb | One learner run: a private task whose brief pins a spec version and a state version | Task queue |
| Verb | Proposed: comparison, phrasing drafts, training export, curate/train/evaluate/promote | Task queue |
| Surface | `GET /v1/executions`, MCP `list_execution_observations`, `Client.execution_observations`, `tools/learner.py` | HTTP, MCP, CLI |
| Infrastructure | Report validation, permission-filtered observation read, pure fingerprint library | Core |

No new database, queue, scheduler, permission registry or server-side model call
was added. A learner is a worker-side component: architecture rules forbid it from
importing the store, SQLite or the web stack, and forbid core modules from importing
learners (`core-never-imports-learners`, `learners-use-the-public-contract` in
`architecture.json`, checked by `make architecture`).

## The optional learning block

A worker may add `learning` to the existing `execution_report` on submit. Every field
is optional and every value is an attributable **worker claim**. A missing field never
blocks submission. A field that is present but malformed fails with a precise path,
as the rest of the report does, and the worker can resubmit with the same
idempotency key. Python and Worker produce identical messages (shared vectors).

| Field | Shape and bounds |
| --- | --- |
| `outcome` | `succeeded`, `partial`, `failed`, `blocked` or `no_change`: the worker's own assessment |
| `went_right`, `went_wrong`, `next_steps`, `ideas` | Up to 8 nonblank strings each, at most 512 UTF-8 bytes |
| `types` | Optional `input` and `output` type signatures, at most 256 bytes, such as `video/mp4` and `image/png[]` |
| `path` | Optional `shim` and `method` labels, at most 128 bytes |
| `reasoning_summary` | Optional, at most 2048 bytes. A short summary, never raw private reasoning |
| `duration_seconds` | Worker-measured duration, 0 to 1e9, or `null` with a matching `unknowns` entry |
| `cost` | `{amount, currency, source}` with a three-letter currency, or `null` with an `unknowns` entry |
| `unknowns` | Up to 16 `{field, reason}` entries for measurements the worker could not take |
| `advice` | Up to 8 `{learner, strategy, version, store, key, record_version}` citations of learner outputs this run used |

Existing report fields carry the rest: `summaries.problem` and `summaries.solution`
(the problem and solution summaries), `harness`, `model`, and `usage` tokens.
`model` may now be `null`, which declares that no model was used; an absent `model`
still means unreported. The whole report keeps its existing 32 KiB canonical limit.

**Server observations**, recorded by Teleion Fabric rather than claimed: task id, kind,
queue, fence, authenticated key, claim and submit times, lease duration, accepted or
failed result, the state after submission, the failure text the worker submitted
with `ok=false`, the record hash, the input snapshot digest and whether the run
was a retry (fence above 1). The observation view labels these separately from the
report, and `trust.telemetry` stays `worker_reported`.

### Privacy and use

Never put credentials, raw prompts, transcripts or private chain-of-thought in a
report. Reports are coordination evidence, **not a training corpus**. Each
observation and learner output carries `use_policy.training_corpus:
excluded_by_default`. Provider terms can restrict training on model outputs; per the
owner's October 29 guidance, Meta's API terms restrict using Muse outputs to train
competing models. Any training use is a separate opt-in export (layer 3 below).
Observations and learner outputs inherit permission filtering: the observation read
rechecks the same source task, queue and artifact rules as `get_execution`, and
learner outputs are private nouns unless a spec names an explicit shared surface.

### How verbs encourage reporting

A verb asks for telemetry in its own brief. Learner runs (and any seed that wants
it) include `reporting`, the `REPORTING_HINT` from `tessera.observations`: requested
fields, what never to include and the rule that submission never requires them.
The brief travels in every claim response, so the hint arrives with the claim; no
claim-path change was needed. MCP instructions and the `submit` tool description
mention the block in both runtimes. The optional report step sits in the ordinary
cycle: claim, optionally recall, work, compose the report, submit once.

A server-wide `reporting` field on every lease is a later option. It would touch the
Worker claim path, which another change is editing now.

## Observations: the stable read interface

`GET /v1/executions?queue=Q[&kind=K][&after=CURSOR][&limit=N]` (MCP
`list_execution_observations`, `Client.execution_observations`) returns up to 100
`execution-observation-v1` items in capture order, with `next_after` for paging. The
caller needs read scope and a grant for the queue. Runs the caller may not read are
skipped while the cursor advances, so a page can be empty and still continue. Items
exclude outputs and the input snapshot. They include a bounded brief view (template,
wording fields within 4096 code points, top-level shape, type signature), the input
digest, server observations and the report subset learners need. Observations are
evidence, never permission to reuse an output.

The cursor is the execution row's insertion order, so incremental learners need no
new index and the submit path writes nothing extra.

## Fingerprints (core library)

`tessera.fingerprints` and `deploy/cloudflare/execution_observations.js` are pure
functions. `tests/fixtures/execution_learning_vectors.json` pins their outputs, and
`tests/test_execution_learning_vectors.py` checks both runtimes against it,
including non-ASCII text, astral-plane keys, lone surrogates, budgets and bounds.

**Exact.** `exact = SHA-256(canonical JSON {profile, category, input_type,
input_digest})`. `category` is the task kind. `input_type` is the bounded structural
signature of the brief, for example `{"contract":str,"goal":str}`, with containers
cut at depth 3 and SHA-256 above 1024 bytes. `input_digest` is the existing digest of
the full captured input snapshot. All three are server-derived, so worker claims
cannot merge or split exact groups. No digest, no exact fingerprint.

**Fuzzy.** MinHash over a feature set: `c:<category>`, declared type tokens
(`ti:`, `to:`) and distinct problem words (`w:`) from the brief's wording fields plus
`summaries.problem`. Words are ASCII case-folded, split on ASCII punctuation and
whitespace, with non-ASCII characters kept. Single ASCII letters and a fixed list of
English function words are dropped. At most 512 words are used. Brief shape is
deliberately left out: verbs from one template share it, and including it made
unrelated problems look alike in testing. The template is reported separately.

Each feature is hashed once with domain-separated SHA-256. Its eight 32-bit words,
mixed with the MurmurHash3 finalizer, give 32 hash functions; the signature keeps
the minimum of each. Similarity is the fraction of equal minima, an estimate of
Jaccard overlap with a standard error near 0.09 at 0.5. It is a ranking signal, not
a probability.

**LSH bands.** Four bands of two values. A pair with Jaccard similarity J shares a
band with probability 1 − (1 − J²)⁴: about 15% at J = 0.2, 50% at 0.4, 68% at 0.5,
93% at 0.7 and 98% at 0.8. Four bands is the smallest count that gave useful recall
near 0.5 while keeping noise low. Bands are part of the library; the memory learner
stores them inside its segment nouns, not as rows.

**Limits.** This is lexical overlap, not semantic equivalence. Paraphrases with
different words score low. Languages written without spaces produce coarse tokens.
Caller-supplied embeddings remain the semantic path in the existing recall. Text is
not Unicode-normalized, and only ASCII letters are case-folded, so both runtimes
agree exactly.

## The learner contract

Every learner is described by a **learner-spec noun** (`tessera.learner-spec.v1`):

| Field | Meaning |
| --- | --- |
| `learner`, `strategy`, `version` | Facet, swappable strategy and its version; the spec key is `learner:strategy:vN` |
| `status` | `implemented` (has a registered deterministic runner) or `proposed` (design only, cannot run) |
| `reads` | Observation kinds, such as `execution_observation` |
| `writes` | Learned noun kinds, each with its existing noun category and store |
| `trigger` | On demand, a schedule noun, or a threshold of new observations |
| `budget` | Observations, runtime, output bytes and model calls (zero for deterministic learners) |
| `evaluation` | Baseline, metric, method and minimum evidence; `keep_baseline_valid` is always true |
| `consumers` | Output visibility (`private` or `surface_only`); outputs are always advice only |
| `verb` | Existing verb category and, when implemented, the runner |

A **config noun** (`tessera.learner-config.v1`) activates specs as `active`,
`shadow` or `off` per category and surface. Two active specs may not claim the same
scope. The exported default config activates nothing. Several strategies for one
facet can run side by side (one active, others shadow), each writing outputs tagged
with its strategy and version.

A **learner run** is a private verb (`tessera.learner-run.v1`). Its identity is the
digest of the pinned spec version, the source scope, options and the **state
version** it starts from, so a duplicate trigger for the same state is the same task
and adds no work. The runner rereads the spec noun and refuses a digest mismatch,
reads only permitted observations, writes outputs in one CAS batch with expected
versions, and reports `superseded` when another run already advanced the state. Its
own execution report declares `model: null`, zero tokens, and cost as an explicit
unknown. Consumers cite the output version they used in `learning.advice`, which is
what a future comparison verb needs to pick a winner from outcomes. **Keep baseline**
is always a valid result.

The runner registry in `tessera.learners.worker` is the swap point.
`tools/learner.py` lists specs, exports the seed pack, previews a run offline,
enqueues with the caller's grants, runs one claimed learner verb and performs
recall. Use a queue dedicated to learner runs.

## Memory learner (implemented): `memory` / `minhash-lsh` v1

**Index run** (`verb.ingest`, deterministic). It pages observations after the head
cursor, up to its budget (2000), and fingerprints each one. Entries are written in
immutable segment nouns of at most 128 entries
(`memory:minhash-lsh:v1:<queue>:seg:NNNNNN`, with `:<kind>` after the queue when a
run is scoped to one kind), then one versioned head noun (`…:head`) that pins every
segment by version and digest and stores the cursor.
Entries keep fingerprints, outcome fields and normalized failure reasons. They
contain no outputs or wording fields.

**Recall** runs in the consumer, not on the server. It reads the head and segments
the consumer may read and checks each segment digest against the head. It
fingerprints the query (category, brief, optional draft report, optional input
digest from the existing recall) and scores entries. Indexes of up to 20,000
entries are scanned exactly; larger ones are first filtered by LSH bands or the
exact fingerprint. Entries below similarity 0.3 are dropped. The accepted ones are
ranked, and the top ones are read back through `get_execution`, which rechecks
permissions. The result has `evidence_only`, `reuse_requires_validation`, the index
version under `advice`, and outcome statistics over all similar entries, failures
included: attempts, acceptance rate with a 95% Wilson interval, distinct tasks,
retried attempts, and median server duration, tokens, reported duration and cost
where known. It also lists worker outcomes, common failure reasons and reported
problems.

The existing server-side `recall_execution` (exact input, contract shape and SimHash
bands) is unchanged. It remains the synchronous strategy and the baseline the memory
learner must beat. The memory index is eventually consistent; runs after the cursor
appear after the next index run.

## Path statistics (implemented): `path_statistics` / `wilson-grouping` v1

A deterministic run (`verb.benchmark_harness`) groups one window of observations by
chosen dimensions (`category`, `template`, `phrasing`, `shim`, `method`, `harness`,
`model`, `queue`, `tagset`; default category, template, phrasing and shim) and writes
one versioned scorecard noun per queue (`noun.eval_run`). Each group carries the
outcome statistics above, a `small` sample flag under five attempts and example run
ids. The scorecard pins the exact runs it used (`runs_sha256`) and is labeled
observational: paths saw different inputs, workers and times, so differences are
associations, not causal effects. Acceptance means a submission was recorded with
`ok=true`, not independent admission.

## Three layers of learning

**1. Memory.** Reports, exact and fuzzy fingerprints, similar-execution recall and
path statistics: the learners above.

**2. Learned routing weights (implemented as `routing_weights` / `beta-bandit` v1).**
Per category or task family, the run computes selection weights over one alternative
dimension (shim, method, harness, model, template or phrasing). Each arm gets a
Beta(1 + accepted, 1 + failed) posterior. An arm keeps the uniform prior score of
0.5 until it reaches `min_evidence` attempts (default 20). Every known arm keeps an
exploration floor, weight = ε/arms + (1 − ε) × score/Σscores with ε = 0.1 by
default. The output is one versioned weights noun (`noun.reflex_procedure`). Weights
advise a worker's choice. They never grant permission and never override an explicit
caller or provider choice, and the reasoning stays in workers. Every choice cites
the weights version it used. Decomposition patterns and seed packs become further
dimensions once observations carry them. Evaluation reserves an exploration share
for the baseline arm and requires the stated evidence before weight moves. The
current runner is observational; randomized assignment is the stronger proof.

**3. Model weights (proposed).** A training-candidate export verb (`training_export`
spec) turns opted-in, accepted, checked, rights-cleared runs with source into
dataset-manifest nouns (`noun.benchmark_suite`). They follow the existing
fine-tuning example contracts: family-separated splits, holdout separation, and
dedup by fingerprint. Raw Muse API outputs are excluded by default because Meta's API
terms restrict training competing models on them. Downstream curate, train, evaluate
and promote verbs run on authorized GPU workers. Each adapter is compared against the
unchanged base on frozen holdouts, and keep baseline is a valid outcome. Several
specialists can be LoRA adapter nouns over one base model noun. Nothing in this
increment exports data or trains a model.

**Risks, plainly.**

- **Feedback loops.** Training on the system's own accepted outputs can amplify its
  own habits and errors. Weights and phrasing advice can starve alternatives.
- **Holdout contamination.** Near-duplicates of test items leak into training.
- **Drift.** Old statistics stop describing new inputs, workers or models.

Mitigations: exploration floors and shadow strategies; frozen holdouts split by
family (template, phrasing, exact fingerprint); exact and MinHash fingerprints used
to dedup across splits and to flag holdout items whose similarity to any training
item exceeds a set threshold; windows and versioned outputs so a stale weights or
scorecard version is visible in every citation.

## Other learners as specs (proposed, design only)

| Learner / strategy | Reads | Writes | Evaluation against baseline |
| --- | --- | --- | --- |
| `phrasing` / `acceptance-by-phrasing` | observations, path scorecards | `noun.prompt_shim` recommendations | Shadow-assigned new wordings versus the current phrasing |
| `searchability` / `findability` | search observations (not recorded today), observations | `noun.seed_hypothesis` tag/description proposals | Held-out query rank of the noun later used |
| `shim` / `injection-uplift` | observations | `noun.prompt_shim` scorecards | Matched with/without-shim comparison within template |
| `decomposition` / `split-pattern` | observations, task graph observations (not exposed today) | `noun.reflex_procedure` patterns | Parent acceptance, child attempts and wall time |
| `preference` / `pairwise-choice` | deliberate choice observations, such as palette picks | private `noun.eval_run` profiles | Agreement with held-out later choices |
| `training_export` / `rights-cleared-export` | observations, execution runs | `noun.benchmark_suite` manifests | Adapter versus unchanged base on frozen holdouts |

The full specs live in `tessera.learners.catalog`. `tools/learner.py pack --output DIR`
writes them as a deterministic seed pack: spec nouns, an empty config noun, run-verb
templates for the implemented learners and a manifest of file digests. Exporting or
importing the pack makes no network call and enqueues, activates or publishes
nothing (tested).

## Write cost

Hosted storage is limited by rows written, and every index entry counts. The Workers
Free plan allows 100,000 rows written per day across Durable Objects SQLite. The
figures below were measured on workerd with `SqlStorageCursor.rowsWritten` in
`tests/workerd_execution_learning.mjs`. That is the runtime's own counter, not a
production bill. D1 writes are unchanged by this work.

| Operation | Rows written |
| --- | --- |
| Accepted submit, report with problem text, before this change (main `5545851`) | 36 (16 are SimHash band rows) |
| The same submit with a full learning block, after | 36: the block lives in the existing execution row |
| Accepted submit without a report, before and after | 20 |
| Rejected alternative: synchronous MinHash bands in a new table | +4 per submit (1 per band in a WITHOUT ROWID table; 2 in a rowid table) |
| Enqueue one private verb / claim it | 12 / 13 |
| Learner outputs: one segment plus head, two tags each | 27, the same for 1 or 128 indexed runs |
| One memory index run, end to end | ≈ 72 (enqueue 12, claim 13, submit 20, outputs 27), plus ≈ 11 per extra 128-run segment |

The asynchronous learner therefore costs about 2.25 rows per indexed run at the
default trigger of 32 new runs and about 0.56 at a full 128-run batch, against 4
for the synchronous alternative. It also keeps the submit hot path unchanged. Below
about 18 runs per batch, synchronous bands would be cheaper; the trigger threshold
exists to stay above that. Each tag costs three rows per record version (table,
primary key and lookup index), so learner outputs carry two tags.

Existing cost worth a decision: the SimHash band table already written on every
reported submit uses 2 rows per band. A WITHOUT ROWID layout would halve that from
16 to 8 rows for new databases with identical queries. It was not changed here, because
existing databases would need a migration that rewrites their band rows.

## Recurring learner and discovery schedules (proposed design, not implemented)

The Worker already has a 6-hour `scheduled()` cron that exits unless
`FRAGILITY_PLANNER_ENABLED`, and another change is editing that handler now. The
proposed adapter authorizes nothing new and stays off by default:

1. An operator key writes **schedule nouns**: target verb template, spec or seed
   pins, cadence, window size, owner key, maximum outstanding tasks and maximum
   catch-up windows.
2. On each tick, for each schedule, the adapter rechecks the owner key: it must be
   unrevoked and unexpired, and still allowed the target queue, kind and effect
   class. Otherwise it skips and records why.
3. It enqueues the referenced verb under the owner with idempotency key
   `schedule:<key>:<version>:<window>`. The existing unique index dedupes repeated
   ticks.
4. It stops at the outstanding-task cap and enqueues at most the catch-up limit of
   missed windows, oldest first. It makes no model call.

Uses: periodic memory, path-statistics and routing runs through the threshold
trigger, and recurring discovery of new motion-graphics or video tools. There, a
`verb.probe_source` or `verb.fetch_feed` seed (see `docs/research-seeds.md`) produces
candidate tool nouns for later review, never activated tools.

## Worker integration still to do

Learning-block validation and the observation function run on workerd today. To
expose the observation read on the Worker after the current routing and access work
lands:

1. Add `listExecutionObservations` (`execution_observations.js`) as an RPC action in
   `cloud_store.js` `_rpcCall`.
2. Add it to the read actions, and to anonymous actions with the `demo.` queue rule,
   in `cloud_access.js`.
3. Route `GET /v1/executions` in `worker.js` next to the other execution routes.
4. Add `list_execution_observations` to `cloud_mcp.js`.
5. Add the path to `scripts/build_access_openapi.py` and regenerate.

Until then, learners can run against the Python service. On the Worker they can use
existing per-task reads.

## Verification

- `tests/test_execution_learning_vectors.py`: Python and Worker reproduce every
  vector, including identical error messages.
- `tests/test_execution_learning.py`: the learning block is optional and adds no
  rows; the observation read is permission filtered and pages correctly; the MCP
  tool works. The memory index is incremental, deduplicates identical state
  versions, supersedes stale runs, recalls with statistics and keeps its outputs
  private. Path statistics are deterministic; routing weights respect thresholds and
  exploration. Spec, config and pin checks hold; the pack export and offline CLI make
  no network call.
- `tests/workerd_execution_learning.mjs` (`make test-worker`): learning block over
  HTTP and MCP, null model, rows written, observation permissions and paging, and
  learner output cost on workerd.

No model, provider or hosted service was called by any of these tests.
