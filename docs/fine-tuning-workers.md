# Optional fine-tuning workers

This research project example turns a permitted source observation and project
profile into a proposed capability noun and follow-up verb. Data preparation and
contract authoring use no ML framework. Training runs in an explicitly prepared
worker process through an optional PEFT backend; Tessera still owns identity,
leases, CAS records and receipts. Nothing adds torch to the server, a second
scheduler or an automatic model promotion path.

The [worker execution policy](WORKER-FIRST-EXECUTION.md) is a required architecture
checkpoint for changes here. A project organizes nouns and verbs using existing
contracts. Independently schedulable training, evaluation and review are verbs;
a deterministic tokenization helper stays a library function. Claiming private or
management status does not authorize server inference.

## Modules and artifact contracts

| Module | Responsibility |
| --- | --- |
| `examples/finetuning/data.py` | Owned synthetic observations, complete target JSON, provenance, immutable fingerprints, source and project family separation |
| `examples/finetuning/encoding.py` | Completion-only labels, exact template-prefix check and full-sequence fit rejection |
| `examples/finetuning/peft_backend.py` | Optional CPU/CUDA LoRA or single-device NF4 QLoRA, loss measurements, save/reload proof and environment report |
| `examples/finetuning/author.py` | Offline `RecordWrite` and `TaskSpec` previews; no client, enqueue or model import |
| `examples/finetuning/local_worker.py` | Actual private local curate/train claims, heartbeats, saved blobs, execution capture and denial checks |
| `examples/finetuning/glimmer.py` | Prepare a pinned official Axolotl configuration for a separately authorized H100 worker |
| `examples/finetuning/kaggle.sh` | Portable invocation inside an existing authorized GPU notebook; no notebook creation or credentials |

The example authors 20 deterministic observations: 12 training, four development
and four test examples. Both source families and project families remain disjoint
across the three splits. The generator, source policy, instruction hash, row IDs,
file hashes and dataset hash are recorded. Targets are repository-authored
synthetic templates under this repository's MIT license. No model outputs,
private histories, hidden reasoning or third-party datasets are training data.

The existing noun categories carry dataset/split manifests (`noun.benchmark_suite`),
base and unadmitted adapter descriptions (`noun.model_spec`), a recipe
(`noun.code_node`), and a run report (`noun.eval_run`). Stage verbs use
`verb.solve_draft` for curate/split and train, `verb.benchmark_harness` for evaluate,
and `verb.verify_gate` for review. These are examples of existing contracts,
not new ontology categories or permission grants.

## Run the local plumbing example

