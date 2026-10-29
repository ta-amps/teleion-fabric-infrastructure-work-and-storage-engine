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
cache, a key expiry clock or a claim throttle. Every action still has its ordinary
audit event and timestamp, including namespace-to-primary audit projection.

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

The source audit also found repeated schema checks at cold start, an existing
`INSERT OR IGNORE` salt attempt, and four retention-cleanup DELETE statements on
every audit call. Existing completed migration markers already skip historical
tombstone migration. These observations do not quantify their production row
cost or prove that cold starts caused the incident. This change leaves schema,
retention, audit and alarm behavior intact. Further optimization needs measured
row counts and tests that preserve security, retention and receipts.
