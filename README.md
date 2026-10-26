# Tessera

**Atomic work queue and atomic storage for agent swarms.** Every task is a tile: claimed once,
finished once, receipted.

When several AI agents share a backlog, the failures are always the same: two agents take the same
task, a stalled agent's late answer overwrites a newer one, a retry stores a result twice, and nobody
can say who did what. Tessera answers each of those with a rule a test can check: leases with
heartbeats, fencing tokens, idempotent submits, dead letters, receipts, and one counts record that
every board reads.

Tessera is the work fabric behind [Shamash](https://github.com/ta-amps/shamash), our entry in the
Meta Global AI Developer Hackathon (Indie track). Shared context for both lives in
[hackathon-context](https://github.com/ta-amps/hackathon-context).

## Status (Oct 26, 2026)

| Part | State | Built by |
|---|---|---|
| [SPEC.md](conformance-draft/SPEC.md), the contract | Draft v0.1, waiting for Taylor's approval (L1) | Claude Code |
| Conformance suite T01 to T16, plus storage tests ST01 to ST04 | Draft (L2); 22 tests pass on the in-process store; 5 service tests wait for the service | Claude Code |
| In-process store (`tessera.Store`) and the `tessera` CLI | Working; the reference implementation the suite is proven against | Claude Code |
| HTTP client (`tessera.Client`) | Written to the spec; unit-tested against a mock | Claude Code |
| HTTP service, keys, reaper loop, MCP door, model-job lane, board, deploy | Briefs L6 to L24 in [briefs/](briefs/) | Muse Code workers, claiming briefs through Tessera itself |

"Meta-built" is literal here: the networked service is built by Muse Code workers from the spec and a
suite they can run but not edit, and the event log of that build ships with the repo.

## Guarantees

Each guarantee is a conformance test (see [SPEC.md §10](conformance-draft/SPEC.md)).

| Guarantee | Test |
|---|---|
| No task is ever leased to two workers at once (8 workers, 1,000 claims) | T03 |
| A lease lapses after missed heartbeats or its hard cap, and the task comes back with a higher fencing token | T04 |
| A stale holder's submit is refused; the current holder's succeeds | T05 |
| The third failure moves a task to dead letter, with every attempt logged | T06 |
| A retried submit with the same idempotency key returns the first receipt and stores one result | T07 |
| Split keeps the parent waiting until every child is resolved, then hands it back with the children's results | T08 |
| The counts record always equals a recount of the tasks table | T09 |
| Health reports the database, the reaper's last run and queue depths | T10 |
| Model jobs are tied to the lease that started them; a lapse asks them to stop | T12 |
| Nothing is lost or doubled across workers, queues and restarts | T13, T14 |
| Every receipt hash matches its stored result | T15 |
| Records change only by compare-and-set and keep every version; blobs are content-addressed; batches are all-or-nothing | ST01 to ST04 |
| Service only: keys required, POST-only writes, MCP door, no secret leaks | T01, T02, T11, T16 |

## Quickstart

Needs [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```bash
make setup            # uv sync
make check            # lint, types, unit tests, conformance suite, leak scan

export TESSERA_DB=.tessera/demo.db
uv run tessera define build --heartbeat 60 --misses 3 --lease 3600
uv run tessera enqueue build --brief '{"title": "Write the keys module", "done_when": "make suite"}'
uv run tessera claim build --worker muse-01          # prints the lease, with its fencing token
uv run tessera heartbeat <task_id> --token 1
uv run tessera submit <task_id> --token 1 --output '{"commit": "abc123"}' --idempotency-key k-1
uv run tessera counts
uv run tessera events
```

Several processes can share one database file, so people and agents can claim work from the same
queue before the HTTP service exists.

```python
from tessera import Store, QueueConfig, TaskSpec

store = Store(".tessera/demo.db")
store.define_queue(QueueConfig(name="aq02"))
store.enqueue(TaskSpec(queue="aq02", brief={"case_id": "C-FS-PH-03"}, task_id="C-FS-PH-03"))
lease = store.claim("aq02", worker="intake-1")
receipt = store.submit(
    lease.task_id,
    lease.fencing_token,
    {"evidence": ["E-1"]},
    idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
)
```

## Defaults

| Setting | Default | Why |
|---|---|---|
| Heartbeat | every 60 s; 3 misses lapse the lease | The reaper acts within minutes |
| Lease hard cap | 600 s (up to 3,600 s for build queues) | Nothing holds a task forever |
| Attempts | 3, then dead letter | Stops retry storms |
| Backoff | jittered exponential, 30 s base, 15 min cap | Spreads retries |
| Output size | 1 MiB per submit; larger outputs go to blobs | Matches Meta's background-body cap |
| Task depth | 4 levels of split | Stops runaway decomposition |

## Layout

```
SPEC.md -> conformance-draft/SPEC.md   the contract (draft until Taylor locks it)
conformance-draft/                     the suite while Taylor reviews it
src/tessera/                           store, models, client, CLI
tests/                                 unit tests, hook and leak-scan tests
briefs/                                task briefs for Muse Code workers (L6 to L24)
tools/muse_worker.py                   claims a brief, runs Muse Code headless, submits a receipt
harness/                               work-harness design and starting config (W tasks)
docs/                                  architecture, operations, making-of
deploy/                                Fly.io machine with a volume; Cloudflare notes
```

## Safety

- The suite runs locally against the in-process store and against services you own; it never touches
  anyone else's queue.
- Keys are stored only as hashes (brief L7). Writes are POST-only. Logs and responses never carry keys
  (T16).
- Agents follow [AGENTS.md](AGENTS.md); Claude Code's guard hook enforces the hard rules in every
  permission mode.

## Licence

MIT. See [LICENSE](LICENSE).
