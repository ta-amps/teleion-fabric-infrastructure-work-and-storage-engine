# Teleion Fabric: handoff

## Integration checkpoint, October 29, 2026

The reviewed release is merged into `main`. The tested implementation revision is
`b1b470475c3ff03db0963bbcf6b271911d79f73e`; [its GitHub checks](https://github.com/ta-amps/teleion-fabric-infrastructure-work-and-storage-engine/actions/runs/40872587755) passed.
Fresh builds matched the deployed Worker executable exactly.

262 Python checks passed, with six existing skips; five actual workerd cases, local HTTP conformance (32 passed, four skipped), and the empty-backend negative control passed. The deployed anonymous/keyed history smoke passed 112 checks.

Execution observations and bounded recall are protected nouns captured with accepted verb completion. Recall never grants permission to replay an effect. Category query repairs are implemented; a general category/profile resolver remains separate work.

Follow the [worker-first policy](WORKER-FIRST-EXECUTION.md): independently
schedulable reasoning is a claimed verb; inputs, contracts, artifacts and evidence
are scoped nouns. Workers reason and self-check. Authentication, leases, CAS,
types and records stay deterministic. Server inference is an explicitly
configured, scoped and bounded exception. Missing capacity or invalid input does
not enable it automatically.

The earlier inventory below is dated history. These checks cover the recorded
implementations and fixtures; they are not universal harness, load or rendered-UI
certification.

## Earlier handoff

> Note, October 29, 2026: this dated section predates the current model, in which Teleion Fabric is infrastructure, Eranos and one overview site are the only public surfaces, and everything else, agent kits included, is nouns and verbs in seed packs; the exploitation and scam surface it describes is archived, and its nouns and verbs are now a pack in the [pack library](https://github.com/ta-amps/seed-packs-library-of-nouns-and-verbs).

Where the work stands as of Oct 26, 2026, how to verify every guarantee from a fresh clone, what sits in
`briefs/` for the next workers, and how Shamash and Eranos connect to the fabric.

## What works today

- **Store and kernel (`src/tessera/`).** Single-node SQLite WAL store (`BEGIN IMMEDIATE` per operation) with
  named queues, leases with heartbeats and a hard cap, rising fencing tokens, idempotent submits with
  SHA-256 records, jittered exponential backoff and dead letters, split and merge (including ordered sibling
  waves via `after`), task dependencies (`depends_on`), park and resume (numbered parks, timeouts, non-self
  resume), model-job tracking tied to the lease, compare-and-set versioned records, atomic record batches,
  content-addressed blobs, one `counts` table and an append-only event log. Nothing in `src/tessera/` names
  any product or domain.
- **Four doors on one set of hashed keys.** HTTP under `/v1/`, MCP over Streamable HTTP at `/mcp`, plain web
  forms at `/work` (never echoing the key) plus the read-only `/board` and `/connect` pages, and the CLI
  (`uv run tessera`). Keys carry the `tsk_` prefix, are stored only as SHA-256 hashes, expire after 14 days,
  and are scoped to queue globs, task kinds, effect classes (`read`, `write-internal`, `write-repo`) and
  `admin` / `store`. No key holds `public`.
- **One default local SQLite path.** `Makefile`, `scripts/dev_stack.sh`, `tools/seed.py`,
  `tools/load_briefs.py`, `tools/muse_worker.py`, `tools/swarm_expander.py` and `uv run tessera` all default to
  `.tessera/tessera.db` (overridable via `TESSERA_DB`), so `make dev`, `make serve` and `make seed` share the
  same database out of the box. `make seed` defines the `seed` and `expand` queues alongside the general and
  surface queues, writes initial `seeds/*` nouns, and saves worker keys to
  `~/.config/tessera/keys-tessera.json` (mode `0600`). `make expand-demo` runs the full four-stage bounded
  expansion loop (`seeds` -> `proposals` -> `canonical`, see [expansion.md](expansion.md)).
- **Local edge and hosting scripts.** `make dev` runs the service behind the real Cloudflare Worker code
  (`deploy/cloudflare/worker.js`) with a fresh per-run `x-edge-auth` secret (`TUNNEL=1` adds a temporary
  `*.trycloudflare.com` tunnel). `deploy/bootstrap-hosting.sh` and `deploy/hosting.env` prepare the Fly.io
  origin (`tessera-fabric`) and Cloudflare Worker (`tessera`), with optional single-domain routing at
  `tessera.<your-domain>` in `deploy/cloudflare/wrangler.jsonc`. Hosted deployment (`L20`) waits on Taylor's
  approval.

## How to verify

Run these from the repo root:

```bash
make setup             # install Python 3.12 and dependencies with uv
make check             # ruff lint & format, mypy strict, pytest, check_public, lock, indexes, architecture
make suite-http-local  # real local service + real keys: 31 passed, 4 skipped (clock/restart run on store)
make suite-stub        # proves the conformance suite fails against an empty backend
```

What each check proves:

| Command | Checks |
|---|---|
| `make check` | Lint, formatting, strict types on `src/tessera`, unit tests and store conformance (`30 passed, 5 skipped` in `conformance-draft/` plus unit tests in `tests/`), secret and private-name scan (`scripts/check_public.py`), index freshness (`scripts/build_index.py --check`), and [architecture.json](../architecture.json) against the code (`scripts/check_architecture.py`) |
| `make suite-http-local` | Starts `tessera serve` on a local port, issues `admin`, `q_a` and `q_b` keys, runs `conformance-draft/` over HTTP and MCP (`31 passed, 4 skipped`), and scans the server log for leaked keys (`T16`) |
| `make suite-stub` | Runs `conformance-draft/` against `tessera.testing:create_empty_backend` and exits non-zero if the suite ever passes on a no-op backend |
| `python3 ../teleionlabs-teleion-fabric/hackathon/scripts/check_docs.py .` | Parses every JSON file and verifies every relative Markdown link resolves |

## Open briefs

`uv run python tools/load_briefs.py` enqueues every agent brief whose `depends_on` tasks are done onto queue
`build` in `.tessera/tessera.db`. `uv run python tools/muse_worker.py --worker muse-01` claims one brief,
validates `repo`, `task_id` and `branch` before touching git or the filesystem, creates a sibling worktree,
runs Muse Code headless with its sandbox on and Contributor mode off, gates on `make check`, and submits a
record.

| Brief | Owner | Effect | Depends on | Summary |
|---|---|---|---|---|
| [briefs/L11.json](../briefs/L11.json) | `muse-code` | `write-repo` | -- | Nightly making-of export (`events.jsonl` and `counts.json`) |
| [briefs/L13.json](../briefs/L13.json) | `muse-code` | `write-repo` | `L8`, `L9` (done) | Run the remaining `L*` briefs through Teleion Fabric's `build` queue |
| [briefs/L16.json](../briefs/L16.json) | `muse-code` | `write-repo` | `L8` (done) | Model-job lane polling and cancel-on-lapse (`T12` over HTTP) |
| [briefs/L18.json](../briefs/L18.json) | `muse-code` | `write-repo` | `L8` (done) | Records gate checked over HTTP (`T15`) |
| [briefs/L19.json](../briefs/L19.json) | `muse-code` | `write-repo` | `L14` (done) | Read-only `/board` v2 checked against `GET /v1/counts` |
| [briefs/L20.json](../briefs/L20.json) | `human` | `public` | `L9`, `L14`, `L17` (done) | Deploy `tessera-fabric` on Fly.io behind the Cloudflare Worker (Taylor only; never queued) |
| [briefs/L21.json](../briefs/L21.json) | `muse-code` | `write-repo` | `L20` | Soak and restart runs (`T13`, `T14`) against the deployed service |
| [briefs/L23.json](../briefs/L23.json) | `muse-code` | `write-repo` | `L21` | Move Shamash's run loop onto the deployed service |
| [briefs/L25.json](../briefs/L25.json) | `muse-code` | `write-repo` | `L8`, `L17` (done) | Resume effect-class gate: require the task's effect class and `admin` for `write-repo`, plus conformance test `T22` |
| [briefs/L26.json](../briefs/L26.json) | `muse-code` | `write-repo` | `L8` (done) | Versioned record hash test vectors JSON so JS/TS clients verify SHA-256 records byte-for-byte |

Once `L25` and `L26` land and Taylor approves [SPEC.md](../conformance-draft/SPEC.md) (`L1`), `make lock-suite`
moves `conformance-draft/` to `conformance/` and writes `conformance.lock`.

## How Shamash and Eranos connect to Teleion Fabric

Two public-good surfaces share one Teleion Fabric instance (`tessera.<domain>` when hosted, or
`http://127.0.0.1:7701` locally under `make dev`). `make seed` (`tools/seed.py`) defines the general agent
queues (`research`, `draft`, `review`, `build`) together with both surfaces' pipeline queues:

- **[Shamash](https://github.com/ta-amps/seed-packs-library-of-nouns-and-verbs/tree/main/packs/exploitation-evidence)** (`shamash.<domain>`) finds, quantifies and combats human harm,
  scams and exploitation across social media, websites, messages, documents and payments. Its five-stage case loop
  runs on queues `aq02` (Ingest), `aq85`
  (Situate), `aq04` (Problemize), `aq05` (Investigate) and `aq86` (Gate + audit). Stages join through
  `depends_on`, pause with `park` / `resume` when a case needs missing documents or human review, and store
  versioned case records and content-addressed evidence blobs through the `store` scope. Shamash's service and
  workers hold keys scoped to `aq*` (or the five queue names explicitly).
- **[Eranos](https://github.com/ta-amps/eranos-public-marketplace-for-public-good-token-burn)** (`eranos.<domain>`) lets people donate unused AI tokens to
  build, test and independently review off-the-shelf agent skills, tools, files and agentic processes that NGOs,
  nonprofits and public-good teams can use right away. Contributors' agents and Eranos's API connect over MCP
  (`/mcp`), HTTP (`/v1/*`) or `/work` forms using keys scoped to `eq01`, `eq02` and `eq03`, write versioned kit
  records with compare-and-set so concurrent contributors never overwrite one another, and verify SHA-256
  records on every submitted draft and review.

Under a single Cloudflare domain, the three Workers sit side by side (`tessera.<domain>`, `shamash.<domain>`,
`eranos.<domain>`), each configured via `CUSTOM_DOMAIN` in `deploy/hosting.env` and a custom-domain route in
`deploy/cloudflare/wrangler.jsonc`, and each locked to its own Fly.io origin with its own `EDGE_SECRET`.

## Rules to keep when picking up work

Read [AGENTS.md](../AGENTS.md) before changing anything:

- Keep `src/tessera/` 100% domain-neutral; Shamash and Eranos belong only in `tools/`, `briefs/` and docs.
- Never edit `conformance/` or `conformance.lock`, and never weaken a test.
- Never print, log, commit or paste a key; public effects (`deploy`, `push`, `publish`, PRs) wait on Taylor.
- Rebuild generated indexes with `make index` whenever files, headings or reading order change, and run
  `make check` before every commit.

More detail: [summary.md](summary.md) · [architecture.md](architecture.md) · [setup.md](setup.md) ·
[expansion.md](expansion.md) · [operations.md](operations.md) · [review.md](review.md)
