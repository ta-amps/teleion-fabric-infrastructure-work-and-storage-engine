# Tessera: generalizing it

Tessera was built for one hackathon project, but nothing in `src/tessera/` knows about that project or any
other domain. This page lists what is neutral, what is specific, the extension points, and a worked example
in a second domain that you can run as written.

## Domain-neutral parts

| Part | Why it carries over |
|---|---|
| Queues | Just names with lease and retry settings; define them at run time (`tessera define`, `POST /v1/queues/define`) |
| Tasks | An opaque JSON brief plus a free-form `kind`, a priority and an effect class; Tessera never reads the brief |
| Workers | Any string identity: an agent, a script, a person |
| Leases, fencing, receipts, retries, dead letters, split and merge | Properties of work in general, specified in [SPEC.md](../conformance-draft/SPEC.md) and checked by the suite |
| Storage | Any JSON value under any store name and key (compare-and-set, full history); any bytes as blobs |
| Doors | HTTP, MCP and web forms speak only in queues, tasks and leases |
| Counts, events, health | Generic per-queue numbers and an append-only log |

## Domain-specific parts, and how to change them

| Part | What is specific | To adapt it |
|---|---|---|
| Effect classes | A fixed set shaped by coding work: `read`, `write-internal`, `write-repo`, `public` | Read them as "reads only", "changes internal state", "changes a shared artifact", "has a public effect". Renaming means changing `models.py` and the spec together |
| Model-job statuses | Follow Meta Model API background responses (`queued`, `in_progress`, `completed`, `failed`, `cancelled`, `incomplete`) | Any provider with job ids fits; the worker maps its provider's statuses before reporting |
| Worker tooling | `tools/muse_worker.py` runs Muse Code on this repo's briefs; `tools/load_briefs.py` loads them | Replace with your own loop: claim, heartbeat, do the work, submit (about five client calls) |
| Edge | A Cloudflare Worker in front of Fly.io | Any reverse proxy that adds the `x-edge-auth` header (nginx, Caddy) gives the same origin lock |
| Page text | The agent pages link to this repo | `ServiceSettings.repo_url` and `TESSERA_PUBLIC_URL` |

## Extension points

| Extension point | What you add | Where | Checked by |
|---|---|---|---|
| A new backend (Postgres, D1, another language) | A class that meets the `Backend` protocol | `src/tessera/backend.py` | The suite: `TESSERA_BACKEND_FACTORY=yourmodule:factory make suite` |
| A new kind of work | A queue definition and a `kind` string | At run time, no code | `make suite` already covers queue semantics |
| A new kind of worker | Any HTTP or MCP client, or a person on `/work` | Outside Tessera | Keys scope it to its queues, kinds and effect classes |
| A new model provider | Job registration with the provider's job id | In the worker | T12 semantics: jobs tied to the lease |
| A new storage namespace | A store name in `/v1/stores/{store}` | At run time | ST01 to ST04 |
| A new host | `deploy/fly/Dockerfile` runs anywhere with a persistent volume | `deploy/` | `GET /v1/health` |

## Worked example: a translation pipeline

A long guide is translated by several agents: one splits it into sections, others translate sections, the first
merges. Nothing below is specific to translation; swap the brief for any other work. These commands were run
on Oct 26, 2026 against a fresh database, and the outputs shown are from that run.

```bash
export TESSERA_DB=/tmp/translate.db
uv run tessera init
uv run tessera define translate --heartbeat 30 --lease 900
uv run tessera enqueue translate --id guide-es --kind translate --effect write-internal \
  --brief '{"source": "guide.md", "to": "es"}'

# Agent A claims the guide (fencing token 1) and splits it into two sections
uv run tessera claim translate --worker agent-a
uv run tessera split guide-es --token 1 --children '[{"brief": {"section": 1}}, {"brief": {"section": 2}}]'

# Agent B claims each section and submits a translation (each gets a receipt)
uv run tessera claim translate --worker agent-b
uv run tessera submit <child id> --token 1 --output '{"text": "sección 1"}' --idempotency-key <child id>-1

# When both children are done, the parent comes back with their results (token 2)
uv run tessera claim translate --worker agent-a
uv run tessera submit guide-es --token 2 --output '{"merged": true}' --idempotency-key guide-es-merge
```

What the run showed: the parent came back with fencing token 2 and both children `done`; submitting the merge
a second time with the same idempotency key returned the first receipt with `replayed: true`; and the counts
record ended at `{"done": 3}` for the queue, with nothing ready, leased or waiting.

The same flow works over HTTP (`/v1/tasks/claim`, `/split`, `/submit`) and over MCP (tools `claim`, `split`,
`submit`), with one key per agent scoped to the `translate` queue.

## A second sketch: a research swarm

Agents on the web connect over MCP with keys scoped to `research` and the `store` scope. Each claims a
question, writes findings to a record (`/v1/stores/findings/records/<question>`) with compare-and-set so two
agents can't clobber each other, stores source PDFs as blobs by hash, and submits a summary. A reviewer agent
works a `review` queue fed by `split`. The board shows progress; the event log shows who did what.

## What would not carry over

- **Scale beyond one machine.** One SQLite file and one writer at a time. The suite exercises 8 concurrent
  workers and 1,000 claims; larger loads haven't been measured (brief L21 is the soak test). For a
  high-throughput job system, a Postgres backend would have to pass the same suite first.
- **Long-lived workflows with timers and signals.** Tessera has tasks, leases and split; a workflow engine has
  durable timers, signals and replay. See [comparison.md](comparison.md).
- **Side effects outside Tessera.** Fencing protects writes that come back to Tessera. A worker that also
  writes to another system (a repo, a payment API) should pass its fencing token or an idempotency key there
  too, or a stalled worker can still act twice outside.
