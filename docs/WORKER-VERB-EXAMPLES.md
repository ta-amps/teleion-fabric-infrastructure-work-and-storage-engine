# Work suitable for claimed verbs

These are design examples, not newly implemented endpoints or permission grants. Bind each example to the existing task, record, identity and effect contracts. The worker runs the substantive checks; the server checks the declared contract and evidence relationships.

| Work | Verb outcome and saved noun | Suitable worker | Check or acceptance boundary |
| --- | --- | --- | --- |
| Research | Source-backed findings with dates and unresolved questions | Research agent | Source access, quotations, dates and claim-to-source checks |
| Capability discovery | A reproduced gap and minimal failing fixture | Diagnostic agent | Reproduce the failure before proposing a repair |
| Task decomposition | A bounded dependency plan with required inputs and effects | Planning agent | Typed dependencies, budgets, ownership and no authority expansion |
| Tool preparation | A reproducible tool environment and invocation manifest | Worker with workspace and installation authority | Pinned source, confined setup, capability check and reusable environment evidence |
| Implementation | A patch and reproducible test result | Coding agent | Task-specific checks, affected suite and clean environment |
| Contract repair | A candidate schema, adapter or example fixing a demonstrated mismatch | Integration agent | Positive and wrong-type controls through the actual transport |
| Review | A decision about an exact artifact and explicit question | Qualified review agent | Appropriate independence and evidence tied to that version |
| Reusable library intake | A complete candidate with source, licence and file digests | Intake agent | Existing admission rules, missing files and execution checks |
| Feed upkeep | A sourced change record and corrected cursor-visible entry | Feed agent | Freshness, deduplication, source dates and public-field projection |
| Compatibility | A native harness run and exact setup manifest | Compatibility agent | Actual tool execution and result readback, not tool-like text |
| Data cleanup | A transformed dataset and rejected-row report | Data agent | Schema, row counts, invariants and input/output digests |
| Evaluation | A complete attempt population and scored evidence | Evaluation agent | Failures, exclusions, controls and reproducibility |
| Design | An editable scene, page or design asset | Design agent | Brief, dimensions, accessibility and meaningful revision |
| Video production | An editable sequence, captions and encoded deliverable | Media worker | Frame/audio checks, rights, aspect ratio, reopen and visual inspection |
| Deterministic rendering | A bounded render batch and output manifest | Renderer-capable worker | Exact recipe, dependencies and output checks; no model per frame |
| Public-good work | A useful artifact for a specific beneficiary need | Domain-capable contributor | Problem-specific acceptance; adoption remains a separately observed fact |
| Private analysis | A customer-owned analysis and decision brief | Customer-authorized worker | Tenant isolation, permitted sources and supported conclusions |
| Support preparation | A draft reply grounded in permitted account records | Scoped support worker | Minimal data access, factual checks and existing send authority |
| Cost analysis | A recommendation from dated usage and invoices | Private finance/operations worker | Separate observed spend, estimates, credits and model costs |
| Incident diagnosis | A cause hypothesis, reproduction and proposed repair | Private operations worker | Read scope, falsifying checks and no implied write permission |
| Deployment preparation | A tested release candidate and rollback evidence | Release preparation worker | Exact source and checks; existing deployment authority still applies |
| Authorized deployment | A permitted release and live result record | Restricted execution worker | Destination, release gate, health checks and rollback conditions |
| Configuration upkeep | A proposed configuration diff with affected resources | Private infrastructure worker | Scope, dry run where supported and explicit effect authority |
| Backlog maintenance | Deduplicated or clarified work with preserved source | Maintenance agent | No lost work, bounded fan-out and unchanged permissions |
| Management planning | A private capacity or priority proposal | Authorized management worker | Evidence, ownership, constraints and human decisions that remain required |
| Recovery | A reconciled uncertain outcome and safe next action | Recovery worker | Read existing records before repeating any effect |

Authentication, serving a stored file, checking a lease, hashing a result, simple arithmetic and deterministic status updates normally stay inside their existing operation. They do not need individual agentic verbs. A single rendering verb may produce thousands of frames without thousands of model calls.

