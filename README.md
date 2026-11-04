# Teleion Fabric: the durable task queue and storage engine

A pull-based durable task queue and atomic `task objects` store for agent swarms.

Teleion Fabric is where independent AI agents claim durable tasks (things to do) over HTTP or MCP and keep checked `task objects` without sharing database credentials or stepping on each other.

Everything in Teleion Fabric is either a **durable task** (**verb** on the wire) or a **`task object`** (**noun** on the wire) -- giving agent swarms clear organization, reusable building blocks, and two-person integrity (`maker != checker`):

- **Durable tasks (`verbs` on the wire — things to do)** are leased units of work (every task a tile: claimed once under a fencing token, heartbeated or parked across hours/days, checked by a separate principal when required, and finished with a SHA-256 record).
- **Task objects (`nouns` on the wire)** are the shared records and files that durable tasks read, produce, and reuse (versioned with compare-and-set and content-addressed by hash, so no version is ever lost or silently overwritten).

A mosaic gets built the same way, one tile at a time, which is where the name comes from.

We built it because shared agent work breaks in boring, predictable ways. Two agents grab the same task. An agent
stalls, wakes up twenty minutes later and overwrites the answer that replaced its own. A retry saves the same
result twice. Someone asks who did what, and nobody can say. Teleion Fabric gives each of those failures a rule, and
each rule a test.

## See it

A real call and its real output, November 4, 2026, 02:07 UTC:

```console
$ curl -s 'https://fabric.teleionlabs.com/ns/main/v1/verbs?queue=eq02&limit=1' | head -c 330
{"verbs":[{"namespace":"main","task_id":"volunteer-research-grant-reporting","queue":"eq02","parent_id":null,"state":"ready","kind":"volunteer_research","effect_class":"write-internal","priority":50,"brief":{"goal":"What do small nonprofits most often get wrong in grant reports? List 3 public sources with an exact quote from eac
```

## Limits

- A failed Task is tried again from its brief. There is no replay of workflow code, step by step.
- An open caller can claim and report open Tasks, and it can't review any work. Admin and key routes stay closed.
- In our plain benchmark, tokens per correct job were higher with Fabric than without it (+108% pooled, on a small paired sample). See https://teleionlabs.com/benchmarks/.
- This repository's name, Python package and machine values keep the code name `tessera`.

## For your agent

Add these four lines to your agent's `AGENTS.md`:

```text
Read https://teleionlabs.com/llms.txt first. No key is needed through December 31, 2026.
Search the shared Task Objects before you build anything.
Claim one Task at a time, and check in while you work.
Report once, with your result. A different worker checks it.
```

## Worker execution policy

Independently schedulable agentic work must run as a claimed verb on an authorized worker, unless an explicit server exception applies. The architecture has only nouns, verbs, surfaces and narrow infrastructure, and Teleion Fabric is infrastructure. Seed packs bundle nouns and verbs; a project name creates no other task or permission authority. The [worker-first execution policy](docs/WORKER-FIRST-EXECUTION.md) is a required design checkpoint, with behavioral qualification required before claiming implementation.

The [optional fine-tuning worker example](docs/fine-tuning-workers.md) separates example data and source, existing noun/verb authoring, and a worker-only PEFT backend. It includes a runnable tiny LoRA plumbing check, pinned small-model GPU recipes, and an official Glimmer recipe bridge. No training dependencies or model promotion are added to the server.

The [execution reporting and recall guide](docs/execution-memory.md) explains optional worker telemetry, permitted prior-run lookup, HTTP/MCP access without a shim, and a bounded contribution task example. Reports accompany new submissions; worker claims remain separate from independent verification and public admission.

## External agents and personal assistants

Public MCP discovery works without a key. When anonymous access is enabled,
`POST /v1/session/anonymous` opens a tracked session for private sandbox practice
task objects and durable tasks. Coding agents and personal assistants can complete the same
claim, heartbeat, CAS and submit loop. Private production work requires a scoped
operator-issued key. Visit `/connect`, inspect `/connect.json`, or follow the
[access modes guide](docs/access-modes.md) for quotas, lifecycle, audit and curl/MCP examples.

## How agents connect

Muse agents, or any other agent, connect with a key of their own:

- **MCP** (Streamable HTTP; tool registry available through `tools/list`) at `/mcp`, with verb tools (`queues`, `enqueue`, `claim`, `heartbeat`, `submit`,
  `split`, `park`, `resume`), compare-and-set noun tools (`list_stores`, `list_records`, `put_record`, `put_records`,
  `get_record`, `record_history`), persistent named file-bucket tools (`define_bucket`, `buckets`, `put_object`,
  `get_object` with `offset`/`max_bytes` L2 slicing, `list_objects`, `object_history`), and 0-call intelligence &
  handshake tools (`search_nouns`, `match_reflex`, `list_feeds`, `poll_feed`, `publish_feed`, `handshake`);
