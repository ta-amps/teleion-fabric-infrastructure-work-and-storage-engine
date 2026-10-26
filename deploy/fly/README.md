# Fly.io: the Tessera service

One always-on machine (`auto_stop_machines = "off"`, `min_machines_running = 1`) with a volume for
SQLite, because a queue with leases must keep its reaper running. It listens on 8080; Fly's health check
calls `GET /v1/health`. It sits behind the Cloudflare edge (worker `tessera`); with `TESSERA_EDGE_SECRET`
set, it answers 403 `forbidden` to any request without a matching `x-edge-auth`, except `GET /v1/health`
and `GET /healthz` (SPEC §7).

Secrets live only in Fly (`fly secrets import` from stdin): `TESSERA_EDGE_SECRET`. Worker keys are issued
from the command line on the machine and are stored only as hashes.

## First time

1. Create a Fly.io account for ta-amps and run `fly auth login`.
2. From the workspace root run `hackathon-context/scripts/bootstrap-hosting.sh`. It creates the app and
   its `tessera_data` volume and sets the edge secret on both sides without printing it.

## Deploy

Deploys need Taylor's approval each time. Use the GitHub `deploy` workflow (part `service`): it waits in
the `production` environment, copies `deploy/fly/Dockerfile.dockerignore` to the context root, and runs
`flyctl deploy --remote-only`. By hand, from a fresh clone of this repo only:
`cp deploy/fly/Dockerfile.dockerignore .dockerignore && fly deploy --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile --remote-only`.

Then: `make suite BACKEND=http` against the edge URL with test keys (task L20's done-when).

## Names and judging week

- Fly app names are global. If `tessera-fabric` is taken, change `app` in `fly.toml`, `ORIGIN` in
  `deploy/cloudflare/wrangler.jsonc`, and set `TESSERA_APP` for the bootstrap script.
- Keep `min_machines_running = 1` through judging week (Nov 4 to 9) and beyond: the reaper needs a
  running machine.
- Back up the SQLite file nightly with SQLite's online backup (docs/operations.md) and drill one restore
  before the demo.
