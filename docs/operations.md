# Operations

## Local, today (no accounts)

```bash
make dev TUNNEL=1     # service on 127.0.0.1:7700, Cloudflare edge code on 127.0.0.1:7701,
                      # and a temporary public https://*.trycloudflare.com address
make seed             # queues, one key per agent in ~/.config/tessera/keys-tessera.json, first tasks
```

The edge adds a per-run secret that the service requires, so nothing reaches the service except
through the edge, as in production. Ctrl-C stops everything `make dev` started and deletes the secret.
Ports 7700 and 7701 are the defaults because 8000 to 8003, 8787 and 18080 are taken on this machine;
override with `TESSERA_SERVICE_PORT` and `TESSERA_EDGE_PORT`.

The quick tunnel needs no Cloudflare account, has no uptime promise and changes address every run.
Use it to let web-based agents connect while testing; use `deploy/` for a permanent address.

## Muse Code workers

```bash
uv run python tools/load_briefs.py                 # queue the open briefs whose dependencies are done
uv run python tools/muse_worker.py --worker muse-01  # one Muse Code worker, one brief, own worktree
```

## Keys

- `make seed` issues the first set. More: `uv run tessera --db .tessera/tessera.db keys issue --label muse-web-06 --queue research --queue draft --scope store`.
- Each key is scoped to its queues, task kinds and effect classes; keys expire in 14 days. `keys list` and `keys revoke <key_id>` manage them.
- The key prints once at issue time. Store it in the worker's environment or the host's secret store,
  never in a repo or a chat.
- No key holds the `public` effect class. Revoke a key the moment a worker misbehaves.

## Reaper

The service runs the reaper at least once a minute (`reaper_last_run` in health). Locally, claims reap
their own queue first, and `uv run tessera reap` reaps everything.

## Backups and restore (hosted, brief L20)

- SQLite in WAL mode on the machine's volume. Copy it off the machine nightly with SQLite's online
  backup (`sqlite3 tessera.db ".backup 'backup.db'"`), never by copying the live file.
- Drill one restore before the demo: stop the service, swap in the backup, run `make suite BACKEND=http`.

## When something looks wrong

See [storage availability](storage-availability.md) for provider-quota `503`
responses, UTC retry guidance and the distinction between approximate profile
timestamps and per-action audit events.

| Signal | Look at | Usual cause |
|---|---|---|
| Expired leases climbing in health | `tessera events` for `lease_lapsed` | A worker stopped heartbeating |
| Dead letters | `tessera results <task>` | The same failure three times: read the errors |
| Counts look off | `recount` against `counts` | Should never differ; if they do, stop and file a bug |
