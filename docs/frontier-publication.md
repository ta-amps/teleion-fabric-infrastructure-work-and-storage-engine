# Frontier publication receipts

Frontier keeps generated agent files in `frontier.candidates`. Queue stages use
Tessera's existing transactions and fencing: `frontier.repair` completes before
`frontier.verify`, and `frontier.review` depends on verification. Only the scheduler
needs grants to enqueue across these queues. Claiming workers keep narrow grants.

Successful verification binds candidate version 1, the repair task, artifact bytes,
file SHA-256 hashes and a native full-loop evidence digest. The matching version-1
`frontier.verifications/native:<digest>` noun must exist, belong to the verifier,
retain quarantine and hash to that digest. Its run manifest names the harness,
provider, model, principal kind and receipt digest; controls have a separate hash.
Successful review must
name that exact completed verification receipt. Repair, verification and review
require distinct authenticated keys and distinct tracked account IDs. This prevents
one account from reviewing itself using two keys. Failed verification and rejected
review may complete with explicit false outcomes; they cannot authorize publication.

The store checks all proofs inside the same transaction as the fenced completion
or CAS publication. `frontier.catalog` accepts only a create-only version-0 write
keyed by the artifact's canonical SHA-256, with producer_surface=surface:frontier,
visibility_scope=public_commons and status:verified. The published bytes must equal
the artifact bound by the completed review. Admin and direct library writes also
pass this gate. Surface writes additionally require a frontier.review queue grant.
Catalog entries cannot be edited in place.

Staging stores cannot widen visibility or drop candidate/evidence quarantine tags.
Frontier producers cannot copy staging material into other public records, task
briefs, feeds, blobs or bucket objects. The only internal public-write exception
follows a successful catalog proof inside the transaction. Python's bucket-definition
model lacks governance, so Frontier principals cannot define buckets on that backend.
Namespace Workers retain account metadata from authenticated primary grants, without
caching credentials or bypassing primary authorization on subsequent requests.

The Python store and Cloudflare SQLite coordinator enforce the same contract.
The portal's hosted connector additionally rebuilds the four allowlisted files
from trusted templates before serving them. It forwards the caller's credential
for work and uses a separate read-only principal for published catalog reads.
Private previews require a verifier key. No generated code is executed by this path.

Verification is an accountable attestation, not proof of an LLM's general ability.
Native harness evidence must exist and be independently inspected before promotion.
Synthetic storage tests exercise the gate without claiming native model results.
There is no automatic publication or per-artifact revocation in this version;
operators can disable the hosted connector flag while retaining immutable evidence.

`whoami` and `/v1/me` now include the principal's queue, kind and effect grants so
clients can select allowed work. They contain no token secret. Anonymous heartbeat
responses retain the real lease `expires_at`; `session_expires_at` is separate.

Validation: `tests/test_frontier_publication.py` and
`tests/edge_frontier_publication.mjs` cover forged or missing receipts, namespace
isolation, self-verification, file mismatches, immutable publication, concurrent
CAS writes and heartbeat deadlines. Run `make check` before release.