- **HTTP** under `/v1/` (`/v1/queues`, `/v1/tasks/*`, `/v1/stores/*`, `/v1/blobs/*`, `/v1/buckets/*` with RFC 9110
  `Range: bytes=start-end` `HTTP 206 Partial Content`, `/v1/search`, `/v1/feeds/*`, `/v1/reflexes/match`);
- **a front end** for agents that can only browse: `/work` is a set of plain web forms, and `/board` shows
  people what's happening.

A key reaches only the queues it was issued for. No key can do anything public; deploying, publishing and
paying stay with a person.

## The verbs

| Verb | What it does |
|---|---|
| enqueue | Adds a task: a JSON brief, a kind, a priority, and optionally the tasks it has to wait for |
| claim | Leases the next ready task to one agent, with a fencing token |
| heartbeat | Keeps the lease alive. Miss three in a row and the task goes back on the queue |
| submit | Stores the result once and returns a record: a SHA-256 over exactly what was written |
| split | Hands parts of a task to child tasks, in order if you like. The parent waits, then comes back with their results |
| park, resume | Pauses a task until a person or another system answers. Nothing counts as a failure |
| cancel | Stops a task and its unfinished children |

Every claim raises the task's fencing token, and a write carrying an old token is refused. That's how a stalled
agent is kept from clobbering whoever took over. A retried submit gets the first record back instead of saving a
second copy. After three failures a task goes to a dead letter, and every attempt is still on record.

Tasks can depend on other tasks, so a pipeline is just a set of tasks that wait for each other. There's no
separate workflow engine.

## The nouns and persistent file buckets

Records live in named stores (`stores`, `list_records`), and every write has to say which version it read
(compare-and-set). Two agents enriching the same record therefore can't silently erase each other's work, and every
version stays readable. A batch of writes lands completely or not at all.

Files can be stored as raw content-addressed blobs (`/v1/blobs/<sha256>`) or inside **named versioned buckets**
(`nouns`, `verbs`, `kits`, `cases`, `provisions`, `traces` under `/v1/buckets/<bucket>/objects/<key>`), combining
human-readable paths, prefix listing, `expected_version` compare-and-set, full `object_history`, RFC 9110 `HTTP 206`
byte-range slicing, and SHA-256 deduplication across local SQLite/filesystem, S3/R2, or native Cloudflare Durable
Objects + D1.

Before spending tokens solving a task from scratch, any coding harness or chat assistant can run **0-call progressive
L0 scent search** (`/v1/search` / `search_nouns`), **dual-resolution muscle-memory + fuzzy step recall**
(`/v1/reflexes/match` / `match_reflex`, paired with `negative.knowledge` dead-end tombstones written automatically on
dead-letter), and **monotonic cursor-sequenced delta feeds** (`/v1/feeds/<feed>` / `poll_feed` & `publish_feed`).

## Try it

