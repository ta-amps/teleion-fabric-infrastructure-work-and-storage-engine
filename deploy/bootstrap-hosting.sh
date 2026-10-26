#!/usr/bin/env bash
# One-time hosting setup for one service on Fly.io behind a Cloudflare Worker: the Fly app and its
# volume, one fresh edge secret stored on both sides, and any extra secrets typed with hidden input.
# Usage: deploy/bootstrap-hosting.sh [--dry-run] [--secrets-only] [--config FILE]
# Settings come from deploy/hosting.env (KEY=value lines); FLY_ORG and FLY_REGION may be overridden.
# Log in first with `fly auth login` and `npx wrangler@4.149.0 login`.
# It prints names and next steps only, never a secret, and it deploys nothing.
set -euo pipefail
umask 077

WRANGLER_VERSION="4.149.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONFIG="$HERE/hosting.env"
DRY_RUN=0
SECRETS_ONLY=0
FAILED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --secrets-only) SECRETS_ONLY=1 ;;
    --config) CONFIG="${2:?--config needs a file}"; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# Read KEY=value lines without sourcing the file, so a settings file can never run code. Only FLY_ORG
# and FLY_REGION may come from the environment; everything else comes from the file.
KEYS="APP VOLUME VOLUME_GB FLY_ORG FLY_REGION EDGE_SECRET_NAME EDGE_CONFIG PROMPT_SECRETS CUSTOM_DOMAIN HEALTH_URL GITHUB_REPO"
ENV_FLY_ORG="${FLY_ORG:-}"
ENV_FLY_REGION="${FLY_REGION:-}"
for key in $KEYS; do printf -v "$key" '%s' ""; done
[ -f "$CONFIG" ] || { echo "settings file not found: $CONFIG" >&2; exit 2; }
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  case "$line" in *=*) ;; *) continue ;; esac
  key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
  value="$(printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case " $KEYS " in
    *" $key "*) printf -v "$key" '%s' "$value" ;;
    *) echo "ignoring unknown setting: $key" >&2 ;;
  esac
done < "$CONFIG"
for key in APP VOLUME EDGE_SECRET_NAME EDGE_CONFIG; do
  [ -n "${!key}" ] || { echo "$CONFIG must set $key" >&2; exit 2; }
done
VOLUME_GB="${VOLUME_GB:-1}"
FLY_ORG="${ENV_FLY_ORG:-${FLY_ORG:-personal}}"
FLY_REGION="${ENV_FLY_REGION:-${FLY_REGION:-iad}}"
PROMPT_SECRETS="${PROMPT_SECRETS:-}"
CUSTOM_DOMAIN="${CUSTOM_DOMAIN:-}"
HEALTH_URL="${HEALTH_URL:-the Worker URL}"
GITHUB_REPO="${GITHUB_REPO:-this repo}"
EDGE_CONFIG_PATH="$REPO/$EDGE_CONFIG"
for name in $EDGE_SECRET_NAME $PROMPT_SECRETS; do
  case "$name" in *[!A-Z0-9_]*) echo "secret names are A-Z, 0-9 and _ only: $name" >&2; exit 2 ;; esac
done
if [ -n "$CUSTOM_DOMAIN" ]; then
  case "$CUSTOM_DOMAIN" in
    *[!a-z0-9.-]*|.*|*.|*..*) echo "CUSTOM_DOMAIN must be a hostname like app.example.org: $CUSTOM_DOMAIN" >&2; exit 2 ;;
  esac
fi

wrangler() { npx --yes "wrangler@$WRANGLER_VERSION" "$@"; }

plan() {
  cat <<EOF
Plan for $APP (nothing secret is printed, nothing is deployed):
  1. Check tools (fly, npx with Node 22+, openssl) and the Fly.io and Cloudflare logins.
  2. Fly app $APP (org $FLY_ORG, region $FLY_REGION) with volume $VOLUME, $VOLUME_GB GB.
  3. A fresh random edge secret, stored as $EDGE_SECRET_NAME on $APP and as EDGE_SECRET on the
     Worker in $EDGE_CONFIG. Secrets go through stdin only, never the command line.
EOF
  [ -z "$CUSTOM_DOMAIN" ] || echo "  4. Custom subdomain in $EDGE_CONFIG: https://$CUSTOM_DOMAIN (Cloudflare creates DNS + TLS on deploy)."
  [ -z "$PROMPT_SECRETS" ] || echo "  5. Typed with input hidden (Enter skips): $PROMPT_SECRETS"
}

