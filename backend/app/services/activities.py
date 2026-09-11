"""Strava activity → Training Log row mapping. Shared by the sheet sync pipeline and the Strava cache/query layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import analytics as an
from .sheets import LOG_HEADERS

# extra columns available on cached Strava rows (not written to the sheet)
STRAVA_EXTRA = ["Kudos", "Avg Watts", "Gear ID", "Start Local"]


def _local_start(activity: dict[str, Any]) -> datetime | None:
    s = activity.get("start_date_local")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))  # Strava's local time carries a fake 'Z'
    except ValueError:
        return None


def activity_to_row(a: dict[str, Any], wx: dict[str, Any] | None, extras: bool = False) -> dict[str, Any]:
    """Map a Strava activity (+ optional hourly weather) onto Training Log columns."""
    start = _local_start(a)
    dist_km = (a.get("distance") or 0) / 1000
    moving_min = (a.get("moving_time") or 0) / 60
    elapsed_min = (a.get("elapsed_time") or 0) / 60
    sport = a.get("sport_type") or a.get("type") or ""
    pace = moving_min / dist_km if dist_km > 0 and sport in an.FOOT_SPORTS else None
    speed_kmh = (a.get("average_speed") or 0) * 3.6
    latlng = a.get("start_latlng") or [None, None]
    wx = wx or {}
    adj_pace = an.heat_adjusted_pace(pace, wx.get("feels_like_c")) if pace else None

    def r(x: Any, nd: int = 1) -> Any:
        return "" if x is None else round(float(x), nd)

    row = {
        "Date": start.strftime("%Y-%m-%d") if start else "",
        "Activity ID": str(a.get("id", "")),
        "Name": a.get("name", ""),
        "Sport": sport,
        "Indoor": "Y" if a.get("trainer") else "",  # Strava's trainer flag: treadmill / turbo / gym
        "Distance (km)": r(dist_km, 2),
        "Moving Time (min)": r(moving_min, 1),
        "Elapsed Time (min)": r(elapsed_min, 1),
        # leading apostrophe keeps "5:32" as text instead of Sheets parsing it as a time
        "Avg Pace (min/km)": f"'{an.fmt_pace(pace)}" if pace else "",
        "Avg Speed (km/h)": r(speed_kmh, 1),
        "Avg HR": r(a.get("average_heartrate"), 0),
        "Max HR": r(a.get("max_heartrate"), 0),
        "Elevation Gain (m)": r(a.get("total_elevation_gain"), 0),
        "Avg Cadence": r((a.get("average_cadence") or 0) * 2 if a.get("average_cadence") and sport in an.FOOT_SPORTS else a.get("average_cadence"), 0),
        "Suffer Score": r(a.get("suffer_score"), 0),
        "Start Lat": r(latlng[0], 5) if latlng and latlng[0] is not None else "",
        "Start Lng": r(latlng[1], 5) if latlng and len(latlng) > 1 and latlng[1] is not None else "",
        "Temp (°C)": r(wx.get("temp_c")),
        "Feels Like (°C)": r(wx.get("feels_like_c")),
        "Humidity (%)": r(wx.get("humidity_pct"), 0),
        "Wind (km/h)": r(wx.get("wind_kmh")),
        "Conditions": wx.get("conditions", ""),
        "Heat Adj Pace (min/km)": f"'{an.fmt_pace(adj_pace)}" if adj_pace else "",
        "Strava Link": f"https://www.strava.com/activities/{a.get('id')}",
    }
    out = {h: row.get(h, "") for h in LOG_HEADERS if h != "Notes"}
    if extras:
        out.update({"Kudos": a.get("kudos_count", ""), "Avg Watts": r(a.get("average_watts"), 0), "Gear ID": a.get("gear_id") or "",
                    "Start Local": start.strftime("%Y-%m-%d %H:%M") if start else ""})
    return out


def compact_activity(row: dict[str, Any]) -> dict[str, Any]:
    keep = ["Date", "Activity ID", "Name", "Sport", "Indoor", "Distance (km)", "Moving Time (min)", "Avg Pace (min/km)",
            "Avg HR", "Elevation Gain (m)", "Feels Like (°C)", "Conditions", "Heat Adj Pace (min/km)"]
    return {k: (str(row.get(k, "")).lstrip("'")) for k in keep if row.get(k, "") != ""}
