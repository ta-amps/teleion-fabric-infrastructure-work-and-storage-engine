# Tessera: setup

Every command here runs on a fresh clone. Day-to-day running (keys, the reaper, backups, what to do when
something looks wrong) is in [operations.md](operations.md).

## Requirements

| Tool | Version | Why |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | recent | Installs Python 3.12 and the dependencies |
| make, git, bash | any | The targets below |
| Node.js with npx | 22 or newer | Only for the local edge (`make dev`) and deploys; runs `wrangler@4.149.0` |
| cloudflared | any | Only for a temporary public address (`make dev TUNNEL=1`) |
| flyctl, openssl | any | Only for hosting |

## Install and check

```bash
git clone https://github.com/ta-amps/tessera.git
cd tessera
make setup          # uv sync: Python 3.12 and every dependency
make check          # lint, format, mypy strict, tests, leak scan, suite lock, indexes, architecture.json
```

## Try it with the CLI (no service)

Several workers can share one SQLite file through the CLI, because SQLite serializes writers.

```bash
export TESSERA_DB=.tessera/try.db
uv run tessera init
uv run tessera define translate --heartbeat 30 --lease 900
uv run tessera enqueue translate --id guide-es --kind translate --effect write-internal \
  --brief '{"source": "guide.md", "to": "es"}'
uv run tessera claim translate --worker agent-a        # prints the lease: task_id, fencing_token, brief
uv run tessera submit guide-es --token 1 --output '{"text": "..."}' --idempotency-key guide-es-1
uv run tessera counts
```

[generalizing.md](generalizing.md) walks the same example through split and merge.

## Run the service locally

```bash
make serve                 # the service alone on 127.0.0.1:7700: no edge, no origin lock
make dev                   # the service behind the real Cloudflare edge code (wrangler dev), origin locked
make dev TUNNEL=1          # the same plus a temporary public https://*.trycloudflare.com address
make seed                  # in another terminal: queues, one key per agent, first tasks
```

`make dev` starts the service on 127.0.0.1:7700 and the edge on 127.0.0.1:7701 with a fresh per-run edge
secret, so requests reach the service only through the edge, as in production. Ctrl-C stops everything it
started. `make seed` writes keys to `~/.config/tessera/keys-tessera.json` (mode 600) and never prints them.

More keys: `uv run tessera --db .tessera/tessera.db keys issue --label my-agent --queue research --scope store`.
The key prints once; keep it in the agent's environment or secret store.

## Connect agents

| Agent | Connects to | Needs |
|---|---|---|
| MCP clients | `<address>/mcp` (Streamable HTTP); verb tools (`queues`, `enqueue`, `claim`, `heartbeat`, `submit`, `split`, `park`, `resume`), noun tools (`list_stores`, `list_records`, `put_record`, `put_records`, `get_record`, `record_history`), and bucket tools (`define_bucket`, `buckets`, `put_object`, `get_object`, `list_objects`, `object_history`) | `Authorization: Bearer <key>` |
| HTTP clients | `POST <address>/v1/tasks/claim`, `/heartbeat`, `/submit`, `/split`, `/park`, `/resume`, and `/v1/tasks` to queue work (with `depends_on` for joins) | The same header; `Idempotency-Key` on submit |
| Python | `tessera.client.Client(url, key)`: the same interface as the in-process `Store` | The key |
| Browser-only agents | `<address>/work`: keyed forms that ask for the key at every step and never echo it | The key |
| People | `<address>/connect`, `<address>/board`, `<address>/llms.txt` | Nothing |

## Configuration

Names only here; values go in `.env` (gitignored) or the host's secret store.

| Setting | Default | Meaning | Secret |
|---|---|---|---|
| `TESSERA_DB` | `.tessera/tessera.db` | SQLite file for the store, CLI and service | no |
| `TESSERA_EDGE_SECRET` | unset (lock off) | When set, every request except the health checks needs a matching `x-edge-auth` | yes |
| `TESSERA_CLAIMS_PER_MINUTE` | 60 | Claim rate limit per key | no |
| `TESSERA_REAPER_SECONDS` | 30 | How often the service reaps lapsed leases | no |
| `TESSERA_PUBLIC_URL` | `http://localhost:8080` | The address the agent pages advertise | no |
| `TESSERA_ALLOWED_HOSTS` | empty | Comma-separated hosts; when set, the MCP door turns on DNS-rebinding protection for them | no |
| `TESSERA_SERVICE_PORT`, `TESSERA_EDGE_PORT` | 7700, 7701 | Ports for `make dev` | no |
| `TESSERA_URL`, `TESSERA_KEY` | unset | Where a worker or the HTTP suite finds the service, and the worker's key | the key is |
| `TESSERA_TEST_KEYS` | unset | JSON of keys for `make suite BACKEND=http` | yes |
| `TESSERA_BACKEND_FACTORY` | set by `make suite` | `module:function` the suite loads its backend from | no |
| `MODEL_API_KEY` | unset | Only for workers that run model jobs; the service never reads it | yes |

Queue settings (heartbeat, misses, lease cap, attempts, backoff) are per queue: `tessera define --help` and
SPEC §4.

## Run the conformance suite

```bash
make suite BACKEND=store       # on the in-process store
make suite-stub                # proves the suite fails against an empty backend, as it must
make suite-http-local          # a real local service with real keys; the suite over HTTP (3 clock or restart tests skip)
make suite BACKEND=http        # against TESSERA_URL with TESSERA_TEST_KEYS
```

## Deploy

1. Create Cloudflare and Fly.io accounts and log in: `fly auth login`, `npx wrangler@4.149.0 login`.
2. Check the names in `deploy/hosting.env`, then `deploy/bootstrap-hosting.sh --dry-run` and the same without
   `--dry-run`. It creates the Fly app and its volume and sets one edge secret on both sides through stdin.
3. In GitHub, create the `production` environment with a required reviewer and add `FLY_API_TOKEN`,
   `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` to it.
4. Actions, `deploy`, Run workflow with part `edge`, `service` or `both`, and approve the job.

Details: [deploy/fly/README.md](../deploy/fly/README.md), [deploy/cloudflare/README.md](../deploy/cloudflare/README.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make dev` says a port is in use | Something else listens on 7700 or 7701 | Set `TESSERA_SERVICE_PORT` and `TESSERA_EDGE_PORT` |
| 401 `unauthorized` | No key, or the key is unknown, expired or revoked | Issue a new key; check the `Authorization` header |
| 403 `forbidden` | The key's scope doesn't cover the queue, kind, effect class or store; or the edge secret is missing | `tessera keys list`; reach the service through the edge |
| 409 `stale_lease` | The lease lapsed and someone else holds the task | Heartbeat more often than `heartbeat_seconds`; drop the work |
| 429 `rate_limited` | Over 60 claims a minute for this key | Wait `Retry-After` |
| 204 on claim | Nothing ready in that queue | Wait `Retry-After`; check `/board` |
| The tunnel address stopped working | Quick tunnels change every run | Use the new address `make dev` printed, or deploy |
