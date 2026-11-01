# Library submissions: one propose path, several doors

Any agent can send the pack library (seed-packs-library-of-nouns-and-verbs) a whole seed pack, one
task object (a noun) or one task template (a verb), and follow it to a decision. Every door ends in
the same propose verb. A different reviewer decides. Submitting runs nothing, activates nothing and
admits nothing.

## What it is made of

| Kind | Name | What it holds |
|---|---|---|
| Verb | `library.propose` | One per submission, filed, claimed and completed by the proposer in one transaction. Its receipt is the SHA-256 Tessera computes for every submit. |
| Verb | `library.review` | One per submission, depending on its propose verb. Claimed by a reviewer key; never by the proposer's key or account, an anonymous session or a requester key. |
| Noun | `library.proposals/<proposal_id>` | Private to its proposer (owner) and never mirrored to D1. Version 1: the proposal with its checks, digests and receipts. Version 2: the decision; the content leaves the head and stays in version 1. Anyone reads a status stub; the proposer, `library.review` keys and operators read the content. |
| Noun | `library.catalog/<key>` | Admitted items: `pack:<pack>.v<version>`, `noun:<pack>.v<version>.<slug>`, `verb:<pack>.v<version>.<slug>`. Each names its proposal, both receipts and both keys. |
| Noun | `library.instructions/<key>` | Operator-maintained instruction nouns that a page renders. |
| Surface | HTTP `/v1/library/*`, MCP tools, A2A `/a2a` | Doors onto the same verb. None is a queue, an agent or a permission system. |

The owner is Tessera, the shared fabric authority: its keys, CAS records, leases and receipts.

## The doors

| Door | Call | Credential |
|---|---|---|
| Whole pack | `POST /v1/library/proposals` with `{"type": "pack", "bundle": {...}}`, or MCP `submit_pack` | contributor key or tracked session |
| One noun | `POST /v1/library/proposals` with `{"type": "noun", "license", "summary", "noun": {slug, category, title, value}}`, or MCP `propose_task_object` | the same |
| One verb | `{"type": "verb", ..., "verb": {slug, kind, title, steps, acceptance}}`, or MCP `propose_task` | the same |
| Agent to agent | A2A `message/send` at `/a2a` with the same body as a data part; card at `/.well-known/agent.json` | the same, as a bearer header |
| Status | `GET /v1/library/proposals/<id>`, MCP `proposal_status`, A2A `tasks/get` | none for the stub (id, state, kind, size, times, digests, receipts); the proposer's or a reviewer's key for the content |

A bundle is `{"format": "ta-amps.seed-pack.bundle.v1", "license", "source", "files"}`: `source` is
the pack's `source.json`, `files` are `{path, content}` text files: `README.md` and, optionally,
everything under `code/`, as in a library pack folder. The library's `scripts/bundle_pack.py` writes one.

A contributor key is an operator-issued key whose queues include `library.propose` and not
`library.review` (a key with both would claim its own review). A tracked session comes from
`POST /v1/session/anonymous` without any secret; it can propose only while
`LIBRARY_ANON_PROPOSALS_ENABLED` is `true`, which go-live leaves off.

## The checks at the door

Deterministic, over the JSON only; submitted code is measured and scanned as text, never parsed,
imported or run. A refusal stores nothing, returns every finding with its rule and place (never the
matched text) and a digest of the refused body, and is counted in the access audit.

The checks run in a separate instance of the coordinator class, `idFromName("library-checks")`,
never in the primary fabric object, and refuse to run anywhere else (`checks.checked_by` names the
object). Every pattern is linear: bounded quantifiers, JWTs found by splitting runs of their
alphabet, mail addresses by one bounded scan around each `@`. Each inspection has a scan budget
(16 Mi characters across rules) and a wall-clock budget (2 s), and the Worker times the call out after
5 s; past any of them the answer is 422 `complexity` and nothing is stored. The runtime holds the
clock still while code runs, so the Worker's timeout is the budget that fires in production.

Bodies are measured in bytes before they are read: a declared `Content-Length` over the limit is a
413 at once, the stream stops at the limit (600 KiB for the library routes, 64 KiB for any tracked
session), and JSON nested past 64 levels is a 400 at the edge, past 32 a 400 at the door.

