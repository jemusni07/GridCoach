"""Strava OAuth + REST client. Tokens are stored per spreadsheet and refreshed transparently."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings
from .state import Store

API = "https://www.strava.com/api/v3"
OAUTH_AUTHORIZE = "https://www.strava.com/oauth/authorize"
OAUTH_TOKEN = "https://www.strava.com/oauth/token"
SCOPES = "read,activity:read_all"


class StravaError(Exception):
    pass


class StravaNotConnected(StravaError):
    pass


class StravaService:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.http = httpx.AsyncClient(timeout=30.0)

    # ---- OAuth -------------------------------------------------------------

    def sign(self, spreadsheet_id: str) -> str:
        return hmac.new(self.settings.gridcoach_api_key.encode(), spreadsheet_id.encode(), hashlib.sha256).hexdigest()[:32]

    def verify_sig(self, spreadsheet_id: str, sig: str) -> bool:
        return hmac.compare_digest(self.sign(spreadsheet_id), sig or "")

    def connect_url(self, spreadsheet_id: str) -> str:
        """URL the sidebar opens; the backend then redirects to Strava with a signed state."""
        return f"{self.settings.public_base_url.rstrip('/')}/auth/strava?{urlencode({'spreadsheet_id': spreadsheet_id, 'sig': self.sign(spreadsheet_id)})}"

    def authorize_url(self, spreadsheet_id: str) -> str:
        if not self.settings.strava_client_id:
            raise StravaError("STRAVA_CLIENT_ID is not configured")
        params = {
            "client_id": self.settings.strava_client_id,
            "redirect_uri": self.settings.strava_redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": SCOPES,
            "state": f"{spreadsheet_id}.{self.sign(spreadsheet_id)}",
        }
        return f"{OAUTH_AUTHORIZE}?{urlencode(params)}"

    def parse_state(self, state: str) -> str:
        sid, _, sig = (state or "").rpartition(".")
        if not sid or not self.verify_sig(sid, sig):
            raise StravaError("Invalid OAuth state")
        return sid

    async def exchange_code(self, spreadsheet_id: str, code: str) -> dict[str, Any]:
        r = await self.http.post(OAUTH_TOKEN, data={
            "client_id": self.settings.strava_client_id,
            "client_secret": self.settings.strava_client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })
        if r.status_code != 200:
            raise StravaError(f"Token exchange failed: {r.status_code} {r.text[:200]}")
        tok = r.json()
        athlete = tok.get("athlete") or {}
        name = " ".join(p for p in (athlete.get("firstname"), athlete.get("lastname")) if p) or "athlete"
        self.store.save_strava_tokens(
            spreadsheet_id,
            athlete_id=int(athlete.get("id", 0)),
            athlete_name=name,
            access_token=tok["access_token"],
            refresh_token=tok["refresh_token"],
            expires_at=int(tok["expires_at"]),
        )
        return {"athlete_id": athlete.get("id"), "athlete_name": name}

    async def access_token(self, spreadsheet_id: str) -> str:
        t = self.store.get_tenant(spreadsheet_id)
        if not t or not t.get("refresh_token"):
            raise StravaNotConnected("Strava is not connected for this spreadsheet. Use 'Connect Strava' in the sidebar.")
        if (t.get("expires_at") or 0) - time.time() > 300:
            return t["access_token"]
        r = await self.http.post(OAUTH_TOKEN, data={
            "client_id": self.settings.strava_client_id,
            "client_secret": self.settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": t["refresh_token"],
        })
        if r.status_code != 200:
            raise StravaError(f"Token refresh failed: {r.status_code} {r.text[:200]}")
        tok = r.json()
        self.store.update_access_token(spreadsheet_id, tok["access_token"], tok["refresh_token"], int(tok["expires_at"]))
        return tok["access_token"]

    # ---- REST ----------------------------------------------------------------

    async def _get(self, spreadsheet_id: str, path: str, params: dict[str, Any] | None = None) -> Any:
        token = await self.access_token(spreadsheet_id)
        r = await self.http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise StravaNotConnected("Strava rejected the token; reconnect Strava.")
        if r.status_code == 429:
            raise StravaError("Strava rate limit hit (100 req / 15 min). Try again shortly.")
        if r.status_code >= 400:
            raise StravaError(f"Strava {path} failed: {r.status_code} {r.text[:200]}")
        return r.json()

    async def list_activities(self, spreadsheet_id: str, after_ts: int | None = None, before_ts: int | None = None, max_items: int = 400,
                              on_page: Any = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < max_items:
            params: dict[str, Any] = {"per_page": 200, "page": page}
            if after_ts:
                params["after"] = after_ts
            if before_ts:
                params["before"] = before_ts
            batch = await self._get(spreadsheet_id, "/athlete/activities", params)
            if not batch:
                break
            out.extend(batch)
            if on_page:
                on_page(len(out))
            if len(batch) < 200:
                break
            page += 1
        return out[:max_items]

    async def athlete(self, spreadsheet_id: str) -> dict[str, Any]:
        return await self._get(spreadsheet_id, "/athlete")

    async def athlete_stats(self, spreadsheet_id: str) -> dict[str, Any]:
        t = self.store.get_tenant(spreadsheet_id) or {}
        if not t.get("athlete_id"):
            raise StravaNotConnected("Strava is not connected for this spreadsheet.")
        return await self._get(spreadsheet_id, f"/athletes/{t['athlete_id']}/stats")

    async def athlete_zones(self, spreadsheet_id: str) -> dict[str, Any]:
        return await self._get(spreadsheet_id, "/athlete/zones")

    async def get_activity(self, spreadsheet_id: str, activity_id: int | str) -> dict[str, Any]:
        return await self._get(spreadsheet_id, f"/activities/{activity_id}", {"include_all_efforts": "false"})

    async def get_streams(self, spreadsheet_id: str, activity_id: int | str) -> dict[str, Any]:
        return await self._get(spreadsheet_id, f"/activities/{activity_id}/streams", {
            "keys": "time,distance,heartrate,cadence,velocity_smooth,altitude", "key_by_type": "true",
        })

    async def get_laps(self, spreadsheet_id: str, activity_id: int | str) -> list[dict[str, Any]]:
        return await self._get(spreadsheet_id, f"/activities/{activity_id}/laps")
