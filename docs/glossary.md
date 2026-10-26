# Tessera: glossary

One term, one meaning. The spec ([SPEC.md §1](../conformance-draft/SPEC.md)) is the authority; this page adds
the terms the code and docs use around it.

| Term | Meaning | In the code |
|---|---|---|
| Queue | A named list of tasks with its own heartbeat, lease cap, attempts and backoff | `queues` table, `QueueConfig` |
| Task | One unit of work: a JSON brief, a kind, an effect class, a priority, a state | `tasks` table, `TaskSpec`, `TaskView` |
| Brief | The task's JSON payload. Tessera stores it and never reads it | `TaskSpec.brief` |
| Kind | A free-form label for the type of work; keys and claims can filter on it | `TaskSpec.kind` |
| Effect class | What a task may change: `read`, `write-internal`, `write-repo` or `public` | `EffectClass` |
| Worker | Whoever claims a task: an agent, a script, a person | `worker` field on claims |
| Lease | The right to work on one task until it lapses; at most one live lease per task | `leases` table, `Lease` |
| Live lease | A lease whose token is current and whose expiry and hard expiry are in the future | `_tasks.py` |
| Heartbeat | A call that extends a live lease, never past its hard cap | `heartbeat` |
| Hard cap | The longest a single lease may last, heartbeats or not (`lease_seconds`) | `hard_expires_at` |
| Lapse | A lease ending because heartbeats stopped or the hard cap passed; counts as a failure | event `lease_lapsed` |
| Fencing token | A per-task integer that rises on every claim; writes carry it and stale tokens are refused | `fencing_token` |
| Attempt, failure | Each claim is an attempt; a failed submit, a lapse or a cancelled job is a failure | `attempts`, `failures` |
| Backoff | The jittered, growing delay before a failed task is ready again | `not_before` |
| Dead letter | The terminal state after `max_attempts` failures | state `dead` |
| Split | Ending a lease by creating child tasks; the parent waits | `split`, state `waiting` |
| Merge | The parent's next claim, which carries its children's states and outputs | `Lease.children` |
| Submit | Ending a lease with an outcome and output | `submit` |
| Idempotency key | A caller-chosen key per submit; a repeat returns the first receipt | `Idempotency-Key` header |
| Receipt | Proof of a submit: SHA-256 over canonical JSON of task, token, outcome and output | `receipts.py`, `Receipt` |
| Counts record | Per-queue totals by state, updated in the same transaction as each change | `counts` table |
| Recount | Recomputing the totals from `tasks`; must equal the counts record | `?recount=true` |
| Event | One entry in the append-only log, with a rising `seq` | `events` table |
| Reaper | The loop that processes lapsed leases; claims also reap their own queue first | `reap`, `TESSERA_REAPER_SECONDS` |
| Model job | A model call started by the lease holder and registered against its lease | `jobs` table |
| Record | A versioned JSON value under a store name and key, changed only by compare-and-set | `records`, `record_heads` |
| Compare-and-set | A write that succeeds only if the head version is still the expected one | `put_record(expected_version)` |
| Blob | Bytes stored under their SHA-256 | `blobs` table |
| Batch | Several record writes applied in one transaction, all or none | `put_records` |
| Key | A bearer secret, stored as a hash, scoped to queues, kinds, effect classes and `admin` or `store` | `_keys.py` |
| Scope | What a key may touch: its queues, kinds, effect classes and `admin` or `store` | `KeyGrant` in `_keys.py` |
| Backend | Anything that meets the `Backend` protocol and passes the suite | `backend.py` |
| Store | The in-process reference backend | `store.py` |
| Service | The HTTP process in front of the store | `service.py` |
| MCP door | The MCP server at `/mcp` | `mcp_door.py` |
| Agent door | The pages for agents and people: `/connect`, `/llms.txt`, agent card, `/board`, `/work` | `pages.py` |
| Edge | The Cloudflare Worker in front of the service | `deploy/cloudflare/worker.js` |
| Origin | The service the edge forwards to | Fly.io app |
| Edge secret | The shared value the edge sends in `x-edge-auth` and the origin requires | `TESSERA_EDGE_SECRET`, `EDGE_SECRET` |
| Conformance suite | Tests T01 to T16 and ST01 to ST04 that define a correct backend | `conformance-draft/` |