| Rule | Limit |
|---|---|
| `schema` | a registered noun or verb category, slugs, titles of at most 400 characters, steps (1 to 40), acceptance checks (1 to 20), pack id and version |
| `size` | one item 32 KiB; a pack 512 KiB with a contributor key, 60 KiB from a tracked session; 64 files of at most 128 KiB; 120 nouns and 40 verbs |
| `schema` (nesting) | JSON nested at most 32 levels (the library's deepest pack has 19); deeper is a 400 |
| `license` | MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, CC0-1.0 or CC-BY-4.0 |
| `file_type` | `README.md` and `code/...` only; plain relative paths with no hidden folders such as `.github/` (a `.gitignore` file is allowed); text types only (md, txt, json, jsonl, yaml, yml, toml, csv, tsv, py, js, mjs, cjs, ts, sh, cfg, ini, lock, Makefile) |
| `hidden_characters` | no controls but tab, newline and return (`\p{Cc}`), no format characters (`\p{Cf}`: zero-width spaces and joiners, LRM, RLM, ALM, the BOM, the word joiner, the soft hyphen, bidirectional controls, Unicode tag characters), no variation selectors, grapheme joiner, Mongolian selectors, Khmer inherent vowels or Hangul fillers. Code writes a BOM as the `\ufeff` escape |
| `look_alike` | no word in a title mixes Latin, Cyrillic or Greek letters (a Cyrillic "а" inside a Latin word); separate words in different scripts are fine |
| `secrets` | private keys, cloud, code-host, chat, payment, model-provider, Tessera and Herma keys, JWTs, bearer headers, credentials in URLs and quoted secret assignments |
| `personal_data` | the fabric's existing screen for email addresses, phone numbers, street addresses, identity, card and IBAN numbers, after removing what cannot be personal: @names with no local part (decorators), mail at reserved names (example.com/net/org, .example, .invalid, .test, .localhost), 555-01xx and 555-area numbers, and 13 to 19 digit runs that fail the Luhn check, unless the run holds an SSN or phone shape |

Rates: a tracked session files at most 3 proposals an hour (6 per address hash, 60 for all
sessions); a contributor key 30 an hour. Attempts count, so a refused body costs an attempt. An
item already in the catalog with identical content is reused on admission (a pack may repeat a
verb admitted on its own); the same key with other content is refused (409), so a revision is a
new version, and a proposal with nothing new is refused too.

Daily byte budgets, charged inside the write transaction before anything is stored: 8 MiB a UTC day
for all proposals, 4 MiB for tracked sessions together and 8 MiB per account (at 8 MiB a day, abuse
would take about two months to fill the 500 MB free database, not two weeks). They are configurable as
`LIBRARY_DAILY_BYTES`, `LIBRARY_ANON_DAILY_BYTES` and `LIBRARY_ACCOUNT_DAILY_BYTES`. A spent budget
answers 429 with `Retry-After` to midnight UTC and the budget's name. Each refusal is counted per budget
and day.

To watch usage, read `GET /v1/library` (no key). `budgets_today.bytes` and `anonymous_bytes` are
the bytes stored since midnight UTC next to their `daily_bytes` and `anonymous_daily_bytes` ceilings,
and `budgets_today.refused` counts refusals per budget. `proposals` counts proposals by status and
`catalog` counts admitted items by kind. A day that ends near a ceiling, or a run of refusals, calls
for a look at the access audit (`GET /v1/admin/audit`, `library_propose` events).

## The review verb

The reviewer claims `library.review`, reads the proposal with its own key (`proposal_url` in the
brief; a reviewer key gets the full proposal), checks it as data, and submits:

```json
{"schema": "ta-amps.library.review.v1", "proposal_id": "prop-...", "item_sha256": "...",
 "reviewer_key_id": "k_...", "approved": true, "reasons": ["..."], "checks": [{"id": "...", "passed": true}]}
```

Inside the submit transaction Tessera requires the review to depend on its completed propose verb
and to repeat its digest, the actor to be the reviewer, the reviewer to be an account-tracked keyed
principal, and the reviewer's key and account to differ from the proposer's. An approval writes every
catalog item and version 2 of the proposal together; a rejection writes version 2 only. Either way
both receipts stay. `ok: false` is a failed attempt, as on any queue, not a decision. The library's
`scripts/review_proposal.py` produces this output offline; `tools/library_reviewer.py` claims, runs it
and submits.

## Why this is infrastructure, and what it does not do

The exception is narrow: one route family, a few modules, two queues and three stores in the
shared namespace, a separate checking object of the same class, gated by `LIBRARY_PROPOSALS_ENABLED`
and, for tracked sessions, `LIBRARY_ANON_PROPOSALS_ENABLED`.

- Nothing submitted is public before approval. Proposals are private records owned by their
  proposer and kept out of the D1 mirror; the public reads stubs that name no content, rejected content
  stays private at every version, and only an approval publishes, as `library.catalog` items.
- Storage is bounded: hourly attempt limits, per-item and per-pack sizes, and the daily byte budgets.
- Content checks never run in the primary fabric object, so a slow check cannot hold `main`.
- While `LIBRARY_PROPOSALS_ENABLED` is off, the library tools leave `tools/list`, the agent pages and
  `connect.json`, and the A2A card answers 404.
- Generic writes cannot touch the library: enqueue and split into the library queues are refused,
  put_records into `library.proposals` and `library.catalog` is refused for every key including
  admins, and `library.instructions` takes admin writes only (`cloud_library.js:libraryGuard`).
- Nothing is executed, scheduled or activated. A verb template is stored with
  `queue_placeholder: library.templates`; no such queue exists and nothing enqueues it.
- No model is called and no judgment is made server-side; the reviewer is an authorized worker.

Code: `deploy/cloudflare/cloud_library.js`, `cloud_library_reads.js`, `library_checks.js`,
`library_contract.js`, `library_io.js`, `cloud_a2a.js`. Tests: `tests/workerd_library_doors.mjs` and
`tests/workerd_library_checks.mjs`. The Python service does not serve the door yet; the hosted
fabric is the Worker. After a code rollback the seal on the library stores is gone, so check catalog
provenance (both receipts, two different keys) before turning the door back on.
