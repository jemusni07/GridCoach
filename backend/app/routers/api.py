"""Endpoints the Apps Script sidebar calls (all require X-API-Key)."""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["sidebar"])


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    expected = request.app.state.ctx.settings.gridcoach_api_key
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def ctx(request: Request):
    return request.app.state.ctx


class ChatIn(BaseModel):
    spreadsheet_id: str
    message: str = Field(min_length=1, max_length=20000)
    context: dict[str, Any] | None = None
    reset: bool = False
    background: bool = False  # true → returns {job_id}; poll GET /api/jobs/{id}


class ActionIn(BaseModel):
    spreadsheet_id: str
    action_id: str
    approve: bool


class SheetIn(BaseModel):
    spreadsheet_id: str


class SetupIn(SheetIn):
    tabs: list[str] = Field(default_factory=list)


class SyncIn(SheetIn):
    days_back: int | None = Field(default=None, ge=1, le=730)


class WeatherIn(SheetIn):
    days: int = Field(default=7, ge=1, le=14)


@router.post("/chat", dependencies=[Depends(require_api_key)])
async def chat(body: ChatIn, request: Request):
    c = ctx(request)
    if body.reset:
        c.store.set_last_response(body.spreadsheet_id, None)
        c.store.clear_messages(body.spreadsheet_id)
    if body.background:
        job = c.jobs.create(body.spreadsheet_id, "chat")
        c.store.add_message(body.spreadsheet_id, "user", body.message)
        c.store.set_last_job(body.spreadsheet_id, job.id)
        c.jobs.start(job, _chat_job(c, body, c.jobs.emitter(job.id)))
        return {"job_id": job.id}
    result = await c.orchestrator.chat(body.spreadsheet_id, body.message, body.context, reset=body.reset)
    return result.to_dict()


