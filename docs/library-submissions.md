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
| Noun | `library.proposals/<proposal_id>` | Version 1: the proposal with its checks, digests and receipts, public. Version 2: the decision; the content leaves the head and stays in version 1. |
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
| Status | `GET /v1/library/proposals/<id>`, MCP `proposal_status`, A2A `tasks/get` | none |

A bundle is `{"format": "ta-amps.seed-pack.bundle.v1", "license", "source", "files"}`: `source` is
the pack's `source.json`, `files` are `{path, content}` text files: `README.md` and, optionally,
everything under `code/`, as in a library pack folder. The library's `scripts/bundle_pack.py` writes one.

A contributor key is an operator-issued key whose queues include `library.propose`. A tracked
session comes from `POST /v1/session/anonymous` without any secret.

## The checks at the door

Deterministic, over the JSON only; submitted code is measured and scanned as text, never parsed,
imported or run. A refusal stores nothing, returns every finding with its rule and place (never the
matched text) and a digest of the refused body, and is counted in the access audit.

| Rule | Limit |
|---|---|
| `schema` | a registered noun or verb category, slugs, titles of at most 400 characters, steps (1 to 40), acceptance checks (1 to 20), pack id and version |
| `size` | one item 32 KiB; a pack 512 KiB with a contributor key, 256 KiB from a tracked session; 64 files of at most 128 KiB; 120 nouns and 40 verbs |
| `license` | MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, CC0-1.0 or CC-BY-4.0 |
| `file_type` | `README.md` and `code/...` only; plain relative paths with no hidden folders such as `.github/` (a `.gitignore` file is allowed); text types only (md, txt, json, jsonl, yaml, yml, toml, csv, tsv, py, js, mjs, cjs, ts, sh, cfg, ini, lock, Makefile) |
| `hidden_characters` | no control characters and no bidirectional overrides |
| `secrets` | private keys, cloud, code-host, chat, payment, model-provider and Tessera keys, JWTs, bearer headers, credentials in URLs and quoted secret assignments |
| `personal_data` | the fabric's existing screen for email addresses, phone numbers, street addresses, identity, card and IBAN numbers, after removing what cannot be personal: @names with no local part (decorators), mail at reserved names (example.com/net/org, .example, .invalid, .test, .localhost), 555-01xx and 555-area numbers, and 13 to 19 digit runs that fail the Luhn check |

Rates: a tracked session files at most 3 proposals an hour (6 per address hash, 60 for all
sessions); a contributor key 30 an hour. Attempts count, so a refused body costs an attempt. An
item already in the catalog with identical content is reused on admission (a pack may repeat a
verb admitted on its own); the same key with other content is refused (409), so a revision is a
new version, and a proposal with nothing new is refused too.

## The review verb

The reviewer claims `library.review`, reads the proposal (`proposal_url` in the brief), checks it as
data, and submits:

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

The exception is narrow: one route family, one module, two queues and three stores in the shared
namespace, gated by `LIBRARY_PROPOSALS_ENABLED`.

- Anonymous content becomes public here, which the demo namespace never allows. It is limited to
  `library.proposals`, labeled `origin:anonymous` and `untrusted:true`, screened and size- and
  rate-limited, served as JSON only, and leaves the public head once decided.
- Generic writes cannot touch the library: enqueue and split into the library queues are refused,
  put_records into `library.proposals` and `library.catalog` is refused for every key including
  admins, and `library.instructions` takes admin writes only (`cloud_library.js:libraryGuard`).
- Nothing is executed, scheduled or activated. A verb template is stored with
  `queue_placeholder: library.templates`; no such queue exists and nothing enqueues it.
- No model is called and no judgment is made server-side; the reviewer is an authorized worker.

Code: `deploy/cloudflare/cloud_library.js`, `library_checks.js`, `library_contract.js`,
`cloud_a2a.js`. Tests: `tests/workerd_library_doors.mjs`. The Python service does not serve the door
yet; the hosted fabric is the Worker.
