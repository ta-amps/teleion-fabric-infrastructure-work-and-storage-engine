# Continuous tuning, learning and evaluation

This example makes self-tuning a set of ordinary Tessera **nouns** and **verbs**. A
small open model gets better at one narrow job from a little owned data, round after
round, and every round is measured against the unchanged model on data it never saw.
Nothing new is added to Tessera itself: no table, queue, scheduler, registry or
server-side model call. The code lives in `examples/continuous_tuning/`, outside the
packaged service, and the GPU backend loads only in a prepared worker environment.

The job is **typed tool calls for the noun/verb fabric**: given a work request and a
few of Tessera's real MCP tool definitions, reply with one JSON tool call whose
arguments have the schema's types, or refuse with a fixed reason. Weaker models fail
this in the field by writing objects and numbers as strings (`"fencing_token": "51"`,
`"metadata": "{\"rows\": 3}"`), so strict servers reject the call.

**In brief.** On a frozen test of 150 requests over tool families it never trained
on, unchanged Llama 3.2 1B Instruct gave exactly the right typed call 12% of the time,
and 8% of its calls stringified a typed value. One round on 63 owned examples lifted it to 43% with no stringified values. The next round, with twice the
data, did not beat that, and the pre-registered rule kept the champion. A round that
continued from the champion reached 51%, again beating the champion on a fresh holdout.
A fourth round raised dev but not test, and the rule kept it out. On the same frozen
test, Llama 3.2 3B Instruct went from 52% to 73% after one round. Its second round
scored 79% but cut schema validity past a pre-registered limit, so it was kept out. This
is one example task with one seed per configuration on one GPU. It shows the loop
measuring and keeping real gains from little data; it does not show general
improvement. The [measured results](#measured-results) have the details.

## The pieces, as nouns, verbs, surfaces and infrastructure

| Kind | What | Existing category | Where it lives |
| --- | --- | --- | --- |
| Noun | Tool snapshot: the 37 tools `tools/list` serves | `noun.mcp_server` | `mcp_tools.json` |
| Noun | Base model (swappable): weights digests, license, tokens | `noun.model_spec` | `recipes/base-*.json` |
| Noun | Recipe and bounded search space | `noun.code_node` | `recipes/lora-sft-r16.json`, `recipes/search-lr3.json` |
| Noun | Learner spec (the component contract below) | `noun.code_node` | `learner_spec.json` |
| Noun | Trigger | `noun.code_node` | `trigger.json` |
| Noun | Pre-registered promotion rule | `noun.code_node` | `promotion_rule.json` |
| Noun | Example batches, accepted batches, training sets, frozen and fresh holdouts | `noun.benchmark_suite` | store `tuning.data`, rows in private blobs |
| Noun | Adapters, always unadmitted candidates | `noun.model_spec` | store `tuning.models`, bytes in private chunked blobs |
| Noun | Evaluation runs, selections, promotion decisions | `noun.eval_run` | store `tuning.evals` |
| Noun | Champion pointer, the one mutable key (compare-and-set) | record in `tuning.models` | `champion.<learner>:<base>` |
| Verb | `tick`: read the trigger, enqueue one round | `verb.decompose_dag` | `loop.py` |
| Verb | `generate_examples` | `verb.solve_draft` | `verbs.py` |
| Verb | `curate_dataset` | `verb.ingest` | `verbs.py` |
| Verb | `tune_hyperparameters` | `verb.benchmark_harness` | `model_verbs.py` |
| Verb | `train_adapter` | `verb.solve_draft` | `model_verbs.py` |
| Verb | `evaluate` | `verb.benchmark_harness` | `model_verbs.py` |
| Verb | `decide_promotion` | `verb.verify_gate` | `decide.py` |
| Verb | `mine_hard_examples` | `verb.problemize` | `verbs.py` |
| Verb | `serve_check`: one base, several adapters by name | `verb.benchmark_harness` | `serve.py` |
| Surface | CLI: `tick`, `serve-check`, `curve`, `pack`, `export-tools` | none | `cli.py` |
| Infrastructure | Tessera's records, blobs, leases, receipts, execution capture | unchanged | `src/tessera/` |

Every noun is a private, create-only record keyed by role and content digest, so a
replay writes nothing new and a changed input gets a new key. Bulky rows, per-row
scores and adapter files are private content-addressed blobs that the nouns pin by
SHA-256. A deterministic helper, such as tokenizing, hashing or scoring one reply,
runs inside the verb that needs it and is never a separate task.

There is no infrastructure exception. Everything runs in an authorized worker. A
future schedule would only enqueue the tick verb (see below); it would never train,
evaluate or call a model.

## One component, one contract

Following the modular rule, the learner is one swappable component described by a
spec noun with the shared fields. Several strategies for the same facet run side by
side and are compared from outcomes.

| Field | This learner |
| --- | --- |
| `learner`, `strategy`, `version` | `tool-call-specialist`, `lora-sft-hard-example-mining`, 1 |
| reads | tool snapshot, base model, recipe, accepted batches, frozen and fresh holdouts, champion pointer and its evaluations |
| writes (versioned) | batches, training sets, holdouts (create-only); adapters and evaluations (create-only); the champion pointer (compare-and-set) |
| trigger | `trigger.json`: at least 32 accepted rows not yet trained on, or 24 hours since the last evaluation |
| budget | one owned GPU, 3 search trials, 2 strategies a round, 512 training rows, 45 minutes a round, no provider calls, no spending |
| evaluation vs baseline | the current champion, which starts as the unchanged base; dev selects, frozen test plus fresh holdout plus sanity decide |
| consumers | workers that read the champion pointer before typed tool calls; the learner's next round; reviewers |

Tessera's learner-spec contract `tessera.learner-spec.v1` is described in
[execution-learning.md](execution-learning.md). This spec maps onto it field by field,
with two gaps still to settle:

| `tessera.learner-spec.v1` | This learner |
| --- | --- |
| `learner`, `strategy`, `version` | `tool-call-specialist`, `lora-sft-hard-example-mining`, 1. The contract's facet list has no model-adapter facet yet (the nearest, `training_export`, only exports data), so one must be added |
| `status`, `verb.runner` | Implemented, but the runner lives in `examples/` on an optional GPU worker; the contract requires runners under `tessera.learners`, so either the pattern allows optional worker packages or this spec is listed there as proposed |
| `reads` | accepted example batches, frozen and fresh holdouts, the champion pointer |
| `writes` | adapters (`noun.model_spec`, `tuning.models`), evaluations (`noun.eval_run`, `tuning.evals`), datasets (`noun.benchmark_suite`, `tuning.data`) |
| `trigger` | on demand; 32 new accepted rows; a daily evaluation |
| `budget` | 512 training rows, 2,700 s a round, a bounded number of local generations; no provider calls |
| `evaluation` | baseline: the champion; metric: strict exact match on the frozen test; method: the pre-registered rule; minimum evidence: 150 rows; keeping the baseline is valid |
| `consumers` | private, advice only: a worker chooses whether to use the champion |

Each verb's learning block cites this spec in `advice` with those field names.

## The specialist task and its data

All data is made up and owned: repository templates (MIT) fill slots from a SHA-256
counter, so a batch name always yields the same bytes on any Python version. Labels
are computed: a call's arguments come from the template and must validate against the
served schema, and a refusal's reason is fixed by the template. No model wrote any
example, label or request. No provider output, Muse API output, private history or
execution report is training data; the Muse API terms restrict training competing
models on their outputs, and execution reports are coordination evidence.

`mcp_tools.json` is the frozen benchmark: exactly the hosted `tools/list`
(`[...TOOLS, ...ACCESS_TOOLS]`) on October 29 before Tessera PRs #12 and #13, pinned
by digest, and the definitions every recorded round was prompted and scored with.
`mcp_tools.served.json` is the current served list: a test rereads the live JavaScript
through Node and fails on any drift, and another requires every benchmark tool to keep
an identical input schema there, so the benchmark's labels remain valid calls. Since
the freeze, only `submit`'s description changed (it now mentions the learning block);
no schema changed. A schema change would make a new benchmark version, never an edit
to this one. The Python MCP door serves the same hosted tools and required arguments,
plus `list_execution_observations`, which the hosted surface does not offer yet. The
hosted schemas also carry governance fields the Python door lacks (`producer_surface`,
`owner_key_id`, and the visibility and tag fields on `define_bucket`); labels follow
the hosted schemas.

Each example offers five tools: the right one (unless the case is "not offered"), up
to two from the same family as tempting look-alikes, and others. Training prompts only
ever show training-family tool definitions. A fifth of the rows are refusals: a
missing required value, a tool that is not offered, or an operation no tool performs
("Delete the file … from bucket …").

| Split | Families (tools) | Rows | Frames |
| --- | --- | --- | --- |
| train | lease (7), verb discovery (4), records (6), access (6) | 64 initial, then 32 streamed and up to 48 mined a round | 5 |
| dev | executions (3), discovery (2) | 80, frozen | 2 |
| test | buckets (6), feeds (3) | 150, frozen; plus 48 fresh each round | 2 |
| sanity | none (arithmetic, letters, repetition) | 40, frozen | none |

Whole families, their templates and their phrasing frames sit in exactly one split,
so dev and test measure generalization to tools the adapter never saw as targets.
Curation computes three fingerprints per row (exact content, normalized request, and
the request's template shape with every slot value masked). A training row may share
none of them with any evaluation row, and a duplicate is rejected with its reason.

Metrics, all strict, with no coercion: exact match after canonicalization (key order,
`300` equals `300.0`, optional arguments that restate the schema default or pass null
are dropped), JSON validity, schema validity, type correctness, the stringified-value
rate, refusal accuracy, refusal recall and false-refusal rate. A lenient diagnostic
also accepts code fences, prose around the JSON and the `name`/`parameters` alias, to
separate formatting failures from wrong tools or values; it never decides anything.

## A round

```text
tick (verb.decompose_dag) ── reads trigger + champion pointer ── enqueues one round:

curate_dataset(training sets) ─┬─ tune_hyperparameters (round 1: 3 lr trials, dev only)
                               └─ train_adapter × strategies → evaluate(selection, dev)
          → generate_examples(fresh holdout) → curate_dataset(freeze fresh)
          → evaluate(dev winner: frozen test, fresh, sanity; champion and base: fresh;
                     other candidates: frozen test, analysis only)
          → decide_promotion(pre-registered rule) → mine_hard_examples(dev failures)
          → generate_examples(streamed rows) → curate_dataset(accept mined, control, stream)
```

Tessera orders the work with `depends_on`; the worker only claims what is ready,
heartbeats during training and generation, and submits once. A failed verb is
recorded and dead-lettered (`max_attempts` 1); its dependents fail fast with the
reason, and the next tick plans a fresh attempt with new task ids, so evidence from
the failed attempt stays. Run directories are create-only, so nothing is overwritten.

- **Round 0** freezes dev, test and sanity, accepts the initial rows and evaluates the
  unchanged base. The base becomes the champion.
- **Round 1** runs the bounded search (learning rates 1e-4, 2e-4, 4e-4) on the
  initial rows (63 accepted of 64 drawn; one duplicate was rejected). Dev picks the
  winner; only the winner's test score counts.
- **Round 2** trains two strategies side by side: `mined` (initial + streamed + mined
  rows) and `random_control` (initial + streamed + the same number of untargeted rows),
  both from the base. Dev selects; the other is scored on test as analysis only.
- **Round 3**: `mined` (retrain on everything) against `continue_champion` (start
  from the champion adapter, train on rows it has not seen plus 32 replayed).
- **Round 4** (learner spec and trigger v2, committed in `1df006a` while round 3 ran
  under v1, after round 2's decision and before round 4 started): `mined` against `schema_augment`, the same rows
  plus 96 schema-perturbed rows. Dev failures, not test results, motivated it: after
  round 1 the adapter put request text into the wrong argument of tools it had never
  seen (the query in `category` or `tag`) and swapped look-alike tools, and round 2's
  extra rows of the same kind did not move dev. Perturbed rows take training-family
  requests and offer renamed variants of the training tools (new tool and argument
  names, same descriptions and types), so a correct label requires reading the schema
  in the prompt. The aliases are disjoint from every served tool and argument name, so
  no held-out name is taught; evaluation still uses only the real definitions.

Hard-example mining reads only the dev failures of the round's selected candidate. A
failure becomes a feature requirement (an object argument written as literal JSON, a
quoted integer, a missed refusal of a given reason, a look-alike tool), and new rows
are drawn from training-family templates that have those features. No dev text, tool
or template enters training. A matched random control of the same size lets the next
round measure whether targeting beat the same amount of new data.

## The pre-registered promotion rule

`promotion_rule.json` was committed (`d3961c8`) before any training round ran, and a
test pins its SHA-256. A candidate replaces the champion only if all of these hold:

- strict exact match on the frozen test improves by at least 0.03 and the paired
  bootstrap lower bound (2,000 resamples, 95%, fixed seed) is above zero;
- test JSON and schema validity drop at most 0.02, refusal accuracy at most 0.03;
- sanity accuracy drops at most 0.05 against the champion and stays within 0.10 of
  the unchanged base;
- exact match on this round's fresh holdout is not below the champion's;
- integrity: zero contamination, frozen base parameters unchanged, the saved adapter
  reloads onto a fresh base with identical greedy probe replies, and the evaluated
  set digests equal the frozen manifest.

Otherwise the champion stays, which is a valid outcome. Promotion moves only the
private local pointer the next round compares against: no deployment, publication,
admission or serving change. Every round scores the frozen test for its dev winner,
and for the other candidates as analysis only, so later rounds carry some
multiple-comparison risk; each round's fresh holdout is the check against that.

## Continuous operation

`trigger.json` is a noun: a training round when at least 32 accepted rows of the
counted origins (initial, streamed, mined) have not been trained on, an evaluation
round on a new fresh holdout when 24 hours pass without one, otherwise no change. For
this proof the training rounds are capped (three in trigger v1, four in v2).

Today the tick runs on demand:

```bash
PYTHONPATH=src:. python -m examples.continuous_tuning.cli tick \
  --root /private/tuning-runs/llama-3.2-1b \
  --base examples/continuous_tuning/recipes/base-llama-3.2-1b-instruct.json \
  --base-path /private/models/llama-3.2-1b-instruct --backend hf
```

Later, Tessera's flag-gated `scheduled()` handler (the Cloudflare cron that already
gates maintenance planning on a flag) could enqueue one `tick` task per learner and
window, with an idempotency key such as `tick:<learner>:<UTC date>`. That is the whole
schedule: it enqueues a verb, and an authorized GPU worker claims it. It never trains,
evaluates or calls a model on the server, never grants a permission, and an empty
queue or a missing worker is never a reason to run anything server-side. The design
of that handler belongs to its own change; this example builds no scheduler.

## Execution reports and privacy

Every verb submits an `execution-report-v1` report: problem, input and solution
summaries; input and output schemas with context, environment and toolchain digests;
blocks for what went right, what went wrong, next steps and duration; references to
the nouns it wrote; usage (local tokenizer counts for model verbs, explicit nulls with
a reason for deterministic ones); and the model (base id, weights digest, adapter
digests) or a declaration that no model ran. Those fields also form the report's
`learning` block (`outcome`, `went_right`, `went_wrong`, `next_steps`, `types`, `path`,
`duration_seconds`, `unknowns`, `advice`), and the same block rides in each submitted
output. The worker detects whether the store accepts the block and a null model
instead of assuming: the 1B rounds ran before main accepted them, so their reports
carry the block only in the output; the 3B rounds ran after, and carry it natively. Cost is listed as unknown: the GPU is owned and
electricity is not metered.

After each tick the worker asks a second principal, with its own key in the same
namespace, to read every record, blob chunk and execution run the tick produced. Each
tick log records the probe counts; all reads must be denied.

## Several specialists over one base

Each specialist is its own learner spec, champion pointer and set of adapter nouns;
the base-model noun is shared, and the holdouts are shared too when the data is the
same, because content-digest keys make identical nouns identical. Serving loads the
base once and attaches adapters by name (PEFT `load_adapter` and `set_adapter`; vLLM
and llama.cpp have multi-LoRA equivalents), choosing per request from the specialist's
champion pointer and falling back to the plain base when the pointer says `base`. A
rank-16 adapter on every projection is about 45 MB for the 1B model and 97 MB for the
3B, so many fit beside one 2.5 GB or 6.4 GB base. Swapping a specialist is a pointer change; adding one is a new spec
noun, not new infrastructure.

Measured on the 1B: one base loaded with three adapter nouns attached by name (the
round-1 trials). Each reproduced its solo dev replies exactly (80 of 80), and so did
the base with adapters disabled (80 of 80), at 4.75 GB peak, the memory of one model.

## Swapping the base model

The base is a noun: `recipes/base-llama-3.2-1b-instruct.json` pins every file's
SHA-256, the pad and end-of-turn tokens, LoRA targets and chat-template arguments.
`recipes/base-llama-3.2-3b-instruct.json` is the second base: it pins two weight
shards and adds only memory settings (gradient checkpointing, an evaluation batch of
8) so the 3B fits one 12 GB card in bf16 with the same recipe and hyperparameters; no
quantization is used. The learner key includes the base id, so each base has its own
champion pointer, while the frozen holdouts are byte-identical across bases (the
evaluation runs record the same set digests). The worker verifies every weight file
against the noun once per process before the first load. The Llama 3.2 Community License and Acceptable Use Policy apply to the
base and to every derived adapter ("Built with Llama"). Base weights and adapters stay
private: the pack, the evidence and this repository carry neither.

## The seed pack

`python -m examples.continuous_tuning.cli pack` writes `pack/`: a manifest plus 18
create-only `RecordWrite` files (tool snapshot, two base models, recipe, search
space, rule, trigger, nine verb templates including `serve_check`, learner spec and
an index) and one tick `TaskSpec` seed per base. Every value names its category and carries that category's required
dimensions under a stated method (`continuous-tuning.pack-dimensions.v1`); the tests
validate every record with `validate_category_payload` (no errors, no warnings), every
seed with `TaskSpec`, and every pin with the research seed's `NounRef`. Building is
offline and deterministic. Importing or exporting it executes, enqueues, activates,
admits and publishes nothing. A test writes the records into a store and has a worker
run the seed's baseline round.

## Reproduce and test

The GPU backend needs a separate worker environment with CUDA torch, Transformers
and PEFT (the runs used torch 2.15.0+cu132, transformers 5.22.0, peft 0.21.2 and
accelerate 1.15.0; evaluation and adapter nouns record them from round 3 on). Run from a
checkout with `PYTHONPATH=src:.`, one `tick` at a time; each writes a create-only tick
log under `<root>/ticks/`. Then read the curve from the stored nouns:

```bash
uv run python -m examples.continuous_tuning.cli curve --root <root> --base <base noun>
PYTHONPATH=src:. python -m examples.continuous_tuning.cli serve-check --root <root> \
  --base <base noun> --base-path <weights> --backend hf
```

Without a GPU, `--backend fixture` runs every verb, receipt, report and decision on
CPU with a deterministic stand-in model; its numbers describe plumbing only.
`tests/test_continuous_tuning.py` runs in `make check` with no ML libraries: the served
tool snapshot, held-out splits, contamination rejection, strict metrics, mining, the
pinned rule, a fixture loop of five rounds, a no-op tick and an evaluation-only round
with execution reports and other-principal denial, schema augmentation, and the seed
pack.

## Measured results

Llama 3.2 1B Instruct, bf16 LoRA (rank 16, every projection, 11.3 M trainable
parameters), one RTX 3060 12 GB, greedy decoding, one seed. "Test" is the frozen
150-row test over tool families the adapter never trained on; "fresh" is that round's
new 48-row holdout from the same families; "sanity" is the 40-row general set.
Exact match is strict. The lenient diagnostic equalled it in every round, the base
included: no reply was right but merely wrapped in fences or prose, so the gains are
not formatting alone.

| Round | Candidate (dev-selected) | Training rows | Test exact | Fresh: candidate / champion / base | Sanity | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | unchanged base | 0 | 0.120 | not run | 0.950 | baseline |
| 1 | lr 1e-4 of three trials | 63 | **0.433** | 0.417 / 0.104 / 0.104 | 0.925 | promoted: +0.313, 95% [0.220, 0.407], 55 rows better, 8 worse |
| 2 | random control (dev 0.5625 vs mined 0.550) | 137 | 0.433 | 0.354 / 0.396 / 0.146 | 0.925 | kept champion: +0.000, [−0.060, 0.067]; fresh and schema validity dropped |
| 3 | continue from champion (dev 0.7125 vs retrain 0.6125) | 178 | **0.507** | 0.438 / 0.417 / 0.146 | 0.925 | promoted: +0.073, 95% [0.007, 0.147], 20 better, 9 worse |
| 4 | schema-augmented (dev 0.7625 vs 0.6125 for the same rows without) | 358 | 0.507 | 0.396 / 0.375 / 0.146 | 0.925 | kept champion: +0.000, [−0.067, 0.073]; schema validity fell to 0.617 |

| Frozen test | Base | Round 1 | Round 3 (champion) | Round 4 (kept out) |
| --- | --- | --- | --- | --- |
| JSON valid | 0.853 | 0.980 | 1.000 | 1.000 |
| Schema valid | 0.342 | 0.708 | 0.750 | 0.617 |
| Type correct | 0.375 | 0.942 | 0.892 | 0.792 |
| Stringified values (share of calls) | 0.083 | 0.000 | 0.000 | 0.000 |
| Refusal accuracy | 0.760 | 0.833 | 0.807 | 0.780 |
| Refusal recall | 0.000 | 0.200 | 0.333 | 0.600 |
| False refusals | 0.050 | 0.008 | 0.075 | 0.175 |
| Replies reaching the decode allowance | 6 | 2 | 0 | 0 |

What it means, plainly. Sixty-three owned examples were enough to remove the field
failure: no stringified values, nearly every reply valid JSON, and type correctness
from 0.38 to 0.94. Doubling the data the same way did not help (round 2): the rule
held, which is the point of pre-registering it. Continuing from the champion on the
rows it had not seen did help (round 3). The two other strategies and the other two
learning rates are analysis only: on test, lr 2e-4 would have scored 0.460 and 4e-4
0.440 in round 1, round 2's mined adapter 0.453, and round 3's retrain 0.460; dev
chose differently from test in round 1, which is what a selection split is expected
to do sometimes. Round 3 also traded upward: refusal recall rose, and so did false
refusals.

What is still wrong is mostly reading unseen schemas: on dev, request text lands in
the wrong argument of a never-seen tool (`category` instead of `query`), and
look-alike tools get swapped. That diagnosis, from dev only, motivated round 4.

Round 4 tested that fix and is a useful negative result. Schema-perturbed rows lifted
dev from 0.6125 to 0.7625 on the same data, but the frozen test did not move (0.507,
tied with the champion; the control without augmentation also scored 0.507), schema
validity fell to 0.617 and false refusals rose to 0.175. Dev and test disagreed, and
the frozen test plus the pre-registered rule kept the regression out. The next
strategy to try is the same augmentation with refusal rows rebalanced and the dev
families' gains checked against a second held-out family before promotion.

Cost: each adapter trained in 64 to 347 s and peaked at 6.4 to 7.0 GB allocated (9.5
to 11.5 GB reserved by the allocator); scoring the 150 test rows took 36 to 79 s at 3.9
to 4.7 GB. Rounds 0 to 4 and the serving check used about 46 GPU-minutes, no provider
calls and no spending. Every one of the 55 verbs succeeded with one captured
execution. A final sweep had another principal try to read all 115 private records,
133 blob chunks and 55 execution runs: all 303 reads were denied.

One interruption: round 4's holdout evaluation lost its worker when the session was
stopped, and its lease lapsed. The reaper returned it for a single retry (the queue's
`max_attempts` raised to 2 for that one reap, then restored to 1), and the retry
completed; the lapsed attempt stays in the event log, and nothing earlier in round 4
was rerun. The public summary is
[`evidence/llama-3.2-1b-instruct-2026-10-29.json`](../examples/continuous_tuning/evidence/llama-3.2-1b-instruct-2026-10-29.json).

### Second base: Llama 3.2 3B Instruct

Same frozen dev, test and sanity sets (identical digests), same recipe, search and
rule; bf16 LoRA with gradient checkpointing (24.3 M trainable parameters), evaluation
batch 8, no quantization. These rounds ran on main after the execution-learning merge,
so their reports carry the learning block natively.

| Round | Candidate (dev-selected) | Training rows | Test exact | Fresh: candidate / champion / base | Sanity | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | unchanged base | 0 | 0.520 | not run | 0.925 | baseline |
| 1 | lr 4e-4 of three trials (dev 0.775) | 63 | **0.727** | 0.646 / 0.563 / 0.563 | 0.950 | promoted: +0.207, 95% [0.120, 0.293], 40 rows better, 9 worse |
| 2 | random control (dev 0.8125 vs mined 0.650) | 138 | 0.793 | 0.667 / 0.625 / 0.521 | 1.000 | kept champion: +0.067, 95% [0.007, 0.133], but schema validity fell 0.025 against a 0.02 limit |

The 3B starts far stronger (0.520) but stringifies more (17.5% of calls; schema valid
0.675). The same 63 rows removed stringified values, lifted schema validity to 0.908 and
type correctness to 0.933, and gained +0.207. In round 2 the random-control adapter
scored higher still (0.793, fresh 0.667, sanity 1.000), but its schema validity fell
0.025 where the rule allows 0.02, so the champion stayed. A guard written before any
result stopped a real-looking gain. Across both bases, targeted mining did not beat a
matched random control in round 2; on test, the analysis-only mined adapters scored
0.453 (1B) and 0.720 (3B). Each 3B adapter trained in 202 to 439 s, peaking at 8.4 GB
allocated (11.8 GB reserved). Rounds 0 to 2 took about 51 GPU-minutes. All 27 verbs
succeeded, and a final sweep denied all 193 reads by another principal. Training then
stopped at the owner's request (see "Data before training"); round 3 was never started.
The public summary is
[`evidence/llama-3.2-3b-instruct-2026-10-29.json`](../examples/continuous_tuning/evidence/llama-3.2-3b-instruct-2026-10-29.json).

| Base | Round 0 | Round 1 | Round 2 | Round 3 | Round 4 |
| --- | --- | --- | --- | --- | --- |
| Llama 3.2 1B | 0.120 | **0.433** promoted | 0.433 kept | **0.507** promoted | 0.507 kept |
| Llama 3.2 3B | 0.520 | **0.727** promoted | 0.793 kept (schema guard) | not run | not run |

## Data before training

Is there enough data, trace and evaluation to make training worth it? From these runs:

- **Checked rows.** Training families now hold 586 checked rows in the 1B run: lease 219,
  records 152, access 97, verb discovery 82 and unsupported-operation refusals 36. The
  held-out families hold none, by design. All are example template draws; there are
  no real, verified tool-call traces for this task yet.
- **What paid off.** About 16 rows per training family (2 to 3 per tool) taught the
  output contract and typed values: +0.31 for the 1B and +0.21 for the 3B, on families
  never trained on. More rows of the same kind did not help. Argument semantics for an
  unseen family did not transfer from other families, so family-specific training needs
  that family's own rows, on the order of 100 checked examples (about 10 per tool).
- **Holdout size.** The rule needs a paired bootstrap lower bound above zero. With the
  discordance these runs show (about 18% of rows flip between two close adapters), the
  150-row test detects a true 5-point gain only about 31% of the time. 80% power needs
  about 560 rows (450 at 14% discordance), per family if the claim is per family.
- **What Tessera records.** Task identity and category tags, the brief and input
  snapshot, worker, timing, receipts, ok or error, the event log, and the worker-reported
  report and learning block: outcome, what went right and wrong, types, path, usage, and
  advice. Fingerprints and a permission-filtered observation view come with them.
- **What a training set still needs.**
  - outcome labels from acceptance checks, because reported outcomes are not
    independently verified;
  - step-level grades for multi-step runs;
  - stable task-family and template tags;
  - the trainable pair itself (offered tools, prompt and accepted reply, which reports
    omit by design);
  - provenance and consent per run, since execution reports are excluded from training
    by default.
- **Cheaper first.** A deterministic shim that decodes a stringified typed argument
  before strict validation, recorded as a repair rather than a silent coercion, lifts
  the unchanged 3B from 0.520 to 0.647 on the stored test replies (1B 0.120 to 0.140)
  with no training. Tessera's memory (recall of verified calls), path-statistics and
  routing-weight learners can use the same traces now. They need verified outcome labels
  too, but far fewer of them.

## Issues found while running

- A candidate continued from the champion was recorded with only that round's new
  batches, so the trigger before round 4 reported 135 rows not yet trained on when 72
  were. The decision to run was still right (72 is over 32). Fixed in `9390050`,
  which round 4's resumed decision already used.
- Nouns from rounds 0 to 2 record only Python, platform and Tessera versions; library
  versions, CUDA, the GPU and verified weights are recorded from round 3 on. The 3B's
  first search trial records `weights_verified: false` because the environment was
  read before that process's first load. The weights were checked at that load, which
  refuses a mismatch, and later nouns record `true`. Fixed for later ticks.
- The base tokenizer's chat template appends an assistant header even after an
  assistant turn, so labels end at the first end-of-turn token rather than at the
  rendered end; a test covers this.

## Limits

- One narrow, example task. A gain here says nothing about other tasks, real user
  traffic or a harness's end-to-end success.
- Two base models, one GPU, one seed per configuration; no variance across seeds was
  measured. The 3B ran rounds 0 to 2; training then stopped at the owner's request
  until there is more checked data (see above).
- The frozen test is reused across rounds; the per-round fresh holdouts are the guard.
- Mined and control rows are template draws, so "hard example" means "has the failing
  feature", not "is individually difficult".
- The champion pointer is local; nothing here admits, deploys or serves an adapter.
