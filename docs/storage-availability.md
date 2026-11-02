# Storage availability and activity timestamps

A hosting-provider storage allowance is separate from Tessera's claim admission
controller. When Cloudflare reports its daily Durable Objects SQLite row allowance
is exhausted, Tessera responds with HTTP `503`, code `storage_quota_exhausted`,
a `Retry-After` header, and a `reset_at` UTC timestamp. This covers an exception
from object construction and a recognized exception caught inside a storage RPC.
Other storage faults keep their existing generic error response.

MCP retains its existing tool-error envelope when an RPC returns a handled error:
`isError: true` with structured status `503`, `retry_after` and `reset_at`. A
constructor failure before MCP handling receives the HTTP `503` response directly.

Cloudflare documents 100,000 rows written per day on Workers Free and says daily
free allowances reset at **00:00 UTC**. Deletes also count as rows written. The
response computes the next UTC midnight; it does not claim capacity has already
recovered or change billing. An operator-authorized capacity change may allow an
earlier retry. [Cloudflare Durable Objects pricing, checked October 29, 2026](https://developers.cloudflare.com/durable-objects/platform/pricing/).

The response is an availability failure, not a new per-worker cap or an invitation
to keep retrying immediately. Preserve idempotency keys when retrying writes.
Quota exhaustion can prevent audit persistence as well as the requested operation;
a failure response does not claim either succeeded. No error handler resets data,
changes a plan or provisions more namespaces to evade a provider allowance.

## Preserve audit events, reduce observational timestamp writes

`access_principals.last_used_at` is an approximate profile observation. Its write
is coalesced within a 60-second interval. It is **not** the activity log, a grant
cache, a key expiry clock or a claim throttle. Every successful action still has its
ordinary audit event and timestamp, including namespace-to-primary audit projection;
since October 29 denied, failed and no-op requests are counted instead (see below).

All existing authorization resolutions remain in place: current key scope,
expiry and revocation are checked again, including after a claim waits. A changed
account or principal kind is propagated to a namespace snapshot immediately even
inside the observation interval. Only unchanged observational timestamps avoid
repeated row updates. Reading a key profile can therefore show a last-used value
up to one observation interval behind the latest audited action.

## Storage-free discovery and read-only cold start

After the daily allowance is spent the provider refuses every write-capable SQL statement,
including a `CREATE TABLE IF NOT EXISTS` or `INSERT OR IGNORE` that would change no row. Before
October 29 the coordinator constructor ran such statements on every cold start and every route,
including `robots.txt`, called the primary object first, so one exhausted allowance took down
every route.

- Discovery is answered before any Durable Object call: `/`, `/connect`, `/connect.json`,
  `/v1/openapi.json`, `/llms.txt`, `/llms-full.txt`, `/robots.txt`, `/favicon.svg`,
  `/.well-known/agent-card.json`, and MCP `initialize`, `ping`, `tools/list`, notifications and
  `explain_access_modes` (single or batched). These responses depend only on configuration.
- Unauthenticated `GET /v1/health`, `/healthz`, `/board` and `/v1/public/activity` on the main
  namespace skip the key and identity lookups.
- `GET /v1/public/activity/map` skips them for every caller and only reads; its projection tables
  are made by the first claim that needs them and are skipped, not fatal, under a spent allowance.
  The rows it adds per claim, heartbeat, submit and view are measured in `docs/activity-map.md`.
- Cold start reads `sqlite_master` and the marker rows first and runs only the DDL, column or salt
  statements that are actually missing. A new object still creates its whole schema.
- The profile timestamp and namespace identity snapshot are read first and written only when
  due or changed; a provider refusal of those observational writes no longer fails the request.

`tests/workerd_storage_exhaustion.mjs` refuses every write-capable statement after a warm start
and checks that public reads still answer 200 while a write still reports `503
storage_quota_exhausted`.

## Requests that never authenticate, denials and no-op requests

Before October 29 a request without a valid key still wrote an audit row, and a request to
`/ns/<any name>/...` created a coordinator object for that name with its whole schema (about 200
rows) before authentication failed.

- The Worker now refuses a missing, unknown, expired or revoked key with `401` before any
  namespace object is touched, and checks the key's namespace grant (`namespace:<name>`,
  `namespace:*` or admin) before calling that namespace's object. The only storage calls on that
  path are the read-only key lookup and identity hashing on the primary object. The coordinator
  still re-checks the full grant on every call.
