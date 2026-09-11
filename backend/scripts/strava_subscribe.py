"""Manage the Strava webhook subscription for this backend.

    uv run python scripts/strava_subscribe.py create   # needs PUBLIC_BASE_URL reachable by Strava (ngrok)
    uv run python scripts/strava_subscribe.py list
    uv run python scripts/strava_subscribe.py delete <id>
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402

URL = "https://www.strava.com/api/v3/push_subscriptions"


def main() -> None:
    s = get_settings()
    creds = {"client_id": s.strava_client_id, "client_secret": s.strava_client_secret}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "create":
        r = httpx.post(URL, data={**creds, "callback_url": s.strava_webhook_url, "verify_token": s.strava_verify_token})
    elif cmd == "delete" and len(sys.argv) > 2:
        r = httpx.delete(f"{URL}/{sys.argv[2]}", params=creds)
    else:
        r = httpx.get(URL, params=creds)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
