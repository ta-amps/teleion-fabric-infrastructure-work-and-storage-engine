# Teleion Fabric: summary

**In one sentence:** Teleion Fabric is an atomic work queue and atomic store where everything is either a **verb** (every
task a tile: claimed once, finished once, receipted) or a **noun** (versioned records and content-addressed files),
giving AI agent swarms clear organization, reusability, and a safe way to pick up shared work.

## The problem

When several agents work from one backlog, the same failures repeat. Two agents take the same task. An agent
that stalled comes back and overwrites a newer answer. A retry stores a result twice. A board shows numbers
nobody can reconcile. Afterwards nobody can say which agent did what. Agents are slower and less reliable
than ordinary workers, and many are web-based tools that can't run a client library, which makes all of this
more likely.

## The approach

Every guarantee is a rule with a test, and every write path ends in one SQLite transaction:

- **Leases** with heartbeats and a hard cap. A lapsed lease returns its task to the queue.
- **Fencing tokens.** Every claim raises the task's token. A write carrying an old token is refused, so a
  stalled agent can't overwrite the agent that took over.
- **Idempotent submits with records.** A retried submit returns the first record; the record is a SHA-256
  over the task, token, outcome and output.
- **Retries with jittered backoff, then a dead letter**, with every attempt logged.
- **Task graphs without a workflow engine.** A task can split into children (which may run in ordered
  waves) and come back with their results; any task can depend on others and wait until they resolve; and
  a task can be parked while it waits for a person or another system, then resumed with that input.
- **One counts record**, updated in the same transaction as every state change, and an **append-only event
  log**. Boards read the counts; the event log is the audit trail.
- **Atomic storage:** versioned records changed only by compare-and-set, content-addressed blobs, named versioned file
  buckets with RFC 9110 `HTTP 206` byte-range slicing, atomic batches, 0-call progressive L0 scent search
  (`/v1/search`), monotonic cursor delta feeds (`/v1/feeds/*`), and 0-call reflex recall (`/v1/reflexes/match`) paired
  with `negative.knowledge` dead-end tombstones.
- **Four ways in, one set of keys:** HTTP, an MCP server (Streamable HTTP), plain web forms for agents that
  can only browse, and a CLI on a shared SQLite file. Keys are scoped to queues, kinds and effect classes,
  stored only as hashes, and no key may hold the `public` effect class.

Teleion Fabric knows nothing about any domain. Queues are names, tasks carry opaque JSON briefs, and it never calls a
model itself: the agent holding the lease does, and Teleion Fabric records the job against that lease.

## What works today (Oct 26, 2026)

| Part | Status | How to check |
|---|---|---|
| Spec v0.2 and conformance suite (T01 to T19, ST01 to ST04) | Built; spec still a draft | `make suite BACKEND=store`, `make suite-stub` |
| In-process store, CLI, HTTP client | Built | `make test` |
| HTTP service, keys, MCP door, agent pages, web forms, reaper | Built | `make suite-http-local` runs the suite over HTTP with real keys: 29 pass; 4 need a fake clock or a restart hook and run on the store |
| Local stack behind the real Cloudflare edge code, optional public tunnel | Built | `make dev TUNNEL=1`, then `make seed` |
| Hosting on Fly.io behind a Cloudflare Worker | Configured, not deployed | `deploy/bootstrap-hosting.sh --dry-run` |
| Making-of export, model-job runner, records gate, board v2, soak test | Planned (open briefs) | `briefs/` |

## Claims

The claims, their status and their evidence are listed in [architecture.json](../architecture.json) and
checked by `make architecture`. The ones that matter most:

1. No task is leased to two workers at once (T03: 8 workers race for 1,000 claims).
2. A stale holder can't overwrite the current one (T05).
3. A retried submit stores one result and returns the first record (T07).
4. Counts always equal a recount (T09), and nothing is lost or doubled across a restart (T14).
5. No key appears in any response, page or log (T16).

## Limits

Single node, single SQLite file, one writer at a time. The claim rate limiter lives in process memory. No
replication or failover. Not load-tested beyond the suite's scale (8 workers, 1,000 claims; 50 tasks across
restarts). The spec is still a draft. More in [review.md](review.md).

## Read next

[architecture.md](architecture.md) · [setup.md](setup.md) · [comparison.md](comparison.md) ·
[generalizing.md](generalizing.md) · [decisions.md](decisions.md) · [glossary.md](glossary.md) ·
[review.md](review.md) · [the spec](../conformance-draft/SPEC.md)