next_steps() {
  cat <<EOF

Next:
  1. In GitHub ($GITHUB_REPO): Settings > Environments > New environment "production", add yourself as
     a required reviewer, then add these environment secrets:
       FLY_API_TOKEN          run: fly tokens create deploy --app $APP
                              and paste the output straight into GitHub (don't save it anywhere)
       CLOUDFLARE_API_TOKEN   Cloudflare > My Profile > API Tokens > template "Edit Cloudflare Workers"
       CLOUDFLARE_ACCOUNT_ID  Cloudflare > Workers & Pages (shown in the sidebar)
  2. Actions > deploy > Run workflow (deploy/fly/README.md lists the parts). Approve each job when asked.
  3. Once deployed, check $HEALTH_URL
EOF
}

if [ "$DRY_RUN" -eq 1 ]; then
  plan
  next_steps
  exit 0
fi

FLY="$(command -v fly || command -v flyctl || true)"
[ -n "$FLY" ] || { echo "Fly.io CLI not found: https://fly.io/docs/flyctl/install/" >&2; exit 1; }
command -v npx >/dev/null || { echo "npx not found: install Node 22 or newer" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }
[ -f "$EDGE_CONFIG_PATH" ] || { echo "edge config not found: $EDGE_CONFIG_PATH" >&2; exit 1; }

"$FLY" auth whoami >/dev/null 2>&1 || { echo "Not logged in to Fly.io: run  fly auth login" >&2; exit 1; }
if ! whoami_out="$(wrangler whoami 2>&1)" || grep -qi "not authenticated" <<<"$whoami_out"; then
  echo "Not logged in to Cloudflare: run  npx wrangler@$WRANGLER_VERSION login" >&2
  exit 1
fi
unset whoami_out
echo "Logged in to Fly.io and Cloudflare."

if [ "$SECRETS_ONLY" -eq 0 ]; then
  echo "Fly.io app and volume:"
  if "$FLY" apps list --json 2>/dev/null | grep -q "\"$APP\""; then
    echo "  app $APP: exists"
  else
    "$FLY" apps create "$APP" --org "$FLY_ORG" >/dev/null
    echo "  app $APP: created"
  fi
  if "$FLY" volumes list --app "$APP" --json 2>/dev/null | grep -q "\"$VOLUME\""; then
    echo "  volume $VOLUME: exists"
  else
    "$FLY" volumes create "$VOLUME" --app "$APP" --region "$FLY_REGION" --size "$VOLUME_GB" --yes >/dev/null
    echo "  volume $VOLUME: created"
  fi
fi

echo "Edge secret:"
secret="$(openssl rand -hex 32)"
printf '%s=%s\n' "$EDGE_SECRET_NAME" "$secret" | "$FLY" secrets import --app "$APP" --stage >/dev/null
if printf '%s' "$secret" | wrangler secret put EDGE_SECRET --config "$EDGE_CONFIG_PATH" >/dev/null 2>&1; then
  echo "  $EDGE_SECRET_NAME on $APP and EDGE_SECRET on its Worker: set to one fresh value (not shown)"
else
  echo "  $EDGE_SECRET_NAME is set on $APP, but the Worker secret failed (the Worker may not exist yet)." >&2
  echo "  Deploy the edge once, then rerun with --secrets-only so both sides get a matching value." >&2
  FAILED=1
fi
unset secret

for name in $PROMPT_SECRETS; do
  read -rs -p "  $name for $APP (input hidden, Enter to skip): " value
  echo
  if [ -n "$value" ]; then
    printf '%s=%s\n' "$name" "$value" | "$FLY" secrets import --app "$APP" --stage >/dev/null
    echo "  $name on $APP: set (not shown)"
  else
    echo "  $name: skipped"
  fi
  unset value
done

next_steps
exit "$FAILED"
