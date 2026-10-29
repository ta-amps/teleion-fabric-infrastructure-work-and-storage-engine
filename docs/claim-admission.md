# Claim admission

Healthy keyed workers have no default claims-per-minute quota. A burst waits for
capacity instead of being rejected merely because it crossed sixty requests.
The Cloudflare coordinator and Python service both use deterministic adaptive
admission; no model runs on the server to make this decision.

Only new claims enter the admission queue. Heartbeats, submissions, reads and
recovery operations continue through their existing paths. Waiting principals
rotate fairly, with FIFO order within each principal. Lease acquisition remains
an atomic database transaction. Fencing, idempotency and receipt semantics do
not change.

The controller measures admitted claim service time, excluding its own waiting
time. An exponential average above the latency target reduces concurrency by
30%; healthy saturated work increases it gradually. Adjustments happen at most
once per target interval. Existing admitted requests finish normally. The
pending queue and maximum wait protect memory and avoid assigning a lease to a
request that has already timed out waiting. Only those overload conditions
produce `429 rate_limited` with `Retry-After`. Clients should honor that response
and add jitter. Empty queues retain their existing retry guidance.

These are resource settings, not advertised throughput guarantees:

| Cloudflare variable | Python variable | Default |
| --- | --- | --- |
| `CLAIM_INITIAL_CONCURRENCY` | `TESSERA_CLAIM_INITIAL_CONCURRENCY` | 32 |
| `CLAIM_MAX_CONCURRENCY` | `TESSERA_CLAIM_MAX_CONCURRENCY` | 1024 |
| `CLAIM_MAX_PENDING` | `TESSERA_CLAIM_MAX_PENDING` | 2048 |
| `CLAIM_MAX_WAIT_MS` | `TESSERA_CLAIM_MAX_WAIT_SECONDS` | 10000 ms / 10 s |
| `CLAIM_TARGET_MS` | `TESSERA_CLAIM_TARGET_SECONDS` | 500 ms / 0.5 s |

Tune these from deployment measurements. The initial window is a starting
point; the maximum window and pending count bound retained work. They are not
per-key quotas. HTTP claim responses expose `X-Claim-Wait-Ms` and
`X-Claim-Concurrency`. Anonymous demo quotas and access controls still apply.
The Python service retains `TESSERA_CLAIMS_PER_MINUTE` as an explicit legacy
opt-in: zero, the default, disables that quota; a positive number enables it.

Cloudflare timer APIs advance after I/O, so the measured duration includes
observable I/O such as authorization and audit operations; it is not a CPU
profile or a RAM reading. Pending and in-flight counts are memory-pressure
proxies. Use deployment metrics and profiling to investigate CPU or memory
exhaustion. See [Cloudflare's timer documentation](https://developers.cloudflare.com/workers/runtime-apis/performance/).

Admission state is ephemeral: one controller per Durable Object, or per Python
service process. Restarting resets scheduling hints, never persisted leases or
authorization. It does not enforce a global quota across namespaces or Python
processes. Autoscaling, database performance and sustained multi-account load
still need their own measurements.

## Competing solutions

Several workers may solve the same versioned brief. Represent these as separate
attempt verbs referencing the shared input nouns. Each attempt owns a lease,
an artifact key and an execution receipt. A subsequent comparison or remix verb
can consume all the alternatives. This preserves useful diversity without
turning an expired lease into permission to overwrite another worker's result.

## Verification

`make check` exercises healthy traffic beyond sixty requests, fair waiting,
cancellation, queue bounds, expiry, latency reduction and recovery. The explicit
legacy-quota test remains unchanged. `make test-worker` runs the actual Worker
and SQLite Durable Object: one hundred simultaneous keyed claims, distinct
alternative outputs, heartbeats and idempotent submissions. These are bounded
synthetic tests, not a sustained capacity claim.
