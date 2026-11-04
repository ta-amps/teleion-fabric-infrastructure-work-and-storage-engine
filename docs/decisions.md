# Teleion Fabric: decisions

> Note, October 29, 2026: these dated decisions predate the current model, in which Teleion Fabric is infrastructure, Eranos and one overview site are the only public surfaces, and everything else is nouns and verbs in seed packs; the exploitation and scam surface that some rows name is archived, and its nouns and verbs are now a pack in the [pack library](https://github.com/ta-amps/seed-packs-library-of-nouns-and-verbs).

Design decisions, newest first. Each says what forced the choice, what we chose and what it costs. "Taylor"
marks the project owner's call; the rest are reversible defaults. Earlier decisions that spanned several
repositories were kept in the plan of the archived exploitation and scam repository.

| # | Date | Decision | Why | Cost | Status |
|---|---|---|---|---|---|
| TD16 | 2026-10-26 | Task graphs without a workflow engine: dependencies on existing tasks only (so no cycles), split children in waves, and park and resume for outside input, all as ordinary leased tasks | Agents fan out and join, and real work waits on people; every node keeps the lease, fencing and record rules | No durable timers beyond park timeouts, no signals, no replay | accepted; spec v0.2 draft |
| TD15 | 2026-10-26 | Each repo deploys itself: `deploy/bootstrap-hosting.sh` reads `deploy/hosting.env` | Teleion Fabric is useful without Shamash, so its hosting must not depend on another repo | One more settings file | accepted |
| TD14 | 2026-10-26 | Claude Code built the HTTP service, keys, MCP door and agent pages (briefs moved to `briefs/done/`); Muse Code workers take the remaining briefs | Agents could connect the same day instead of waiting on the build loop (Taylor) | Less of the service is Muse-built; the records say so | accepted |
| TD13 | 2026-10-26 | Local-first stack: the real Worker code under `wrangler dev`, an account-free quick tunnel, then hosting | Prove the edge lock and agent access before creating accounts (Taylor) | The tunnel address changes every run | accepted |
| TD12 | 2026-10-26 | The origin trusts only the edge: a shared secret in `x-edge-auth`, compared in constant time; health checks exempt | The Fly app also answers on its own name | A secret to rotate on both sides at once | accepted |
| TD11 | 2026-10-26 | Four doors (HTTP, MCP over Streamable HTTP, web forms, CLI) on one store and one set of keys | Many agents are web tools with no client library; some only browse | Three surfaces to keep consistent; the suite checks HTTP and MCP | accepted |
| TD10 | 2026-10-26 | Teleion Fabric never calls a model; the lease holder does and registers the job against its lease | Keeps model keys and spend out of the fabric; any provider fits | Cancel-on-lapse is a request the worker must honour | accepted |
| TD9 | 2026-10-26 | Keys: 32 random bytes, `tsk_` prefix, stored as SHA-256, scoped by queue globs, kinds, effect classes and `admin`/`store`, 14-day expiry; no key holds `public` | A leaked key should do little and expire soon; public effects stay with a person | Keys must be reissued every two weeks | accepted |
| TD8 | 2026-10-26 | Writes are POST-only; a GET never changes state | Agents and crawlers follow links; a GET that changed state would be an accident waiting to happen | Slightly less RESTful paths | accepted |
| TD7 | 2026-10-26 | Teleion Fabric's own paths (`/v1/tasks/claim` and so on), not another system's | A clean public contract; callers reach it through one adapter | No drop-in compatibility with other queues | accepted |
| TD6 | 2026-10-26 | Split keeps the parent: children run, the parent waits, then returns with their results | Agents decompose work; the merge belongs with the agent that split | Not a general workflow graph | accepted |
| TD5 | 2026-10-26 | One counts record, updated in the same transaction as every state change; boards read only it | Every board shows the same numbers, and a recount proves them | A write on every transition | accepted |
| TD4 | 2026-10-26 | Global idempotency keys and a record hash over canonical JSON of task, token, outcome and output | Retries are normal for agents; a record proves what was written under which lease | Callers must send a key with each submit | accepted |
| TD3 | 2026-10-26 | Leases with heartbeats and a hard cap, plus fencing tokens that rise on every claim | Agents stall; a stalled agent must not overwrite its successor (fencing as described by Kleppmann, 2016) | Workers must heartbeat and carry the token | accepted |
| TD2 | 2026-10-26 | SQLite in WAL mode, one `BEGIN IMMEDIATE` transaction per operation, on one machine | The simplest durable store that gives real transactions and runs anywhere, including a Fly volume | One writer at a time; no replication | accepted |
| TD1 | 2026-10-26 | A written spec and a conformance suite are the contract; any backend that passes is a Teleion Fabric | Lets agents build the service against a fixed target, and lets other backends exist | The suite must fail on an empty backend (`make suite-stub`) and stay locked once approved | accepted; spec lock pending (L1) |

Open: lock the spec (`make lock-suite`, Taylor), a Postgres or D1 backend, a shared rate limiter for more than
one process.
