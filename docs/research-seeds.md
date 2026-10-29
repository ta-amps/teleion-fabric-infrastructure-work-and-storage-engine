# Research seeds and proposed recurring work

`tools/research_seed.py` prepares one private verb using existing Tessera task,
noun, lease and receipt contracts. Its default is a network-free preview. Explicit
enqueue first reads every referenced noun version through the caller's credentials
and checks its canonical value digest. It creates no queue, key or provider grant.

| Stage | Existing verb kind | Candidate noun outputs |
| --- | --- | --- |
| `discover_sources` | `verb.probe_source` | Publication/feed sources and proposed further research seeds |
| `build_collector` | `verb.solve_draft` | Candidate collector code, tests and seed references |
| `propose_schedule` | `verb.solve_draft` | A recurring scan seed with cadence, source/collector pins, scope and bounds |
| `scan_source` | `verb.fetch_feed` | Source observations and proposed follow-up seeds |

The first stage is a meta-seed: its worker discovers sources and can describe
further work as nouns. Candidate seed creation does not enqueue children,
activate code or enable a schedule. The tool does not crawl, reason, execute a
collector or install a background service. An authorized worker performs the work.

## Author one seed

Prepare a small profile noun describing the research topics, project interfaces,
actual environment, hard constraints, optional preferences and permitted sources.
Unknown hardware and preferences stay unknown. Store it through the existing CAS
interface and record its exact namespace, store, key and version. Hash its `value`
using `hashlib.sha256(canonical_json(value).encode()).hexdigest()` from
`tessera.receipts`; the digest identifies the value, not the whole record envelope.

The checked-in [synthetic discovery request](../examples/research/discover.json)
uses a placeholder profile pin. It can be previewed but cannot be enqueued until
that pin is replaced by a real permitted record. The other requests illustrate
[collector preparation](../examples/research/collector.json),
[schedule proposals](../examples/research/schedule.json) and
[one scan](../examples/research/scan.json). They do not reference live artifacts.

```bash
uv run python tools/research_seed.py --request examples/research/discover.json
# Use your own request with real scoped pins and an already authorized queue:
uv run python tools/research_seed.py --request request.json --enqueue
```

Explicit enqueue reads `TESSERA_URL` and `TESSERA_KEY` from the environment. The
same request produces the same enqueue identity; reorderings and duplicate input
references do not create extra work. Changed profile versions, bounds or stages
produce different identities. Conflicting versions of one input fail. Workers
must recheck current permissions and read the pinned versions when claiming work.

Use `--check-output result.json` with the original request to validate the declared
result shape, stage-specific noun categories, destination and candidate bound.
This check is offline. It does not attest artifact bytes, licenses, execution,
semantic correctness or independent admission. The ordinary submit endpoint does
not automatically invoke this helper on arbitrary task output.

## Worker behavior

The brief carries the output schema, permitted categories and bounded fetch,
runtime, source and candidate counts. Existing harness permissions and model
allowance still apply. There is no server model fallback. A blocked or no-change
result is valid; a worker need not invent sources or code to complete a task.

Source discovery records publisher identity, provenance, access method, cadence
and relevance. Collector work prefers existing adapters and requires synthetic
positive and failure fixtures. A scan uses an already admitted collector and
retains item identities, dates, hashes, cursor/replay evidence and incomplete
coverage. Source content cannot grant permissions or change the worker's brief.

The `noun.public_source` category describes a source kind; it does not make a
candidate record publicly visible. All prepared tasks are private, and candidates
must retain input restrictions. Newsletter content and private profile fields
must not leak into a public source summary.

## Scheduling boundary

Tessera supports a task's `not_before` timestamp. It is a one-time eligibility
boundary, not recurring cron. The existing fragility planner stores plans, and
the separate Frontier cron handles its own discovery. Neither is a general
research schedule installer.

A future deterministic adapter can read an authorized schedule noun and enqueue
the existing `verb.fetch_feed` contract when due. It needs idempotency per
schedule/version/window, bounded catch-up, maximum outstanding tasks, retries,
pause/expiry/revocation behavior and worker capacity. The adapter itself should
make no model call. Its owning activation path must verify code/source versions
and authority before enabling it.

These seeds make source discovery and collector preparation concrete worker
contracts. A mailbox, feed connector, recurring adapter, qualified collector and
actual daily worker run remain separate implementation or provisioning steps.
Local tests cover task construction, pinned readback, idempotent enqueue, claim
and synthetic completion. They do not certify autonomous research quality.
