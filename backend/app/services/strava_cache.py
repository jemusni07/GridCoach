"""Local cache of the athlete's FULL Strava history — the rigorous query source.

The Training Log is the athlete's curated view (only what they chose to sync). Questions about history,
totals or other years are answered from here without touching the sheet or re-hitting Strava's rate limits.
First use backfills everything (≈1 API call per 200 activities); later calls are incremental (with a 3-day
overlap to catch edits); the webhook keeps it current.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .activities import STRAVA_EXTRA, activity_to_row
from .sheets import LOG_HEADERS
from .state import Store
from .strava import StravaService

STRAVA_COLUMNS = [h for h in LOG_HEADERS if h not in ("Notes", "Strava Link", "Temp (°C)", "Feels Like (°C)", "Humidity (%)",
                                                        "Wind (km/h)", "Conditions", "Heat Adj Pace (min/km)")] + STRAVA_EXTRA


class StravaCache:
    def __init__(self, store: Store, strava: StravaService):
        self.store = store
        self.strava = strava

    async def ensure(self, spreadsheet_id: str, progress: Any = None, max_age_seconds: int = 300) -> dict[str, Any]:
        """Backfill on first use; otherwise pull anything new/edited since the last sync. Cheap when fresh."""
        say = progress or (lambda *a, **k: None)
        info = self.store.cache_info(spreadsheet_id)
        if not info["backfilled"]:
            say("status", "Backfilling your full Strava history (first time only)…")
            acts = await self.strava.list_activities(spreadsheet_id, after_ts=None, max_items=20000,
                                                     on_page=lambda n: say("status", f"Backfilling Strava history… {n} activities"))
            n = self.store.upsert_activities(spreadsheet_id, acts)
            self.store.mark_cache(spreadsheet_id, backfilled=True)
            return {**self.store.cache_info(spreadsheet_id), "fetched": n, "mode": "backfill"}
        if info["synced_at"] and time.time() - info["synced_at"] < max_age_seconds:
            return {**info, "fetched": 0, "mode": "fresh"}
        since = int((info["synced_at"] or 0) - 3 * 86400)
        acts = await self.strava.list_activities(spreadsheet_id, after_ts=since, max_items=5000)
        n = self.store.upsert_activities(spreadsheet_id, acts)
        self.store.mark_cache(spreadsheet_id)
        return {**self.store.cache_info(spreadsheet_id), "fetched": n, "mode": "incremental"}

    def activities(self, spreadsheet_id: str, days_back: int | None = None) -> list[dict[str, Any]]:
        date_from = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ") if days_back else None
        return self.store.list_activities(spreadsheet_id, date_from=date_from)

    def rows(self, spreadsheet_id: str) -> list[dict[str, Any]]:
        """Log-shaped dicts (same headers as the Training Log + a few extras) so query/aggregate code is shared."""
        return [activity_to_row(a, None, extras=True) for a in self.store.list_activities(spreadsheet_id)]

    def table(self, spreadsheet_id: str) -> list[list[Any]]:
        """Header + rows, for the analyze_data sandbox (`strava` DataFrame)."""
        rows = self.rows(spreadsheet_id)
        return [STRAVA_COLUMNS] + [[r.get(h, "") for h in STRAVA_COLUMNS] for r in rows]


def simplify_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Strava /athletes/{id}/stats → compact km/hours per window per sport."""
    out: dict[str, Any] = {}
    for key, val in stats.items():
        if not isinstance(val, dict) or "distance" not in val:
            continue
        window, _, sport = key.partition("_")  # recent_run_totals / ytd_ride_totals / all_swim_totals
        sport = sport.replace("_totals", "")
        label = {"recent": "last_4_weeks", "ytd": "year_to_date", "all": "all_time"}.get(window, window)
        out.setdefault(label, {})[sport] = {
            "count": val.get("count"), "distance_km": round((val.get("distance") or 0) / 1000, 1),
            "moving_time_h": round((val.get("moving_time") or 0) / 3600, 1), "elevation_m": round(val.get("elevation_gain") or 0),
            **({"achievements": val["achievement_count"]} if val.get("achievement_count") is not None else {}),
        }
    for k in ("biggest_ride_distance", "biggest_climb_elevation_gain"):
        if stats.get(k) is not None:
            out[k] = round(stats[k] / (1000 if "distance" in k else 1), 1)
    return out


def simplify_gear(athlete: dict[str, Any]) -> dict[str, Any]:
    def items(lst: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [{"name": g.get("name"), "distance_km": round((g.get("distance") or 0) / 1000, 1), "primary": bool(g.get("primary")),
                 "retired": bool(g.get("retired")), "id": g.get("id")} for g in (lst or [])]
    return {"shoes": items(athlete.get("shoes")), "bikes": items(athlete.get("bikes"))}
