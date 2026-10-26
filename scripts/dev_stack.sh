#!/usr/bin/env bash
# Run Tessera on this machine behind the real Cloudflare edge code (`wrangler dev`), with the origin
# locked to the edge exactly as in production. No Cloudflare or Fly.io account is needed.
#
#   scripts/dev_stack.sh             service on 127.0.0.1:7700, edge on 127.0.0.1:7701
#   scripts/dev_stack.sh --tunnel    also a temporary public https://*.trycloudflare.com address
#                                    (a Cloudflare quick tunnel) so web-based agents can connect
#
# Ctrl-C stops everything this script started. Ports: TESSERA_SERVICE_PORT, TESSERA_EDGE_PORT.
# Database: TESSERA_DB (default .tessera/dev.db). Seed keys and tasks with tools/seed.py.
set -euo pipefail

SERVICE_PORT=${TESSERA_SERVICE_PORT:-7700}
EDGE_PORT=${TESSERA_EDGE_PORT:-7701}
DB=${TESSERA_DB:-.tessera/dev.db}
WRANGLER=wrangler@4.149.0
TUNNEL=0
[[ "${1:-}" == "--tunnel" ]] && TUNNEL=1

cd "$(dirname "$0")/.."
mkdir -p .tessera
for port in "$SERVICE_PORT" "$EDGE_PORT"; do
  if ss -ltn | grep -qE ":${port}\b"; then
    echo "Port $port is already in use. Set TESSERA_SERVICE_PORT / TESSERA_EDGE_PORT to free ports." >&2
    exit 1
  fi
done
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
[[ $TUNNEL == 0 ]] || command -v cloudflared >/dev/null || { echo "cloudflared is required for --tunnel" >&2; exit 1; }

# One edge secret per run, shared by the edge (.dev.vars, gitignored) and the service (env). Never printed.
umask 077
SECRET=$(openssl rand -hex 32)
printf 'EDGE_SECRET=%s\n' "$SECRET" > deploy/cloudflare/.dev.vars

GROUPS_STARTED=()
cleanup() {
  for pgid in "${GROUPS_STARTED[@]}"; do kill -- "-$pgid" 2>/dev/null || true; done
  rm -f deploy/cloudflare/.dev.vars
}
trap cleanup EXIT INT TERM

wait_for() {  # wait_for <description> <command...>
  local what=$1; shift
  for _ in $(seq 1 90); do "$@" && return 0; sleep 1; done
  echo "$what did not start; see the logs in .tessera/" >&2
  exit 1
}

PUBLIC_URL="http://127.0.0.1:$EDGE_PORT"
if [[ $TUNNEL == 1 ]]; then
  setsid cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$EDGE_PORT" > .tessera/tunnel.log 2>&1 &
  GROUPS_STARTED+=("$!")
  wait_for "The quick tunnel" grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' .tessera/tunnel.log
  PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' .tessera/tunnel.log | head -1)
fi

TESSERA_EDGE_SECRET="$SECRET" TESSERA_PUBLIC_URL="$PUBLIC_URL" \
  setsid uv run tessera --db "$DB" serve --port "$SERVICE_PORT" > .tessera/service.log 2>&1 &
GROUPS_STARTED+=("$!")
service_up() { [[ "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://127.0.0.1:$SERVICE_PORT/v1/health")" == 200 ]]; }
wait_for "The Tessera service" service_up

setsid bash -c "cd deploy/cloudflare && exec npx --yes $WRANGLER dev --config wrangler.jsonc \
  --ip 127.0.0.1 --port $EDGE_PORT --var ORIGIN:http://127.0.0.1:$SERVICE_PORT" > .tessera/edge.log 2>&1 &
GROUPS_STARTED+=("$!")
edge_up() { [[ "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://127.0.0.1:$EDGE_PORT/v1/health")" == 200 ]]; }
wait_for "The edge" edge_up

cat <<EOF
Tessera is running.
  Edge, the address agents use:   $PUBLIC_URL
  Origin (accepts the edge only): http://127.0.0.1:$SERVICE_PORT
  Connect page: $PUBLIC_URL/connect     Board: $PUBLIC_URL/board     MCP: $PUBLIC_URL/mcp
Seed queues, keys and first tasks (in another terminal):
  uv run python tools/seed.py --db $DB --agents 5
Logs: .tessera/service.log, .tessera/edge.log$( [[ $TUNNEL == 1 ]] && echo ", .tessera/tunnel.log")
Ctrl-C stops everything.
EOF
wait
