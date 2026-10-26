# Deploy

- **Fly.io** (`deploy/fly/`): the service on one always-on machine with a volume for SQLite, health
  checks on `/v1/health`, secrets through `fly secrets set`. Brief L20, run by Taylor.
- **Cloudflare alternative**: a Worker with one Durable Object per queue, its alarm running the reaper,
  and the same schema on D1. More platform to learn under a deadline; kept as an option.

Fly's acceptable use policy bans security testing, so soak and restart tests (T13, T14) run only
against our own app.
