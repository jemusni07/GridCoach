"""GridCoach backend — FastAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import gspread.exceptions
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .agent.orchestrator import Orchestrator
from .config import Settings, get_settings
from .routers import api, strava
from .services.jobs import JobManager, PendingActions
from .services.pipeline import Pipeline
from .services.sheets import MissingTabError, SheetsError, SheetsService
from .services.state import Store
from .services.strava import StravaError, StravaService
from .services.weather import WeatherService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@dataclass
class AppContext:
    settings: Settings
    store: Store
    sheets: SheetsService
    strava: StravaService
    weather: WeatherService
    pipeline: Pipeline
    orchestrator: Orchestrator
    jobs: JobManager


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or get_settings()
    store = Store(settings.database_path)
    sheets = SheetsService(settings.google_service_account_file, settings.google_service_account_json)
    strava_svc = StravaService(settings, store)
    weather = WeatherService()
    pipeline = Pipeline(settings, store, sheets, strava_svc, weather)
    orchestrator = Orchestrator(settings, store, sheets, weather, pipeline, pending=PendingActions())
    return AppContext(settings, store, sheets, strava_svc, weather, pipeline, orchestrator, JobManager())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ctx = build_context()
    yield
    await app.state.ctx.strava.http.aclose()
    await app.state.ctx.weather.http.aclose()


app = FastAPI(title="GridCoach", version="0.1.0", lifespan=lifespan)
app.include_router(api.router)
app.include_router(strava.router)


@app.exception_handler(SheetsError)
async def _sheets_error(_: Request, exc: SheetsError):
    body: dict = {"error": str(exc)}
    if isinstance(exc, MissingTabError):
        body.update(code="missing_tab", tab=exc.tab)
    return JSONResponse(status_code=400, content=body)


@app.exception_handler(StravaError)
async def _strava_error(_: Request, exc: StravaError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(gspread.exceptions.APIError)
async def _gspread_error(request: Request, exc: gspread.exceptions.APIError):
    """Google Sheets API errors — the common one is the sheet not being shared with the service account."""
    ctx: AppContext = request.app.state.ctx
    status = getattr(getattr(exc, "response", None), "status_code", 400) or 400
    msg = str(exc)
    if status in (403, 404):
        email = ctx.sheets.service_account_email() or "the service account"
        msg = f"Google Sheets denied access ({status}). Share THIS spreadsheet with {email} as Editor, then retry."
    return JSONResponse(status_code=status if status in (403, 404, 429) else 400, content={"error": msg})


@app.get("/health")
async def health(request: Request):
    ctx: AppContext = request.app.state.ctx
    return {"ok": True, "model": ctx.settings.openai_model, "public_base_url": ctx.settings.public_base_url}