## Worked private incident

1. A deterministic health signal identifies a failure and enqueues one private diagnostic verb with a deduplication identity.
2. The authorized operations worker receives only the relevant logs and configuration references. Other workers cannot see the brief or its results.
3. The worker reproduces the issue, runs checks and writes a candidate repair noun. If context is missing, it parks the task with a specific question.
4. If the applicable change policy requires judgment, a qualified private worker claims a review verb bound to the candidate version. Otherwise the existing deterministic gate is sufficient.
5. An execution worker with the required authority applies the allowed change and verifies the real destination. It records an uncertain outcome instead of blindly retrying a lost response.
6. A public update, if authorized, uses an approved redacted projection. The private logs and credentials never become public merely because the incident is resolved.

All of this can use worker inference without a coordinator model call. Operator-owned workers still consume operator resources; record that cost. Queue outage recovery follows the existing incident procedure rather than pretending the unavailable queue can schedule its own repair.

## Worked tool preparation and separate-process execution

This is an authoring example, not a new endpoint or a claim that a tool is installed. Replace workspace and version placeholders with actual authorized values from the resolved brief before running it. The source, digest, permitted installer, timeout and resource limits must also be resolved; this example does not supply a download to execute blindly.

**Outcome:** convert the supplied local movie, which contains video and audio, into `outputs/result.mp4` with H.264 video and AAC audio. Preserve the input, check the declared duration/dimension tolerances and save the verification report.

**Required procedure for this example:** use the exact FFmpeg build named by the task's approved tool reference. Reuse it if checked and compatible; otherwise prepare it under `<workspace>/.tools/ffmpeg/<pinned-version>/` using the declared source and installation recipe. Check that this build supplies FFmpeg, ffprobe, the `libx264` encoder and the AAC encoder before starting. This tool requirement is explicit; another task may make the same tool only a suggestion.

**Run:** set the working directory to the assigned workspace. Launch the executable through the harness's existing supervised process adapter with an argument vector:

```json
[
  "<workspace>/.tools/ffmpeg/<pinned-version>/bin/ffmpeg",
  "-nostdin", "-n", "-i", "inputs/source.mov",
  "-c:v", "libx264", "-c:a", "aac", "outputs/result.mp4"
]
```

This example assumes a compatible Unix-style tool bundle; resolve the actual executable paths for the worker's platform. The command avoids an interactive prompt and refuses to overwrite an existing result. On retry, inspect the existing result and task history before choosing a new attempt path or any authorized replacement. Input files and any referenced media must remain within the declared file and network scope.

**Check:** use the same pinned tool bundle to obtain machine-readable media information:

```json
[
  "<workspace>/.tools/ffmpeg/<pinned-version>/bin/ffprobe",
  "-v", "error", "-show_format", "-show_streams", "-of", "json",
  "outputs/result.mp4"
]
```

Compare the observed streams, dimensions and duration with the task's criteria, verify that the actual media decodes and perform playback or visual/audio inspection when the outcome depends on perceptual quality. A metadata probe alone does not establish that the whole output is good. Save the output reference and digest, the tool manifest, command result and relevant checks. Stop or retain the task's process/environment according to its declared lifecycle.

The option semantics and probe output are described in the official [FFmpeg](https://ffmpeg.org/ffmpeg.html) and [ffprobe](https://ffmpeg.org/ffprobe.html) documentation, checked October 28, 2026. The commands illustrate process invocation; no codec performance or successful local run is claimed here.

## The same outcome with open-ended guidance

An alternative brief can retain the same output and permission requirements while saying:

> FFmpeg and the linked media tools may help. These are starting points, not an exhaustive list or a required implementation. Inspect what is already available, search further when useful and choose a suitable approach. Prepare any needed tool in the assigned workspace, run it through the permitted execution environment and demonstrate the same acceptance checks. Record material departures from the suggested approach and why they helped.

The worker can discover a better existing tool or composition without requesting a routine reviewer to choose for it. Required effects, spending limits, data destinations and result checks remain unchanged. If comparing tools is itself the requested outcome, produce a bounded comparison with observed evidence rather than claiming that a listed resource is best.
