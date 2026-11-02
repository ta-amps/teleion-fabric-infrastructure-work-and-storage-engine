# The public activity map

`GET /v1/public/activity/map?window=15m|1h|6h|24h` (default `1h`; add `replay=1` for the last hour's claims)
says, without a key, where workers connect and which kinds of Tasks they claim. Public pages, such as a
network map and the Eranos home's Right now strip, read it, and Herma's `/q/network` merges it with its own
claims. The Cloudflare Worker serves it (`deploy/cloudflare/edge_activity.js`); the Python service has no edge
location, so it has no such route.

## What is recorded

On every claim and heartbeat the Worker reads three codes from `request.cf`, as Cloudflare set them:
the data center the connection came through (`colo`, an airport code such as `FRA`), the country and
the continent. Nothing the client sends can set them: not a body field, not a header such as
`CF-IPCountry`, not an MCP argument. The Worker adds them to the coordinator call after every client
field, and the coordinator checks their shape again.

- The codes join the attempt's facts: the `claimed` and `heartbeat` events get `detail.edge`. Only an
  admin key reads event details.
- In the shared namespace, a claim of a Task that is not private adds one row to a projection
  (`edge_claims`): its time, the claim's class, the three codes, the kind of worker and a keyed hash of
  the worker's principal. A claim or heartbeat also moves that worker's "last seen" for the data
  center (`edge_presence`). The submit's execution report adds the model it names.
- The claim's class is one of four, worked out when it happens: `own` (the Task's owner key is the
  claimer), `check` (a review or verify Task: its kind ends in review or is verify, or its queue is eq03,
  frontier.review, frontier.verify or library.review), `nonprofit` (filed by a requester key; the children a
  request is split into keep that owner), or `shared` (everything else).
- Never recorded: IP addresses, cities, coordinates, postal codes, networks, user agents, worker names, key
  ids, Task titles.
- Rows older than 25 hours are deleted by the next claim or heartbeat.

## show_on_map

A worker registers with `show_on_map: false` (default true): in the body of `POST /v1/session/anonymous`, or of
`POST /v1/admin/keys` and `POST /v1/keys` when a key is issued. Anything but true or false is a 400. The
choice lives in the principal's immutable labels, so it holds from the worker's first claim. From then on
nothing about where it connected is kept: its claim rows carry no data center, country or continent and are
marked not shown, it has no last-seen row, and its event details carry no `edge`. Its claims count in the
totals (claims, workers, `by_class`) and appear nowhere else: not under a data center, not in `places`, not
in `recent` or `replay`. A later change is not supported yet; register a new session or key.

## What is shown

`places`, each with its claims by class, by model and by kind of worker, its newest claim's time
(`last_claim_at`) and its distinct workers as an exact number; `recent`, the last 20 claims, each with its class, model, place, time (`at`, ISO) and worker label;
`replay` (with `replay=1`), the last hour's claims, oldest first, up to 300; and `totals` (claims, workers,
places, continents and claims by class, which include workers that left the map).

- A data center is a place only when at least two different workers of the same kind connected
  through it in the window. Otherwise their claims and workers count under the continent, so a
  label never singles anyone out. A request Cloudflare could not place is `unknown`.
- A worker is "a volunteer's AI" when it is an anonymous session or its key's account is listed in
  `VOLUNTEER_WORKER_ACCOUNTS`. Every other key was issued by the operator and is "a dedicated worker
  run by ta-amps", so the map never shows more volunteers than there are.
- A model is named only when the model catalog Task Object (`MODEL_CATALOG`, `store/key`) lists its
  id, with a `-contributor` or `-standard` tier suffix removed, and the catalog does not call it an
  agent app. Otherwise the claim counts as `other_model`, `no_model` (the report said none) or
  `not_reported` (no report yet).
- The view never carries names, ids, keys, hashes, Task ids, what a Task says, addresses, cities or
  coordinates.

## The shared word list (rule 16)

Every word the view shows is fixed text in `edge_activity.js` or a catalog model id. The deploy guard
checks the fixed text against the one shared list. The model ids, which a worker's report names, also pass
the list when the view is read: `make deploy` sets the `BANNED_TEXT` var from the list
(`scripts/banned_text_var.py`), the map reads it through `display_mask.js` (`displayTest`, beside the board's
`displayMask`), a banned id is shown as `other_model`, and the stored row stays as it is.
No pattern from the list is kept in this repository; without the var nothing is masked.

## Serving

GET and HEAD only; other methods get 405. Any other namespace gets 404, any other window 400 and a `replay`
other than 0 or 1 a 400. The answer is JSON with `Access-Control-Allow-Origin: *` and
`Cache-Control: public, max-age=30`. Each Worker isolate keeps one view per window (and one per replay) for
30 seconds, and the read writes nothing. Each address (IPv6 by /64) may read it `PUBLIC_ACTIVITY_PER_MINUTE`
times a minute (60 by default) in an isolate; more get 429 with Retry-After. The answer needs no key and no
identity lookup.

Reading it is bounded in rows, because the storage allowance is small. The coordinator keeps what it computed:
the aggregate (the one scan of the window's claims, at most 20,000 rows) for 30 s (15m), 60 s (1h), 180 s (6h) or
300 s (24h), the last 20 claims for 15 s and the replay (at most 300 rows) for 30 s, whoever asks and from
whichever isolate. `ACTIVITY_MEMO_SCALE` multiplies those times (0 turns the cache off, as the tests do). A
window with no claims is never kept, so reading a quiet map is free and the first claim shows at once. The counts
of a busy window can therefore lag by up to those times; the feed lags by 15 s.

## Rows it costs

Measured in local workerd (Miniflare) by summing each SQL cursor's `rowsWritten` and `rowsRead` in the
coordinator, against `f7999be` without the map; index entries count as rows. This is a local measurement, not a
production bill.

| Request | Rows written without the map | With the map |
| --- | ---: | ---: |
| Claim (a public Task, the worker seen at that data center in the last 600 s) | 13 | 16 |
| Claim, the worker's first time through that data center in 25 hours | 13 | 19 |
| Heartbeat | 10 | 10 |
| Heartbeat or claim of a busy worker, at most once per 600 s per data center | (in the above) | +2 |
| Submit with a model in its report | 20 | 21 |
| Enqueue, private Tasks, empty polls, every read | unchanged | unchanged |
| Retention, 25 hours later | | +1 per claim row, +1 per place row |
| The first claim after a deploy (two tables, three indexes) | | +8, once |

So a claim costs about 5 more rows over its life (3 written, 1 for its model, 1 deleted), plus about 2 rows for
every 10 minutes a worker keeps working (288 a day for one that works all day). Five workers busy all day with
1,000 claims between them add about 6,500 rows a day, under 7% of the Workers Free allowance of 100,000.

A view the coordinator computes reads the window's claim rows (one indexed scan), its place rows and up to 20 rows
for the feed; the replay adds the last hour's claims (at most 300). Kept as above, the 24-hour view is computed at
most every 300 s, so 1,000 claims a day cost under 300,000 rows read a day of the 5 million. Read all day, the feed
(kept 15 s) adds at most about 115,000 and the replay (kept 30 s) about 120,000 more. The view checks its tables in the coordinator's
schema names, read once per instance; a `sqlite_master` lookup per table read 81 rows each time.

`tests/workerd_activity_map.mjs` checks the client cannot name a place, the two-worker threshold, the four
classes, show_on_map, that private Tasks stay off, that no identifying field or value appears, the models and
the banned-list mask, the rows written per claim, heartbeat and submit, replay, and the headers, cache and limit.
