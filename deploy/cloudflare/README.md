# Cloudflare: Teleion Fabric cloud runtime and edge

The `tessera` Worker (`deploy/cloudflare/wrangler.jsonc`) supports two modes controlled by `STORAGE_MODE`:

1. **`STORAGE_MODE = "cloud"` (default in production at `https://fabric.teleionlabs.com`)**:
   - Executes Teleion Fabric natively 24/7 in Cloudflare's cloud with zero dependency on any external VM or tunnel.
   - **`TesseraCoordinator` (SQLite-backed Durable Object)** serializes single-writer ACID verb transitions
     (`enqueue`, `claim`, `heartbeat`, `submit`, `split`, `park`, `resume`, `cancel`), fencing tokens, `alarm()`
     lease reaping, compare-and-set noun records (`stores`, `list_records`, `put_record`, `put_records`,
     `get_record`, `record_history`), and named bucket object versions (`nouns`, `verbs`, `kits`, `cases`,
     `provisions`, `traces`).
   - **`FABRIC_DB` (`ta-amps-fabric` D1)** mirrors head noun records and queue counts for cross-surface reads via
     Cloudflare Service Bindings.
   - **`BUCKETS_DB` (`ta-amps-buckets` D1)** stores content-addressed SHA-256 blobs in 256 KiB chunks (up to 16 MiB
     per object) with optional `BLOBS_BUCKET` R2 write-through.
   - Serves the full HTTP `/v1/*` contract, Streamable HTTP `/mcp` (20 verb + noun + bucket tools), `/board`,
     `/work`, `/connect`, `/llms.txt`, and isolated `/ns/<namespace>` test coordinators.
2. **`STORAGE_MODE = "proxy"` (local `make dev` / Fly.io origin mode)**:
   - Acts as the locked-down reverse proxy in front of `ORIGIN` (`http://127.0.0.1:7700` or Fly.io):

- `Authorization` passes through untouched, so worker keys work exactly as against the origin;
- `x-edge-auth`, `x-forwarded-host` and `x-forwarded-proto` are added; hop-by-hop headers and cookies
  are dropped;
- streaming responses (MCP over Streamable HTTP) pass straight through;
- writes are never cached, and reads are `no-store` unless the service says otherwise;
- every response gets HSTS, `nosniff`, a strict referrer policy, a permissions policy that denies
  camera, microphone and geolocation, and `X-Frame-Options: DENY`.

**Content security policy.** If the service sends its own policy, the edge keeps it. Otherwise data
responses get `default-src 'none'`, and HTML pages (`/board`, `/work`, `/connect`) get a policy that
allows files and inline styles from the same origin but **no inline scripts**. A page that needs an
inline script should send its own policy with a hash or nonce for that script.

With the free hostname the edge is `https://tessera.<your-subdomain>.workers.dev`; workers point
`TESSERA_URL` there.

## Origin lockdown

`EDGE_SECRET` on this Worker and `TESSERA_EDGE_SECRET` on the Fly app hold the same random value. With it
set, the service answers 403 `forbidden` to any request without a matching `x-edge-auth`, except
`GET /v1/health` and `GET /healthz` (health checks). See SPEC §7.
`deploy/bootstrap-hosting.sh` sets both sides without printing the value.

## Deploy

Deploys need Taylor's approval each time: run the GitHub `deploy` workflow with part `edge` (it waits
in the `production` environment), or by hand
`npx wrangler@4.149.0 deploy --config deploy/cloudflare/wrangler.jsonc`.
