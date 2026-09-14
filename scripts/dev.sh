#!/usr/bin/env bash
# Bring the local dev stack up: tunnel -> .env -> backend.
#
#   ./scripts/dev.sh          (re)start tunnel + backend, print the public URL
#   ./scripts/dev.sh status   show what's running and whether it's publicly reachable
#   ./scripts/dev.sh stop     stop tunnel + backend
#
# Tunnel choice, in order:
#   1. ngrok on a FIXED domain  — set NGROK_DOMAIN=xxx.ngrok-free.app in .env (free static domain from
#      dashboard.ngrok.com → Domains). The URL never changes, so the sidebar and the Strava callback
#      domain are configured once.
#   2. Cloudflare quick tunnel  — no account, but a new random URL on every restart (dev fallback).
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
getenv() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
NGROK_DOMAIN="$(getenv NGROK_DOMAIN || true)"

stop_all() {
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  pkill -f "ngrok http" 2>/dev/null || true
  pkill -f "uvicorn app.main" 2>/dev/null || true
}

case "${1:-}" in
  status)
    url="$(getenv PUBLIC_BASE_URL)"
    echo "backend : $(curl -s -m 3 "http://localhost:$PORT/health" || echo 'DOWN')"
    echo "tunnel  : $url -> HTTP $(curl -s -m 15 -o /dev/null -w '%{http_code}' "$url/health" || echo 'unreachable')"
    pgrep -fl "ngrok http|cloudflared tunnel" | sed 's/^/process : /' || echo "process : no tunnel running"
    echo "logs    : $SERVER_LOG"
    exit 0 ;;
  stop)
    stop_all; echo "stopped"; exit 0 ;;
esac

echo "==> stopping old processes"
stop_all; sleep 2

: > "$TUNNEL_LOG"
if [[ -n "$NGROK_DOMAIN" ]]; then
  command -v ngrok >/dev/null || { echo "ngrok not installed (brew install --cask ngrok)"; exit 1; }
  ngrok config check >/dev/null 2>&1 || { echo "ngrok has no authtoken: run  ngrok config add-authtoken <token>"; exit 1; }
  echo "==> starting ngrok on fixed domain $NGROK_DOMAIN"
  nohup ngrok http --url="https://$NGROK_DOMAIN" "$PORT" --log=stdout > "$TUNNEL_LOG" 2>&1 &
  URL="https://$NGROK_DOMAIN"
  for _ in $(seq 1 20); do sleep 1; grep -q "started tunnel" "$TUNNEL_LOG" && break; done
  grep -q "started tunnel" "$TUNNEL_LOG" || { echo "ngrok failed to start:"; tail -5 "$TUNNEL_LOG"; exit 1; }
else
  [[ -x "$CLOUDFLARED" ]] || { echo "no NGROK_DOMAIN in .env and cloudflared not found at $CLOUDFLARED"; exit 1; }
  echo "==> starting Cloudflare quick tunnel (random URL — set NGROK_DOMAIN in .env for a fixed one)"
  nohup "$CLOUDFLARED" tunnel --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
  URL=""
  for _ in $(seq 1 40); do
    sleep 2
    URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
    [[ -n "$URL" ]] && break
  done
  [[ -n "$URL" ]] || { echo "tunnel failed to start:"; tail -6 "$TUNNEL_LOG"; exit 1; }
fi

if [[ "$(getenv PUBLIC_BASE_URL)" != "$URL" ]]; then
  echo "==> pointing PUBLIC_BASE_URL at $URL"
  sed -i '' "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=$URL#" "$ENV_FILE"
fi

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
if [[ -z "$NGROK_DOMAIN" ]]; then
  echo "  Paste it into the sheet: GridCoach -> Configure backend..."
  echo "  Strava callback domain: ${URL#https://}"
fi
echo "  Logs: $SERVER_LOG"
