# Tessera

**Atomic work queue and atomic storage for agent swarms.** Every task is a tile: claimed once,
finished once, receipted.

When many AI agents share a backlog, the same failures repeat: two agents take the same task, a
stalled agent's late answer overwrites a newer one, a retry stores a result twice, and nobody can say
who did what. Tessera answers each with a rule a test checks: leases with heartbeats, fencing tokens,
idempotent submits, dead letters, receipts, one counts record that every board reads, and storage that
changes only by compare-and-set.

Tessera is a general work backend: any project defines queues, issues one key per agent, and lets
agents claim, split and finish tasks over HTTP, MCP or plain web forms. It knows nothing about any
domain and never calls a model itself. Its first project is [Shamash](https://github.com/ta-amps/shamash),
our entry in the Meta Global AI Developer Hackathon, whose research, drafting, review and build work Muse
agents pick up from Tessera's queues.

## Review it

| Read | For |
|---|---|
| [docs/summary.md](docs/summary.md) | One page: the problem, the approach, what works, the claims |
| [docs/architecture.md](docs/architecture.md) | Components, data, task states, flows, invariants, trust boundaries, failure handling |
| [docs/setup.md](docs/setup.md) | Install, run, connect agents, every setting, deploy, troubleshooting |
| [docs/comparison.md](docs/comparison.md) | How it differs from established queues and workflow engines, with sources |
| [docs/generalizing.md](docs/generalizing.md) | What is domain-neutral, the extension points, a worked example in another domain |
| [docs/decisions.md](docs/decisions.md), [docs/glossary.md](docs/glossary.md) | Why it is built this way; one term, one meaning |
| [docs/review.md](docs/review.md) | What we most want checked, claims to try to break, a prompt for model reviewers |
| [architecture.json](architecture.json) | The architecture as data; `make architecture` proves it still matches the code |
| [llms-full.txt](llms-full.txt) | Everything above in one file, for review tools and models |

## Status (Oct 26, 2026)

| Part | State |
|---|---|
| [SPEC.md](conformance-draft/SPEC.md), the contract | Draft v0.1, waiting for Taylor's approval (L1) |
| Conformance suite: T01 to T19, ST01 to ST04 | Passes on the in-process store; over HTTP against a real local service with real keys, 29 pass and 4 are skipped because they need a fake clock (T04 hard cap, T06 backoff, T19 park timeout) or a restart hook (T14) (`make suite-http-local`) |
| Store, CLI and HTTP client | Working |
| HTTP service, keys, MCP door, agent pages, web forms, reaper | Working (briefs L6 to L9, L12, L14, L15, L17, L24, built by Claude Code on Oct 26; see `briefs/done/`) |
| Local stack behind the real Cloudflare edge code, optional public tunnel | Working (`make dev`, `make dev TUNNEL=1`) |
| Hosted on Fly.io behind Cloudflare | Configured, not deployed (`deploy/bootstrap-hosting.sh`, brief L20) |
| Making-of export, model-job runner, receipts gate, board v2, soak, Shamash on the service | Open briefs for Muse Code: L11, L13, L16, L18, L19, L21, L23 |

## Connect your agents (local, no accounts needed)

```bash
make setup                 # uv sync, Python 3.12
make dev TUNNEL=1          # service + local Cloudflare edge + a temporary public https address
make seed                  # in another terminal: queues, one key per agent, first tasks
```

`make seed` writes the keys to `~/.config/tessera/keys-dev.json` (mode 600) and never prints them.
Give each agent its own key and the public address that `make dev` printed:

- **MCP** (Streamable HTTP): `<address>/mcp` with `Authorization: Bearer <key>`. Tools: `queues`,
  `claim`, `heartbeat`, `submit`, `split`, `park`, `resume`.
- **HTTP**: `POST <address>/v1/tasks/claim`, `/v1/tasks/heartbeat`, `/v1/tasks/submit`,
  `/v1/tasks/split`, `/v1/tasks/park`, `/v1/tasks/resume`, `/v1/tasks` (to queue new work, with
  `depends_on` for joins).
- **Browser-only agents**: `<address>/work`, keyed forms that ask for the key on every step.
- **People**: `<address>/connect`, `<address>/board`, `<address>/llms.txt`.

The quick tunnel is temporary and changes address each run; for a permanent address, see `deploy/`.

## Guarantees

| Guarantee | Test |
|---|---|
| Keys are required and scoped to queues; writes are POST only | T01, T02 |
| No task is ever leased to two workers at once (8 workers, 1,000 claims) | T03 |
| A lease lapses after missed heartbeats or its hard cap; the task returns with a higher fencing token | T04 |
| A stale holder's submit is refused; the current holder's succeeds | T05 |
| The third failure dead-letters a task, every attempt logged | T06 |
| A retried submit returns the first receipt and stores one result | T07 |
| Split keeps the parent waiting, then hands it back with the children's results | T08 |
| A task can depend on others: it waits until each is done, dead or cancelled, then sees their results | T17 |
| Children can run in ordered waves | T18 |
| A task can be parked while it waits for outside input, with no failure counted, and resumed with that input | T19 |
| The counts record always equals a recount | T09 |
| Health reports the database, the reaper's last run and queue depths | T10 |
| An MCP client can claim and submit | T11 |
| Model jobs are tied to the lease that started them; a lapse asks them to stop | T12 |
| Nothing is lost or doubled across workers, queues and restarts | T13, T14 |
| Every receipt hash matches its stored result | T15 |
| No key appears in any response, page or log | T16 |
| Records change only by compare-and-set and keep every version; blobs are content-addressed; batches are atomic | ST01 to ST04 |

Beyond the suite: only the key that claimed a task may change it, claims are rate-limited per key, no
key can hold the `public` effect class, and with `TESSERA_EDGE_SECRET` set the service answers only
requests that came through the edge.

## Defaults

| Setting | Default |
|---|---|
| Heartbeat | every 60 s; 3 misses lapse the lease |
| Lease hard cap | 600 s (up to 3,600 s for long queues) |
| Attempts | 3, then dead letter |
| Backoff | jittered exponential, 30 s base, 15 min cap |
| Claims | 60 a minute per key |
| Keys | 14 days, stored only as SHA-256 |
| Output size | 1 MiB per submit; larger outputs go to blobs |
| Task depth | 4 levels of split |

## Layout

```
conformance-draft/   SPEC.md and the suite (draft until Taylor locks it)
src/tessera/         store (_base, _tasks, _jobs, _observe, _storage, _keys), service, mcp_door, pages, client, cli
tests/               service, tools, guard-hook and leak-scan tests
scripts/             dev_stack.sh, suite_http_local.py, and the shared checks: check_public.py,
                     build_index.py, check_architecture.py
tools/               seed.py, load_briefs.py, muse_worker.py
briefs/              open briefs for Muse Code; briefs/done/ for finished ones
docs/                summary, architecture, setup, comparison, generalizing, decisions, glossary,
                     review, operations, making-of
deploy/              Cloudflare edge Worker, Fly.io configs, bootstrap-hosting.sh and hosting.env
architecture.json    the architecture as data, checked by `make architecture`
harness/             work-harness design and starting config
```

## Licence

MIT. See [LICENSE](LICENSE).
