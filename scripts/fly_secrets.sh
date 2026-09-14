#!/usr/bin/env bash
# Push the backend's secrets from the local .env + service_account.json to the Fly app.
# Run after `fly launch`; re-run any time a value changes. Secrets never touch git.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
SA_FILE="${GOOGLE_SERVICE_ACCOUNT_FILE:-$ROOT/service_account.json}"
cd "$ROOT/backend"

[[ -f "$ENV_FILE" ]] || { echo "no .env at $ENV_FILE"; exit 1; }
[[ -f "$SA_FILE" ]]  || { echo "no service account file at $SA_FILE"; exit 1; }
command -v fly >/dev/null || { echo "fly CLI not installed (brew install flyctl)"; exit 1; }

get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

APP="$(grep -E "^app *= *['\"]" fly.toml | sed -E "s/.*['\"]([^'\"]+)['\"].*/\1/")"
URL="https://${APP}.fly.dev"

args=(
  "OPENAI_API_KEY=$(get OPENAI_API_KEY)"
  "OPENAI_MODEL=$(get OPENAI_MODEL)"
  "OPENAI_REASONING_EFFORT=$(get OPENAI_REASONING_EFFORT || true)"
  "GRIDCOACH_API_KEY=$(get GRIDCOACH_API_KEY)"
  "STRAVA_CLIENT_ID=$(get STRAVA_CLIENT_ID)"
  "STRAVA_CLIENT_SECRET=$(get STRAVA_CLIENT_SECRET)"
  "STRAVA_VERIFY_TOKEN=$(get STRAVA_VERIFY_TOKEN)"
  "PUBLIC_BASE_URL=$URL"
  "GOOGLE_SERVICE_ACCOUNT_JSON=$(base64 < "$SA_FILE" | tr -d '\n')"
)
# drop any that came back empty (optional keys)
final=(); for kv in "${args[@]}"; do [[ -n "${kv#*=}" ]] && final+=("$kv"); done   # drop only truly empty values (base64 ends in "=")

echo "Setting ${#final[@]} secrets on Fly app '$APP' (PUBLIC_BASE_URL=$URL)…"
fly secrets set "${final[@]}" --app "$APP"
echo
echo "Next:"
echo "  fly deploy"
echo "  then in the sheet: GridCoach → Configure backend… → $URL"
echo "  Strava app → Authorization Callback Domain → ${APP}.fly.dev"