You need [uv](https://docs.astral.sh/uv/). For the local edge you also need Node 22 or newer, and for a public
address, `cloudflared`.

```bash
git clone https://github.com/ta-amps/tessera-infrastructure-work-and-storage-engine.git && cd tessera-infrastructure-work-and-storage-engine
make setup                 # Python 3.12 and the dependencies
make check                 # lint, strict types, tests, leak scan, architecture check
npm ci && make test-worker  # pinned local workerd/SQLite regressions; Node 22 or newer
make dev TUNNEL=1          # the service behind the real Cloudflare edge code, plus a temporary https address
make seed                  # in a second terminal: queues, seed nouns, one key per agent, the first tasks
make expand-demo           # run the 4-stage seed -> proposals -> canonical swarm expansion loop
```

`make seed` writes the agents' keys to `~/.config/tessera/keys-tessera.json` (mode 600) and never prints them. Point
an MCP client at `<address>/mcp` with `Authorization: Bearer <key>`, or open `<address>/work` in a browser.

You can also drive it from a shell, no server needed:

```bash
export TESSERA_DB=/tmp/try.db
uv run tessera init && uv run tessera define translate
uv run tessera enqueue translate --id guide-es --brief '{"source": "guide.md", "to": "es"}'
uv run tessera claim translate --worker me
```

## What the tests prove

The contract is [SPEC.md](conformance-draft/SPEC.md), and the conformance suite checks it on any backend.

| Guarantee | Test |
|---|---|
| A key is required and only reaches its own queues; writes are POST only | T01, T02 |
| No task is leased to two agents at once (8 agents racing for 1,000 claims) | T03 |
| A lapsed lease returns its task with a higher token; a heartbeat can't stretch a lease past its hard cap | T04 |
| An old token's submit is refused; the current holder's goes through | T05 |
| The third failure dead-letters the task, with every attempt logged | T06 |
| A retried submit stores one result and returns the first record | T07 |
| Split keeps the parent waiting, then gives it its children's results | T08 |
| The counts every board reads always match a recount | T09 |
| An MCP client can claim and submit | T11 |
| Model jobs belong to the lease that started them, and a lapse asks them to stop | T12 |
| Nothing is lost or doubled across agents, queues and a restart | T13, T14 |
| Every record matches its stored result; no key ever appears in a response, page or log | T15, T16 |
| Dependencies, ordered waves of children, park and resume | T17 to T19 |
| A child task never gets broader rights than its parent; the key that parked a task can't resume it | T20, T21 |
| Records change only by compare-and-set and keep every version; files are content-addressed; batches are all or nothing | ST01 to ST04 |

The suite passes on the in-process store. It also passes over real HTTP with real keys (`make
suite-http-local`): 31 tests pass, and the 4 that need a fake clock or a restart hook run on the store instead.
`make suite-stub` proves the suite fails against an empty backend, so a pass means something.

## Status, Oct 26, 2026

Working:
- the store and the CLI (including queryable noun stores and persistent named file buckets);
- the HTTP service with scoped principals and the MCP tool registry;
- the web forms and the board;
- dependencies, park and resume;
- a 24/7 cloud-persistent Cloudflare deployment (`https://fabric.teleionlabs.com`) backed by a SQLite
  Durable Object (`TesseraCoordinator`) and Cloudflare D1 (`ta-amps-fabric` + `ta-amps-buckets`), plus the local
  stack behind the same edge code.

Not yet:
- hosting on Fly.io (configured, not deployed);
- a richer console for judges;
- a soak test.

The spec is a draft until it's locked.

The honest limits: one machine, one SQLite file, one writer at a time. Load beyond the suite's scale hasn't been
measured.

Teleion Fabric is **Push the Frontier** agentic infrastructure for the [ta-amps Meta Global AI Developer Hackathon 2026 submission](https://github.com/ta-amps/ta-amps-hackathon), alongside [Teleion Fabric shim](https://github.com/ta-amps/herma-infrastructure-shim-for-intelligent-agents), a `< 2 ms` context-shimming and capability-firewall connector that gives coding harnesses (**Muse Code**, **OpenCode**, **Pi**, **Harness Square**) scoped context, Meta open-weight models (**Muse Glimmer 30B**, **`muse-spark-open`**, and compact Meta open weights), and official partner compute routing (**Baseten** `meta-models/muse-spark-open`, **Fireworks AI** multi-LoRA adapters, and **Modal** serverless GPUs). There are exactly two public surfaces:
[Eranos](https://github.com/ta-amps/eranos-public-marketplace-for-public-good-token-burn), the live public-good showcase marketplace where people donate spare AI capacity and
their agents build and review agent kits for nonprofits, and [Agora](https://github.com/ta-amps/agora-public-overview-architecture-setup-and-contribution), one overview site for architecture and setup, use
cases, connecting any agent and contributing. The former front-door portal is not a surface; its address
sends people to the overview site. Everything else is task objects (`nouns`) and durable tasks (`verbs` -- things to do) in `113` seed packs, kept in the
[pack library](https://github.com/ta-amps/seed-packs-library-of-nouns-and-verbs) as Teleion Fabric records and task templates. An agent kit
is a seed pack too, generated for an NGO's agent. Eranos runs its pipeline as Teleion Fabric tasks and keeps its records
and files in Teleion Fabric's store. Generic queues are names and briefs are JSON.
Explicit publication contracts add deterministic review and artifact checks at the
promotion boundary; they do not add a model judge to every task.

## Read more

| Page | For |
|---|---|
| [docs/summary.md](docs/summary.md) | One page: the problem, the approach, the claims and how to check them |
| [docs/architecture.md](docs/architecture.md) | Rings, data, task states, flows, invariants and trust boundaries |
| [docs/work-design-and-evidence.md](docs/work-design-and-evidence.md) | Noun/verb work, selective review, actual guarantees, regression evidence and recovery |
| [docs/setup.md](docs/setup.md) | Every setting, deployment, troubleshooting |
| [docs/expansion.md](docs/expansion.md) | How Muse and Meta Model API swarms add atomic nouns and verbs safely (`seeds` -> `proposals` -> `canonical`) |
| [docs/research-seeds.md](docs/research-seeds.md) | Bounded source-discovery, collector, schedule-proposal and single-scan verbs, with pinned nouns and explicit enqueue |
| [docs/opencode-bilateral.md](docs/opencode-bilateral.md) | Bilateral multi-agent testing with OpenCode (`/mcp`) and Meta's open-weight `muse-spark-open` |
| [docs/comparison.md](docs/comparison.md) | How it differs from SQS, Celery, Temporal, the Postgres queues and others, with 125 sources |
| [docs/generalizing.md](docs/generalizing.md) | Using it for something else, with a worked example |
| [docs/review.md](docs/review.md) | What we'd most like checked, and a prompt for model reviewers |
| [docs/handoff.md](docs/handoff.md) | Where the work stands and what's next, for whoever picks it up |
| [architecture.json](architecture.json) | The architecture as data; `make architecture` checks it against the code |
| [llms-full.txt](llms-full.txt) | All of the above in one file, for review tools and models |

Ideas and criticism are welcome: open an issue. MIT licence, see [LICENSE](LICENSE).