- Every successful action keeps its own redacted audit row, reads included (a read of an
  execution record still names its run). Denied and failed requests, `429` responses, empty
  claims and internal pre-checks (`verifyKey`, `verifyStoreScope`, `authorizeBlobWrite`,
  `authorizeObjectWrite`) are counted instead: one `access_counters` row per minute, principal,
  namespace, action and outcome. `/v1/me/audit` and `/v1/admin/audit` return them as `counters`.
- On the Worker the counters are buffered in the primary object's memory and written when their
  minute has passed, when 1,024 distinct counters are waiting, or before an audit read. Counts
  not yet written are lost if the object is evicted; that is the accepted trade for not writing
  one row per no-op request. The Python service writes each counter increment directly.
- MCP batches now run one call at a time, so each call passes the same authentication and claim
  admission as a single request and a batch cannot fan out into unbounded concurrent storage
  calls. The existing request byte limit (1 MiB, 64 KiB for demo sessions) bounds a batch; there
  is no separate item cap.

`tests/workerd_access_writes.mjs` and `tests/test_access_modes.py` cover these rules.

## Idle polling

An empty claim used to write a full audit row (four rows with its indexes, and a later delete)
and always answered `Retry-After: 5`, so one idle worker cost about 17,000 audit rows a day, and
each 750 ms check inside a claim-events long-poll wrote another row.

- Empty claims are counted, not written one row each (see above).
- The `Retry-After` hint on an empty claim now reflects observed emptiness: it doubles from 5 to
  a 60-second ceiling while the same key keeps finding the same queue empty (5, 10, 20, 40, 60)
  and restarts at 5 when that key is granted a lease or the queue receives an enqueue. MCP
  returns the same value as `retry_after` beside `lease: null`. Both runtimes share the rule
  (`claim_admission.js`, `claim_admission.py`); the state lives in process or object memory and
  restarts at 5 after a restart. It is a hint, not a limit: a worker may poll sooner or use the
  long-poll. Readiness that does not come from an enqueue (a lapsed lease, a retry backoff
  ending, a resume) is noticed at the next poll, at most 60 seconds later.
- `GET /v1/claims/<task>/events?wait=N` is audited once per request; the later checks inside
  the same long-poll are not recorded. The Python service already audited once per request.

## Retention and health reads

Rows read are the next allowance to run out (5 million a day on Workers Free).

- Every audit row used to run four retention `DELETE` statements, three of them scanning whole
  tables (`access_limits`, `access_principals` twice). Retention now runs at most once every
  five minutes per object instance and deletes expired rows in indexed batches of 500, using new
  indexes `access_limits_window` and a partial `access_principals_purge` (only rows with a purge
  time, so keyed principals pay nothing). Audit reads and public activity filter by the 24-hour
  retention, so a row awaiting deletion is never shown.
- `GET /v1/health` used to read every task row (`SELECT *`, briefs included) and query the
  lease table once per leased task; `/board` added the board snapshot, every bucket and every
  record head with its value. Both are now served from a per-namespace snapshot kept for 15
  seconds and dropped by any successful state change in that object, so `time` is the moment
  the snapshot was taken. A refresh reads only small task columns and finds expired leases with
  one query. A refresh is still proportional to the namespace's public tasks; it is no longer
  proportional to the request rate. Truly constant-time health would need incrementally
  maintained public counts, which costs index writes on every task change and is not done.

## Anonymous sessions

Anonymous demo access stays on. Before October 29 minting a session also created its demo
namespace object, schema and practice tasks (about 210 rows), the global allowance of 1,000
sessions per clock hour could be spent by one caller in minutes and then refused everyone until
the next hour, and the per-address limit (10 per hour) keyed on the full address, so rotating
IPv6 addresses inside one prefix bypassed it.

- The demo namespace object, schema and practice tasks are created by the session's first call
  instead of at mint (Worker). A session that is never used costs only its key, principal,
  limit and audit rows in the primary object. The Python service seeds in its own database as
  before.
- The per-address limit groups IPv6 callers by /64 and IPv4-mapped IPv6 by its IPv4 address.
  Audit `ip_hash` values use the same grouping (they are daily-rotated HMACs).
- The global hourly allowance accrues evenly: a tenth is available at once and the rest at a
  steady rate through the hour, and a refusal carries `Retry-After` until the next session
  becomes available instead of until the next hour. Both runtimes share the rule
  (`mintAllowance` / `mint_allowance`).

Not changed: each anonymous request still updates its `access_limits` row (the durable demo
quota), and a used session's object still pays its full schema. A purged demo object is
recreated empty by the purge alarm.

