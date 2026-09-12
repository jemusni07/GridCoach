#!/usr/bin/env bash
# Bring the whole local dev stack up: tunnel -> .env -> backend.
#
# Cloudflare quick tunnels are disposable (they get revoked, and the URL changes every
# restart), so this re-points PUBLIC_BASE_URL and restarts the backend in one step and
# prints the URL to paste into the sidebar.
#
#   ./scripts/dev.sh          restart everything, print the new URL
#   ./scripts/dev.sh status   show what's running and whether it's publicly reachable
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
LOG_DIR="${TMPDIR:-/tmp}/gridcoach"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
SERVER_LOG="$LOG_DIR/server.log"
PORT="${PORT:-8000}"
CLOUDFLARED="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"

# Keep the venv off iCloud-synced ~/Documents — imports there block for minutes (see README).
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/gridcoach}"

mkdir -p "$LOG_DIR"
current_url() { grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-; }

if [[ "${1:-}" == "status" ]]; then
  url="$(current_url)"
  echo "backend : $(curl -s -m 3 "http://localhost:$PORT/health" || echo 'DOWN')"
  echo "tunnel  : $url -> HTTP $(curl -s -m 15 -o /dev/null -w '%{http_code}' "$url/health" || echo 'unreachable')"
  echo "logs    : $SERVER_LOG"
  exit 0
fi

[[ -x "$CLOUDFLARED" ]] || { echo "cloudflared not found at $CLOUDFLARED (see README)"; exit 1; }

echo "==> stopping old processes"
pkill -f "cloudflared tunnel" 2>/dev/null || true
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 2

echo "==> starting tunnel"
: > "$TUNNEL_LOG"
nohup "$CLOUDFLARED" tunnel --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
URL=""
for _ in $(seq 1 40); do
  sleep 2
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
  [[ -n "$URL" ]] && break
done
[[ -n "$URL" ]] || { echo "tunnel failed to start:"; tail -6 "$TUNNEL_LOG"; exit 1; }

echo "==> pointing PUBLIC_BASE_URL at $URL"
sed -i '' "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=$URL#" "$ENV_FILE"

echo "==> starting backend"
cd "$ROOT/backend"
nohup uv run uvicorn app.main:app --port "$PORT" > "$SERVER_LOG" 2>&1 &
for _ in $(seq 1 60); do
  sleep 3
  curl -sf -m 2 "http://localhost:$PORT/health" >/dev/null 2>&1 && break
done
curl -sf -m 2 "http://localhost:$PORT/health" >/dev/null 2>&1 || {
  echo "backend did not start; last log lines:"; tail -10 "$SERVER_LOG"; exit 1; }

sleep 2
echo
echo "  Backend URL   $URL   (HTTP $(curl -s -m 20 -o /dev/null -w '%{http_code}' "$URL/health"))"
echo "  Paste it into the sheet: GridCoach -> Configure backend..."
echo "  Strava callback domain: ${URL#https://}"
echo "  Logs: $SERVER_LOG"
