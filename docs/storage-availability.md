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
