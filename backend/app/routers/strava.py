"""Strava OAuth redirect/callback and the webhook receiver."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..services.strava import StravaError

log = logging.getLogger("gridcoach.strava")
router = APIRouter(tags=["strava"])


def ctx(request: Request):
    return request.app.state.ctx


# ---- OAuth -------------------------------------------------------------------

@router.get("/auth/strava")
async def auth_start(spreadsheet_id: str, sig: str, request: Request):
    c = ctx(request)
    if not c.strava.verify_sig(spreadsheet_id, sig):
        raise HTTPException(status_code=403, detail="bad signature")
    return RedirectResponse(c.strava.authorize_url(spreadsheet_id))


@router.get("/auth/strava/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, code: str | None = None, state: str = "", error: str | None = None, scope: str = ""):
    c = ctx(request)
    if error or not code:
        return _page("Strava connection cancelled", f"Strava said: {error or 'no code'}. Close this tab and try again.")
    try:
        sid = c.strava.parse_state(state)
        info = await c.strava.exchange_code(sid, code)
    except StravaError as e:
        return _page("Strava connection failed", str(e))
    if "activity:read" not in scope:
        return _page("Missing permission", "Please re-connect and allow 'View data about your activities'.")
    return _page("Strava connected ✓", f"Connected as <b>{info['athlete_name']}</b>. You can close this tab and go back to your sheet — click <b>Sync Strava</b> in the sidebar.")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<body style="font-family:system-ui;max-width:520px;margin:12vh auto;padding:0 24px;color:#1f2937">
<h2>{title}</h2><p style="font-size:16px;line-height:1.5">{body}</p></body>"""


# ---- Webhook -------------------------------------------------------------------

@router.get("/webhook/strava")
async def webhook_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == ctx(request).settings.strava_verify_token:
        return JSONResponse({"hub.challenge": q.get("hub.challenge", "")})
    raise HTTPException(status_code=403, detail="verify token mismatch")


@router.post("/webhook/strava")
async def webhook_event(event: dict[str, Any], background: BackgroundTasks, request: Request):
    """Strava requires a 200 within 2 s; do the real work in the background."""
    c = ctx(request)
    if event.get("object_type") == "activity" and event.get("aspect_type") in ("create", "update"):
        tenant = c.store.find_by_athlete(int(event.get("owner_id", 0)))
        if tenant:
            background.add_task(_process, c, tenant["spreadsheet_id"], event["object_id"], event["aspect_type"])
        else:
            log.info("webhook for unknown athlete %s ignored", event.get("owner_id"))
    return {"ok": True}


async def _process(c, spreadsheet_id: str, activity_id: int, aspect: str) -> None:
    try:
        processed = await c.pipeline.process_activity(spreadsheet_id, activity_id)
        log.info("webhook %s activity %s → sheet %s (%s)", aspect, activity_id, spreadsheet_id, processed.get("activity", {}).get("Name"))
        if aspect == "create" and processed.get("appended"):
            result = await c.orchestrator.background_analysis(spreadsheet_id, processed)
            log.info("coach note: %s", result.reply[:200])
    except Exception:
        log.exception("webhook processing failed for activity %s", activity_id)
