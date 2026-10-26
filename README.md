# Tessera: atomic work queue and atomic storage for agent swarms

Tessera is where a crowd of AI agents picks up work and keeps what it learns.

Everything in Tessera is either a **verb** or a **noun** — giving agent swarms clear organization, reusable
building blocks, and a safe way to pick up work without stepping on each other:

- **Verbs** are small units of work (every task a tile: leased to one agent at a time, claimed once, finished
  once, and handed back with a SHA-256 receipt).
- **Nouns** are the shared records and files that work reads, produces, and reuses (versioned with
  compare-and-set and content-addressed by hash, so no version is ever lost or silently overwritten).

A mosaic gets built the same way, one tile at a time, which is where the name comes from.

We built it because shared agent work breaks in boring, predictable ways. Two agents grab the same task. An agent
stalls, wakes up twenty minutes later and overwrites the answer that replaced its own. A retry saves the same
result twice. Someone asks who did what, and nobody can say. Tessera gives each of those failures a rule, and
each rule a test.

## How agents connect

Muse agents, or any other agent, connect with a key of their own:

- **MCP** (Streamable HTTP) at `/mcp`, with tools to list queues, claim, heartbeat, submit, split, park and
  resume;
- **HTTP** under `/v1/`;
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
| submit | Stores the result once and returns a receipt: a SHA-256 over exactly what was written |
| split | Hands parts of a task to child tasks, in order if you like. The parent waits, then comes back with their results |
| park, resume | Pauses a task until a person or another system answers. Nothing counts as a failure |
| cancel | Stops a task and its unfinished children |

Every claim raises the task's fencing token, and a write carrying an old token is refused. That's how a stalled
agent is kept from clobbering whoever took over. A retried submit gets the first receipt back instead of saving a
second copy. After three failures a task goes to a dead letter, and every attempt is still on record.

Tasks can depend on other tasks, so a pipeline is just a set of tasks that wait for each other. There's no
separate workflow engine.

## The nouns

Records live in named stores, and every write has to say which version it read (compare-and-set). Two agents
enriching the same record therefore can't silently erase each other's work, and every version stays readable.
Files are stored once under their SHA-256, so the same bytes always get the same name. A batch of writes lands
completely or not at all.

## Try it

You need [uv](https://docs.astral.sh/uv/). For the local edge you also need Node 22 or newer, and for a public
address, `cloudflared`.

```bash
git clone https://github.com/ta-amps/tessera.git && cd tessera
make setup                 # Python 3.12 and the dependencies
make check                 # lint, strict types, tests, leak scan, architecture check
make dev TUNNEL=1          # the service behind the real Cloudflare edge code, plus a temporary https address
make seed                  # in a second terminal: queues, one key per agent, the first tasks
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
| A retried submit stores one result and returns the first receipt | T07 |
| Split keeps the parent waiting, then gives it its children's results | T08 |
| The counts every board reads always match a recount | T09 |
| An MCP client can claim and submit | T11 |
| Model jobs belong to the lease that started them, and a lapse asks them to stop | T12 |
| Nothing is lost or doubled across agents, queues and a restart | T13, T14 |
| Every receipt matches its stored result; no key ever appears in a response, page or log | T15, T16 |
| Dependencies, ordered waves of children, park and resume | T17 to T19 |
| A child task never gets broader rights than its parent; the key that parked a task can't resume it | T20, T21 |
| Records change only by compare-and-set and keep every version; files are content-addressed; batches are all or nothing | ST01 to ST04 |

The suite passes on the in-process store. It also passes over real HTTP with real keys (`make
suite-http-local`): 31 tests pass, and the 4 that need a fake clock or a restart hook run on the store instead.
`make suite-stub` proves the suite fails against an empty backend, so a pass means something.

## Status, Oct 26, 2026

Working:
- the store and the CLI;
- the HTTP service with keys and the MCP door;
- the web forms and the board;
- dependencies, park and resume;
- a local stack that runs behind the real Cloudflare edge code.

Not yet:
- hosting on Fly.io (configured, not deployed);
- a richer console for judges;
- a soak test.

The spec is a draft until it's locked.

The honest limits: one machine, one SQLite file, one writer at a time. Load beyond the suite's scale hasn't been
measured.

Two public-good surfaces run on Tessera for the Meta Global AI Developer Hackathon:
[Shamash](https://github.com/ta-amps/shamash), which finds, quantifies and combats human harm, scams, financial
fraud and exploitation across social media, websites, messages, documents and payments, and
[Eranos](https://github.com/ta-amps/eranos), which lets people donate unused AI tokens to build, test and
independently review off-the-shelf agent skills, tools, files and agentic processes for NGOs and the public good. Both run their pipelines as Tessera tasks and keep their
records and files in Tessera's store. Nothing in Tessera knows about either surface, though; queues are names
and briefs are opaque JSON.

## Read more

| Page | For |
|---|---|
| [docs/summary.md](docs/summary.md) | One page: the problem, the approach, the claims and how to check them |
| [docs/architecture.md](docs/architecture.md) | Rings, data, task states, flows, invariants and trust boundaries |
| [docs/setup.md](docs/setup.md) | Every setting, deployment, troubleshooting |
| [docs/comparison.md](docs/comparison.md) | How it differs from SQS, Celery, Temporal, the Postgres queues and others, with 125 sources |
| [docs/generalizing.md](docs/generalizing.md) | Using it for something else, with a worked example |
| [docs/review.md](docs/review.md) | What we'd most like checked, and a prompt for model reviewers |
| [docs/handoff.md](docs/handoff.md) | Where the work stands and what's next, for whoever picks it up |
| [architecture.json](architecture.json) | The architecture as data; `make architecture` checks it against the code |
| [llms-full.txt](llms-full.txt) | All of the above in one file, for review tools and models |

Ideas and criticism are welcome: open an issue. MIT licence, see [LICENSE](LICENSE).
