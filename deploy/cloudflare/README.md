# Cloudflare: the Tessera edge

One Worker named `tessera` is the reverse proxy in front of the Tessera service on Fly.io. It forwards
every path (`/v1/*`, `/mcp`, `/board`, `/work`, `/connect`, `/llms.txt`, `/.well-known/agent-card.json`,
`/healthz`) to `ORIGIN` (default `https://tessera-fabric.fly.dev`):

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
`hackathon-context/scripts/bootstrap-hosting.sh` sets both sides without printing the value.

## Deploy

Deploys need Taylor's approval each time: run the GitHub `deploy` workflow with part `edge` (it waits
in the `production` environment), or by hand
`npx wrangler@4.149.0 deploy --config deploy/cloudflare/wrangler.jsonc`.