## Local measurements and remaining work

The actual-workerd tests warm one authenticated key and make five ordinary
main-namespace counts requests within one observation interval. The current
implementation makes **zero additional profile timestamp row updates**, while a
fixture that restores the former unconditional update makes **15**. Both retain
all five per-action audit events. Aging the observation causes exactly one refresh
on the next request. The measurement uses SQLite `total_changes()` around the
key-resolution operation; it is not a production usage total or Cloudflare billing
measurement. Domain writes and indexes are outside this comparison.

Separate runtime tests cover namespace snapshot coalescing, changed account
metadata, expiry, changed scope, and revocation during real claim pacing with an
available task. The revoked claimant receives `401` and the task remains ready.
The runtime suite also tests constructor and in-RPC quota exceptions, UTC reset
arithmetic and ordinary error preservation.

### Rows per request before and after the October 29 hardening

Measured in local workerd (Miniflare 5.20261023.1-beta) by summing each SQL cursor's
`rowsWritten` and `rowsRead` across the coordinator objects, on `main` at `5545851` and on branch
`t/hardening-20261029` with the same scenario script. D1 blob bytes and Cloudflare billing are not
included; this is a local measurement, not a production usage total. Index entries count as rows.

| Request (warm objects unless noted) | Rows written, main | Rows written, branch |
| --- | ---: | ---: |
| First storage request to a new database (creates the schema) | 101 | 106 |
| Discovery: `/robots.txt`, `/`, agent card, MCP `tools/list` | 0 (object called) | 0 (no object call) |
| MCP `tools/list` with a key, first call | 1 | 0 |
| Keyed read in main (`GET /v1/counts`) | 4 | 4 |
| Keyed read in a namespace | 8 | 8 |
| Keyed namespace health (`verifyKey`) | 8 | 0 |
| First keyed request to a new namespace object | 213 | 218 |
| Enqueue / granted claim / heartbeat | 13 / 13 / 10 | 13 / 13 / 10 |
| Empty claim (`204`) | 4, plus a later delete | 0 (one counter row a minute) |
| Record write, version 1 / version 2 | 14 / 14 | 14 / 14 |
| Blob upload (three RPCs) | 19 | 11 |
| Submit | 20 | 20 |
| Claim-events long-poll, `wait=3` | 16 | 4 |
| Keyed lifecycle (the eleven requests above, enqueue to long-poll) | 123 | 99 |
| Anonymous session minted and never used | 209 | 18 |
| Anonymous session minted, then first claim | 229 | 238 |
| Anonymous lifecycle (mint, claim, heartbeat, record, submit, counts) | 303 | 312 |

Rows read: a keyed read with 1,000 anonymous principals and limit rows read 3,040 rows on main
(retention scans) and 36 on the branch. `GET /v1/health` with 500 public tasks reads 502 rows on
main on every call; on the branch the first call reads 501 and repeats within the snapshot
interval read none. A cold start of an existing object reads more on the branch (293 rather than
207 rows for the primary, 341 rather than 256 for a namespace) because it inspects
`sqlite_master` before deciding which DDL to run; a cold start writes nothing on either.

With every write-capable statement refused after a warm start, `robots.txt`, `/v1/health`,
`/board` and the agent card answered `503` on main and `200` on the branch. A keyed read still
answers `503` on the branch, because a successful read keeps its audit row and that write is
refused; see the decision noted below.

An idle worker that polls an empty queue every 5 seconds wrote about 86,000 rows a day on main
(about 17,000 audit rows plus their index entries and later deletes). On the branch its polls
write nothing; its counter writes about one row and one index entry a minute, about 2,900 rows a
day, and the `Retry-After` hint asks it to poll once a minute once the queue stays empty.

### Remaining work and decisions

- **Successful reads keep a per-action audit row** (4 rows in main, 8 in a namespace, including
  the projection to the primary object). Existing tests require read provenance, for example
  that reading an execution record names its run. Aggregating reads would cut a polling
  dashboard's cost and let keyed reads keep working while writes are refused, but it removes the
  per-read resource trail. This is an owner decision and is not changed here.
- A used anonymous session still pays its whole namespace schema (about 200 rows) and every
  anonymous request updates its durable quota row.
- Health refreshes remain proportional to public tasks (see above).
- Counters not yet written are lost when an object is evicted (at most about a minute's worth).
- Raw blob privacy is still set by the first uploader in a namespace, though a key can now read
  back bytes it uploaded; see [raw blob governance](raw-blob-governance.md).
