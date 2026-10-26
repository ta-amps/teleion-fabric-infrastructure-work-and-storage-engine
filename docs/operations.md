# Operations

## Local, today

```bash
export TESSERA_DB=.tessera/tessera.db
uv run python tools/load_briefs.py                 # queue the briefs whose dependencies are done
uv run python tools/muse_worker.py --worker muse-01  # one Muse Code worker, one brief
uv run tessera counts && uv run tessera events --after 0
```

Run several workers in separate terminals; SQLite serializes their writes, and leases keep them apart.
Rerun `load_briefs.py` after tasks finish to release the briefs that depended on them.

## Keys (after brief L7)

- Issue one key per worker, scoped to its queues, task kinds and effect classes; keys expire in 14 days.
- The key prints once at issue time. Store it in the worker's environment or the host's secret store,
  never in a repo or a chat.
- No key holds the `public` effect class. Revoke a key the moment a worker misbehaves.

## Reaper

The service runs the reaper at least once a minute (`reaper_last_run` in health). Locally, claims reap
their own queue first, and `uv run tessera reap` reaps everything.

## Backups and restore (with the deploy, brief L20)

- SQLite in WAL mode on the machine's volume. Copy it off the machine nightly with SQLite's online
  backup (`sqlite3 tessera.db ".backup 'backup.db'"`), never by copying the live file.
- Drill one restore before the demo: stop the service, swap in the backup, run `make suite BACKEND=http`.

## When something looks wrong

| Signal | Look at | Usual cause |
|---|---|---|
| Expired leases climbing in health | `tessera events` for `lease_lapsed` | A worker stopped heartbeating |
| Dead letters | `tessera results <task>` | The same failure three times: read the errors |
| Counts look off | `recount` against `counts` | Should never differ; if they do, stop and file a bug |