def _record_result(c, spreadsheet_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the coach's turn so the sidebar can replay it after being closed."""
    c.store.add_message(spreadsheet_id, "coach", result["reply"],
                        {k: result.get(k) for k in ("tool_log", "touched_ranges", "pending_actions")})
    return result


async def _chat_job(c, body: ChatIn, emit):
    try:
        result = await c.orchestrator.chat(body.spreadsheet_id, body.message, body.context, reset=False, on_event=emit)
    except Exception as e:
        c.store.add_message(body.spreadsheet_id, "err", f"{type(e).__name__}: {e}")
        raise
    return _record_result(c, body.spreadsheet_id, result.to_dict())


@router.get("/history", dependencies=[Depends(require_api_key)])
async def history(spreadsheet_id: str, request: Request, limit: int = 60):
    """Transcript replay + whether a job is still running, so a reopened sidebar picks up where it left off."""
    c = ctx(request)
    msgs = c.store.list_messages(spreadsheet_id, limit)
    open_ids = set(c.orchestrator.pending.items)
    for m in msgs:
        for pa in (m.get("meta") or {}).get("pending_actions") or []:
            pa["open"] = pa.get("action_id") in open_ids
    active = None
    t = c.store.get_tenant(spreadsheet_id) or {}
    if t.get("last_job_id"):
        snap = c.jobs.snapshot(t["last_job_id"])
        if snap and snap["status"] == "running":
            active = {"job_id": snap["id"], "kind": snap["kind"]}
    return {"messages": msgs, "active_job": active}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def job_status(job_id: str, request: Request, after: int = 0):
    snap = ctx(request).jobs.snapshot(job_id, after)
    if not snap:
        raise HTTPException(status_code=404, detail="unknown job")
    return snap


@router.post("/actions/resolve", dependencies=[Depends(require_api_key)])
async def resolve_action(body: ActionIn, request: Request):
    """Athlete pressed Confirm/Cancel on a parked destructive action. Runs as a job (the coach wraps up afterwards)."""
    c = ctx(request)
    job = c.jobs.create(body.spreadsheet_id, "action")
    emit = c.jobs.emitter(job.id)
    item = c.orchestrator.pending.items.get(body.action_id)
    c.store.add_message(body.spreadsheet_id, "sys", f"{'Confirmed' if body.approve else 'Cancelled'}: {item['description'] if item else body.action_id}")
    c.store.set_last_job(body.spreadsheet_id, job.id)

    async def _run():
        res = await c.orchestrator.resolve_action(body.spreadsheet_id, body.action_id, body.approve, on_event=emit)
        return _record_result(c, body.spreadsheet_id, res.to_dict())

    c.jobs.start(job, _run())
    return {"job_id": job.id}


@router.post("/chat/reset", dependencies=[Depends(require_api_key)])
async def chat_reset(body: SheetIn, request: Request):
    c = ctx(request)
    c.store.set_last_response(body.spreadsheet_id, None)
    c.store.clear_messages(body.spreadsheet_id)
    return {"ok": True}


@router.post("/setup", dependencies=[Depends(require_api_key)])
async def setup(body: SetupIn, request: Request):
    """Create only the tabs the athlete ticked. Tabs are opt-in; an empty list just reports state."""
    import asyncio
    c = ctx(request)
    result = await asyncio.to_thread(c.sheets.ensure_layout, body.spreadsheet_id, body.tabs)
    return {**result, "service_account_email": c.sheets.service_account_email()}


@router.get("/tabs", dependencies=[Depends(require_api_key)])
async def tab_catalog(request: Request):
    return {"tabs": ctx(request).sheets.tab_catalog()}


@router.get("/status", dependencies=[Depends(require_api_key)])
async def status(spreadsheet_id: str, request: Request):
    c = ctx(request)
    t = c.store.get_tenant(spreadsheet_id) or {}
    return {
        "strava_connected": bool(t.get("athlete_id")),
        "athlete_name": t.get("athlete_name"),
        "last_sync_at": t.get("last_sync_at"),
        "has_conversation": bool(t.get("last_response_id")),
        "model": c.settings.openai_model,
        "service_account_email": c.sheets.service_account_email(),
    }


@router.post("/strava/connect-url", dependencies=[Depends(require_api_key)])
async def strava_connect_url(body: SheetIn, request: Request):
    return {"url": ctx(request).strava.connect_url(body.spreadsheet_id)}


@router.post("/strava/disconnect", dependencies=[Depends(require_api_key)])
async def strava_disconnect(body: SheetIn, request: Request):
    ctx(request).store.disconnect_strava(body.spreadsheet_id)
    return {"ok": True}


@router.post("/strava/sync", dependencies=[Depends(require_api_key)])
async def strava_sync(body: SyncIn, request: Request, background: bool = False):
    c = ctx(request)
    if not background:
        return await c.pipeline.sync(body.spreadsheet_id, body.days_back)
    # the missing-tab check is done inline so the sidebar can offer the one-click fix before a job is created
    from ..services.sheets import TAB_LOG, MissingTabError
    import asyncio as _aio
    if not await _aio.to_thread(c.sheets.has_tab, body.spreadsheet_id, TAB_LOG):
        raise MissingTabError(TAB_LOG, "Strava sync")
    job = c.jobs.create(body.spreadsheet_id, "sync")
    c.store.set_last_job(body.spreadsheet_id, job.id)

    async def _run():
        res = await c.pipeline.sync(body.spreadsheet_id, body.days_back, progress=c.jobs.emitter(job.id))
        c.store.add_message(body.spreadsheet_id, "sys", f"Synced Strava: {res['fetched']} activities · {res['appended']} new · {res['updated']} updated")
        return res

    c.jobs.start(job, _run())
    return {"job_id": job.id}


@router.post("/weather/refresh", dependencies=[Depends(require_api_key)])
async def weather_refresh(body: WeatherIn, request: Request):
    try:
        res = await ctx(request).pipeline.refresh_weather(body.spreadsheet_id, body.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {k: v for k, v in res.items() if k != "forecast"}


@router.post("/recompute", dependencies=[Depends(require_api_key)])
async def recompute(body: SheetIn, request: Request):
    return await ctx(request).pipeline.recompute(body.spreadsheet_id)
