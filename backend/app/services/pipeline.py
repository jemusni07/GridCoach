"""Deterministic data pipelines: Strava sync, weather enrichment, snapshot recompute, forecast refresh.

Both the sidebar tools and the webhook path call into here, so behaviour is identical.
gspread is synchronous; every sheet call is pushed to a thread so the event loop stays free.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings
from . import analytics as an
from .activities import _local_start, activity_to_row, compact_activity  # noqa: F401 — re-exported
from .sheets import TAB_HEALTH, TAB_LOG, TAB_PLAN, TAB_SNAPSHOT, MissingTabError, SheetsService
from .strava_cache import StravaCache
from .state import Store
from .strava import StravaService
from .weather import WeatherService

log = logging.getLogger("gridcoach.pipeline")


class Pipeline:
    def __init__(self, settings: Settings, store: Store, sheets: SheetsService, strava: StravaService, weather: WeatherService):
        self.settings = settings
        self.store = store
        self.sheets = sheets
        self.strava = strava
        self.weather = weather
        self.cache = StravaCache(store, strava)

    # ---- weather enrichment ------------------------------------------------

    async def enrich(self, activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach hourly weather at the start point; one Open-Meteo call per ~10 km cell + date span."""
        groups: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
        rows: dict[int, dict[str, Any]] = {}
        for a in activities:
            latlng = a.get("start_latlng") or []
            start = _local_start(a)
            if len(latlng) == 2 and latlng[0] is not None and start:
                groups[(round(latlng[0], 1), round(latlng[1], 1))].append(a)
            else:
                rows[a["id"]] = activity_to_row(a, None)

        async def do_group(cell: tuple[float, float], acts: list[dict[str, Any]]) -> None:
            starts = [_local_start(a) for a in acts]
            dates = {s.date() for s in starts if s}
            try:
                hourly = await self.weather.hourly_for_dates(cell[0], cell[1], dates)
            except Exception as e:  # weather is best-effort
                log.warning("weather lookup failed for %s: %s", cell, e)
                hourly = {}
            for a, s in zip(acts, starts):
                rows[a["id"]] = activity_to_row(a, self.weather.lookup(hourly, s) if s else None)

        await asyncio.gather(*(do_group(cell, acts) for cell, acts in groups.items()))
        return [rows[a["id"]] for a in activities if a["id"] in rows]

    # ---- sync ------------------------------------------------------------------

    async def sync(self, spreadsheet_id: str, days_back: int | None = None, max_items: int = 400, progress: Any = None) -> dict[str, Any]:
        say = progress or (lambda *a, **k: None)
        days_back = days_back or self.settings.default_lookback_days
        if not await asyncio.to_thread(self.sheets.has_tab, spreadsheet_id, TAB_LOG):
            raise MissingTabError(TAB_LOG, "Strava sync")
        # the cache is the source: backfilled once, then incremental — the sheet gets the curated window
        await self.cache.ensure(spreadsheet_id, progress=say)
        activities = self.cache.activities(spreadsheet_id, days_back=days_back)[-max_items:]
        say("status", f"Enriching {len(activities)} activities with weather…")
        rows = await self.enrich(activities)
        say("status", "Writing Training Log…")
        result = await asyncio.to_thread(self.sheets.upsert_log_rows, spreadsheet_id, rows)
        # keep the log chronological even when the athlete/agent appended manual rows since the last sync
        await asyncio.to_thread(self.sheets.sort_log_by_date, spreadsheet_id)
        self.store.set_last_sync(spreadsheet_id)
        say("status", "Recomputing snapshot…")
        snapshot = await self.recompute(spreadsheet_id)
        latest = sorted(rows, key=lambda r: str(r.get("Date")), reverse=True)[:5]
        return {
            "fetched": len(activities), "updated": result["updated"], "appended": result["appended"],
            "days_back": days_back,
            "touched": [f"{TAB_LOG}!{r}" for r in result["touched"]] + ([snapshot["snapshot_range"]] if snapshot["snapshot_range"] else []),
            "latest_activities": [compact_activity(r) for r in latest],
            "snapshot": snapshot["metrics"],
            "snapshot_written": snapshot["snapshot_written"],
        }

    async def process_activity(self, spreadsheet_id: str, activity_id: int | str) -> dict[str, Any]:
        """Webhook path: one activity → cache → enrich → upsert → recompute. Sheet part is skipped without a log tab."""
        activity = await self.strava.get_activity(spreadsheet_id, activity_id)
        self.store.upsert_activities(spreadsheet_id, [activity])  # keep the full-history cache current
        if not await asyncio.to_thread(self.sheets.has_tab, spreadsheet_id, TAB_LOG):
            log.info("webhook: sheet %s has no '%s' tab; cached only", spreadsheet_id, TAB_LOG)
            return {"skipped": f"no {TAB_LOG} tab", "appended": 0, "updated": 0}
        rows = await self.enrich([activity])
        result = await asyncio.to_thread(self.sheets.upsert_log_rows, spreadsheet_id, rows)
        if result["appended"]:
            await asyncio.to_thread(self.sheets.sort_log_by_date, spreadsheet_id)
        self.store.set_last_sync(spreadsheet_id)
        snapshot = await self.recompute(spreadsheet_id)
        return {"activity": compact_activity(rows[0]) if rows else {}, "row": rows[0] if rows else {}, "snapshot": snapshot["metrics"], **result}

    # ---- deterministic recompute -----------------------------------------------

    async def recompute(self, spreadsheet_id: str) -> dict[str, Any]:
        """Metrics are always computed from whatever tabs exist; they're only *written* to tabs the athlete has."""
        def _run() -> dict[str, Any]:
            tabs = set(self.sheets.tab_titles(spreadsheet_id))
            log_rows = self.sheets.read_tab_dicts(spreadsheet_id, TAB_LOG)
            plan_rows = self.sheets.read_tab_dicts(spreadsheet_id, TAB_PLAN)
            health_rows = self.sheets.read_tab_dicts(spreadsheet_id, TAB_HEALTH)
            settings = self.sheets.get_settings(spreadsheet_id)
            today = date.today()
            metrics = compute_metrics(log_rows, plan_rows, health_rows, settings, self.store.get_tenant(spreadsheet_id), today)
            out: dict[str, Any] = {"metrics": dict(metrics), "snapshot_written": False, "snapshot_range": None,
                                   "plan_rows_reconciled": 0, "tabs_present": sorted(tabs)}
            if TAB_SNAPSHOT in tabs:
                snap = self.sheets.write_snapshot(spreadsheet_id, metrics)
                out.update(snapshot_written=True, snapshot_range=f"{snap['sheet']}!{snap['range']}")
            if TAB_PLAN in tabs and plan_rows:
                statuses = an.reconcile_plan(plan_rows, log_rows, today)
                self.sheets.write_plan_statuses(spreadsheet_id, statuses)
                out["plan_rows_reconciled"] = len(statuses)
            return out
        return await asyncio.to_thread(_run)

    # ---- weather tab ----------------------------------------------------------------

    async def refresh_weather(self, spreadsheet_id: str, days: int = 7) -> dict[str, Any]:
        settings = await asyncio.to_thread(self.sheets.get_settings, spreadsheet_id)
        lat, lng = an.to_float(settings.get("Home Latitude")), an.to_float(settings.get("Home Longitude"))
        if lat is None or lng is None:
            # fall back to the most recent activity start location
            log_rows = await asyncio.to_thread(self.sheets.read_tab_dicts, spreadsheet_id, TAB_LOG)
            for r in reversed(log_rows):
                lat, lng = an.to_float(r.get("Start Lat")), an.to_float(r.get("Start Lng"))
                if lat is not None and lng is not None:
                    break
        if lat is None or lng is None:
            raise ValueError("No location: fill Settings → Home Latitude/Longitude, or sync Strava first.")
        forecast = await self.weather.daily_forecast(lat, lng, days)
        rows = []
        for d in forecast:
            dt = an.parse_date(d["date"])
            rows.append([
                d["date"], dt.strftime("%a") if dt else "", d["min_c"], d["max_c"], d["feels_like_max_c"],
                d["precip_prob_pct"], d["precip_mm"], d["wind_max_kmh"], d["conditions"], d["sunrise"], d["sunset"],
                an.weather_tip(d["feels_like_max_c"], d["precip_prob_pct"], d["wind_max_kmh"], d["weather_code"]),
            ])
        result = await asyncio.to_thread(self.sheets.write_weather, spreadsheet_id, rows)
        return {**result, "location": {"lat": lat, "lng": lng}, "forecast": forecast}

    # ---- deep-dive on one activity -----------------------------------------------

    async def analyze_activity(self, spreadsheet_id: str, activity_id: int | str) -> dict[str, Any]:
        activity, streams = await asyncio.gather(
            self.strava.get_activity(spreadsheet_id, activity_id),
            self.strava.get_streams(spreadsheet_id, activity_id),
            return_exceptions=True,
        )
        if isinstance(activity, Exception):
            raise activity
        rows = await self.enrich([activity])
        out: dict[str, Any] = {"summary": compact_activity(rows[0]) if rows else {}, "description": activity.get("description") or ""}
        splits = activity.get("splits_metric") or []
        out["splits_km"] = [
            {"km": i + 1, "pace": an.fmt_pace((s.get("moving_time") or 0) / 60 / max((s.get("distance") or 1) / 1000, 0.01)),
             "avg_hr": round(s["average_heartrate"]) if s.get("average_heartrate") else None,
             "elev_diff_m": s.get("elevation_difference")}
            for i, s in enumerate(splits[:60])
        ]
        if not isinstance(streams, Exception):
            out["stream_analysis"] = an.analyze_streams(streams)
        else:
            out["stream_analysis"] = {"error": str(streams)}
        return out


def compute_metrics(log_rows, plan_rows, health_rows, settings, tenant, today) -> list[tuple[str, str]]:
    metrics = an.compute_snapshot(log_rows, plan_rows, health_rows, settings, today)
    athlete = (tenant or {}).get("athlete_name") or "—"
    last_sync = (tenant or {}).get("last_sync_at")
    stamp = datetime.fromtimestamp(last_sync).strftime("%Y-%m-%d %H:%M") if last_sync else "never"
    return [("Athlete (Strava)", athlete), ("Last Strava sync", stamp)] + metrics