From a checkout, create a **separate worker environment**, outside the service's
`.venv`. Install a CPU or compatible CUDA torch build using
[PyTorch's installation selector](https://pytorch.org/get-started/locally/), then
install the optional pinned dependencies:

```bash
python3 -m venv /tmp/tessera-training-venv
source /tmp/tessera-training-venv/bin/activate
# Install the torch build selected for this worker before the next command.
python -m pip install -r examples/finetuning/requirements.txt
python -m examples.finetuning.run prepare --output /tmp/tessera-training-data
python -m examples.finetuning.run train \
  --dataset /tmp/tessera-training-data \
  --recipe examples/finetuning/recipes/tiny-smoke.json \
  --output /tmp/tessera-training-cpu --device cpu
```

Choose new output paths for each run. The smoke creates a random 110,784-parameter
Llama architecture including 3,584 trainable LoRA parameters, uses its own byte
tokenizer, trains for the recipe's 12 optimizer steps, and saves the random base
and adapter separately. It compares unchanged-base and adapter teacher-forced
loss on development/test families, hashes the artifacts, checks that frozen
parameters stayed identical, and reloads the adapter onto a **fresh** base. A run
fails if reload logits or held-out losses disagree beyond its recorded tolerance.
Run the same command with `--device cuda` in a compatible GPU environment.

This is a real adapter update on a tiny random model. It is **not pretrained
Meta Llama or Muse Glimmer fine-tuning**, and lower synthetic loss does not establish
factual grounding, relevance or useful task decomposition. The report leaves
those quality metrics unmeasured. The unchanged baseline remains the candidate
for use until a separate meaningful evaluation and review justify replacement.

The [October 29 smoke evidence](../examples/finetuning/evidence/smoke-2026-10-29.json)
records successful CPU and RTX 3060 local fabric runs. Both saved six base/adapter
blobs, captured the training execution and denied a different principal record,
blob and execution reads. The GPU run used 3,584 trainable parameters, measured
41,225,728 bytes peak allocated memory, and reloaded with zero probe-logit
difference. Its synthetic test loss changed from 5.586128 to 5.396280. Those
measurements qualify this tiny plumbing example only. Pretrained Llama, Glimmer,
Kaggle and useful-task quality runs remain unrun.

The explicit `sequence_budget` is a memory recipe, not an inference response
ceiling. Every full prompt and target must fit; over-length examples fail with
their identity and measured length. There is no truncation, padding target or
implicit `max_new_tokens` setting. One sequence per microbatch avoids padding;
gradient accumulation is an explicit recipe parameter. Changing step count,
sequence budget or rank is a new recorded recipe, not a server-wide cap.

## Preview nouns and verbs

For a real local noun/verb round trip, run the tiny example in its optional ML
environment with the Tessera source available:

```bash
PYTHONPATH=src python -m examples.finetuning.local_worker \
  --dataset /tmp/tessera-training-data \
  --recipe examples/finetuning/recipes/tiny-smoke.json \
  --output /tmp/tessera-training-fabric
```

It creates a task-owned SQLite fabric, keeps two scoped keys in memory, validates
the curation verb, claims the dependent training verb, heartbeats during optimizer
steps and artifact writes, and stores the actual base/adapter bytes as private
blobs. Submission captures the run as an existing execution noun. Readback checks
the receipt and artifacts; another principal must be denied record, blob and
execution reads. This executes the tiny smoke only. It does not pretend to run
the proposed quality evaluation or admission stages. Server-resident records and
operations remain nouns and verbs with authorization; execution location does
not exempt them from access checks.

Use the normal Tessera development environment for contract authoring:

```bash
uv run python -m examples.finetuning.author \
  --dataset /tmp/tessera-training-data \
  --recipe examples/finetuning/recipes/tiny-smoke.json \
  --report /tmp/tessera-training-cpu/report.json
```

This prints private create-only record writes and four dependent task specs.
The predicted version-1 noun references become valid only after authorized CAS
writes and exact readback. No task is enqueued, no worker is provisioned and no
artifact is admitted. If a device override changed the recipe, use the effective
`recipe` from that run's report when authoring its artifact references.

An existing authorized harness can claim these verbs and invoke the documented
worker command. It must maintain the lease, stop on lease/scope loss, verify input
pins, and submit private artifact references and checks through the existing
execution contract. This CLI does not implement a second claim loop or lease
supervisor. A completed dependency may have failed; inspect its receipt and
required artifacts before continuing. Store larger adapter bytes in the existing
content-addressed blob/bucket contract, with their digest referenced by the noun.

## Pretrained 1B/3B GPU recipes

The `llama-1b-qlora.json` and `llama-3b-qlora.json` recipes pin Meta Llama 3.2 Instruct
revisions and use frozen NF4 weights, rank-8 adapters on `q_proj`/`v_proj`, gradient
checkpointing, a microbatch of one and explicit accumulation. They target one
16 GB T4 or 12 GB RTX 3060 as small pilot configurations; actual fit and throughput
must be measured on the worker. No memory-fit guarantee or training cost estimate
is inferred from a recipe. The 1B model is the smaller starting point.

Install `bitsandbytes==0.50.2` in the optional CUDA environment. These model cards
require existing authorized access under the Llama 3.2 Community License. By
default, the runner uses only an already available cache. If access and downloads
are already authorized, `--allow-download` fetches the exact pinned revision; it
does not accept a license. An inference API or Ollama cloud key does not provide
training weights, a training GPU or license acceptance.

```bash
python -m examples.finetuning.run train \
  --dataset /tmp/tessera-training-data \
  --recipe examples/finetuning/recipes/llama-1b-qlora.json \
  --output /tmp/tessera-training-1b
```

This runner compares the adapter with the **same quantized base** and unchanged
prompt/schema on held-out families. A synthetic smoke remains a plumbing check
even when pretrained weights are used. For a useful pilot, curate permitted and
reviewed task inputs/final outputs/evidence, hold out whole source/project families,
freeze acceptance criteria and compare grounding, useful decomposition, failure
rate, latency and resources. Do not train around broken harness contracts or
malformed tool handling; fix those in code. Retain the baseline if no improvement
is demonstrated, and never tune repeatedly against the test split.

For Kaggle, attach this checkout and any already permitted pinned model cache to
an existing authorized notebook. Install the optional requirements and
bitsandbytes while preserving its compatible CUDA torch, set `TESSERA_CHECKOUT`,
`FINETUNE_OUTPUT` and `FINETUNE_RECIPE`, then run `bash examples/finetuning/kaggle.sh`.
It launches no notebook, spends no credits itself and carries no credentials.

## Muse Glimmer on an H100

Meta documents a single-H100 QLoRA path for the 30B Glimmer model. That is a
separate training environment and is not a claim that 30B training fits a T4 or
3060. Follow the official pinned Axolotl/CUDA dependencies and reload guidance;
keep them out of Tessera's service environment. [Meta post-training guide](https://dev.meta.ai/docs/muse-glimmer/post-training).

The optional bridge expects an existing cookbook checkout at
`b30ca2ce0c4abeb2a0aa52a4f2610a550d5d7447` and the already cached Glimmer tokenizer
at `a4e59da52a7bc87ae7251dd5545c0dd437c44b68`. It checks the official config's byte
hash, verifies complete example lengths, substitutes our train/dev message files,
keeps test examples separate, disables external tracking, and writes a single-GPU
configuration and command. It does not download, provision or train:

```bash
python -m examples.finetuning.glimmer \
  --cookbook /path/to/meta-oss-cookbook \
  --dataset /tmp/tessera-training-data \
  --output /tmp/tessera-glimmer-job --sequence-budget 4096 --steps 2
# Run only on an already authorized, prepared H100 worker:
CUDA_VISIBLE_DEVICES=0 axolotl train /tmp/tessera-glimmer-job/qlora.yaml
```

The bridge's configuration tests do not qualify an actual Glimmer training run.
Require the upstream environment/preflight tests, measured memory, an unchanged
baseline comparison and saved-adapter reload before claiming that result. The
current official cookbook path includes `muse-glimmer/` before `post-training/`.
[Reviewed official configuration](https://github.com/meta-models/meta-oss-cookbook/blob/b30ca2ce0c4abeb2a0aa52a4f2610a550d5d7447/muse-glimmer/post-training/sft/axolotl/configs/muse-glimmer-30b-qlora.yaml).

Library and model references checked October 29, 2026:
[PEFT adapters](https://huggingface.co/docs/peft/main/en/quicktour),
[Transformers bitsandbytes](https://huggingface.co/docs/transformers/main/en/quantization/bitsandbytes),
[Llama 3.2 1B](https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct),
[Llama 3.2 3B](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct).
