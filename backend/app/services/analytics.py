"""Deterministic training analytics. Pure functions over sheet rows — no I/O, no LLM.

Everything the "Health Snapshot" tab shows, the plan-vs-log reconciliation, and the
heat/weather adjustments live here so they are reproducible and testable.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

FOOT_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}
REST_WORDS = {"rest", "off", "rest day", ""}


# ---------- parsing helpers ----------

def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    # "5:32" style pace -> decimal minutes
    if ":" in s and s.replace(":", "").isdigit():
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 2:
            return parts[0] + parts[1] / 60
        if len(parts) == 3:
            return parts[0] * 60 + parts[1] + parts[2] / 60
    try:
        return float(s)
    except ValueError:
        return None


def rows_to_dicts(values: list[list[Any]], with_row: bool = False) -> list[dict[str, Any]]:
    """First row is the header. Short rows are padded; blank header cells are skipped. `with_row` adds `_row` (sheet row no.)."""
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    out: list[dict[str, Any]] = []
    for n, row in enumerate(values[1:], start=2):
        if not any(str(c).strip() for c in row):
            continue
        padded = list(row) + [""] * (len(header) - len(row))
        rec = {h: padded[i] for i, h in enumerate(header) if h}
        if with_row:
            rec["_row"] = n
        out.append(rec)
    return out


def fmt_pace(minutes_per_km: float | None) -> str:
    if not minutes_per_km or minutes_per_km <= 0:
        return ""
    total = round(minutes_per_km * 60)
    return f"{total // 60}:{total % 60:02d}"


# ---------- weather adjustments ----------

def heat_adjustment_pct(apparent_temp_c: float | None) -> float:
    """Rough heat tax: ~1% slower per °C of apparent temperature above 15 °C, capped at 25%.

    Coarse heuristic (in the spirit of the commonly cited ~1-2% per °F/°C above ~60°F);
    good enough to flag "that 5:40 in 30 °C was really a 5:00 day", not a physiological model.
    """
    if apparent_temp_c is None:
        return 0.0
    return max(0.0, min(0.25, (apparent_temp_c - 15.0) * 0.01))


def heat_adjusted_pace(pace_min_per_km: float | None, apparent_temp_c: float | None) -> float | None:
    if not pace_min_per_km:
        return None
    return pace_min_per_km / (1.0 + heat_adjustment_pct(apparent_temp_c))


def weather_tip(feels_max_c: float | None, precip_prob: float | None, wind_max_kmh: float | None, code: int | None) -> str:
    tips: list[str] = []
    if feels_max_c is not None and feels_max_c >= 28:
        tips.append("Hot — train early/late, add fluids, expect slower paces")
    elif feels_max_c is not None and feels_max_c <= 2:
        tips.append("Cold — longer warm-up, layers")
    if precip_prob is not None and precip_prob >= 60:
        tips.append("Likely rain — waterproof layer, watch footing")
    if wind_max_kmh is not None and wind_max_kmh >= 35:
        tips.append("Windy — start into the wind, finish with it")
    if code is not None and code >= 95:
        tips.append("Thunderstorms possible — have an indoor backup")
    return "; ".join(tips) if tips else "Good conditions"


# ---------- activity stream analysis ----------

def analyze_streams(streams: dict[str, Any]) -> dict[str, Any]:
    """Aerobic decoupling / cardiac drift from Strava streams (key_by_type=true format)."""
    def data(key: str) -> list[float]:
        s = streams.get(key) or {}
        return [float(x) if x is not None else 0.0 for x in (s.get("data") or [])]

    t, d, hr, cad = data("time"), data("distance"), data("heartrate"), data("cadence")
    n = min(len(t), len(d))
    out: dict[str, Any] = {"samples": n}
    if n < 20:
        out["note"] = "not enough samples"
        return out
    total = d[n - 1]
    half_idx = next((i for i in range(n) if d[i] >= total / 2), n // 2)

    def segment(a: int, b: int) -> dict[str, float | None]:
        dist = d[b - 1] - d[a]
        dur = t[b - 1] - t[a]
        seg_hr = [h for h in hr[a:b] if h > 0]
        return {
            "distance_km": round(dist / 1000, 2),
            "duration_min": round(dur / 60, 1),
            "pace_min_per_km": round((dur / 60) / (dist / 1000), 2) if dist > 0 else None,
            "avg_hr": round(sum(seg_hr) / len(seg_hr), 1) if seg_hr else None,
            "speed_mps": dist / dur if dur > 0 else None,
        }

    first, second = segment(0, half_idx), segment(half_idx, n)
    out["first_half"] = {k: v for k, v in first.items() if k != "speed_mps"}
    out["second_half"] = {k: v for k, v in second.items() if k != "speed_mps"}
    if first["avg_hr"] and second["avg_hr"] and first["speed_mps"] and second["speed_mps"]:
        eff1 = first["speed_mps"] / first["avg_hr"]
        eff2 = second["speed_mps"] / second["avg_hr"]
        out["aerobic_decoupling_pct"] = round((eff1 - eff2) / eff1 * 100, 1)
        out["cardiac_drift_pct"] = round((second["avg_hr"] / first["avg_hr"] - 1) * 100, 1)
        dec = out["aerobic_decoupling_pct"]
        out["decoupling_read"] = (
            "well coupled (<5%) — aerobic base is holding" if dec < 5
            else "moderate drift (5-10%) — heat, fuelling or pacing" if dec < 10
            else "high drift (>10%) — likely too hard, hot, or under-fuelled"
        )
    cad_vals = [c for c in cad if c > 0]
    if cad_vals:
        out["avg_cadence_spm"] = round(2 * sum(cad_vals) / len(cad_vals))  # Strava reports one-leg cadence
    return out


# ---------- snapshot + reconciliation ----------

def _window(rows: Iterable[dict[str, Any]], today: date, days: int) -> list[dict[str, Any]]:
    start = today - timedelta(days=days - 1)
    out = []
    for r in rows:
        d = parse_date(r.get("Date"))
        if d and start <= d <= today:
            out.append(r)
    return out


def _sum(rows: list[dict[str, Any]], col: str) -> float:
    return sum(to_float(r.get(col)) or 0.0 for r in rows)


def compute_snapshot(
    log_rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]],
    health_rows: list[dict[str, Any]],
    settings: dict[str, str],
    today: date | None = None,
) -> list[tuple[str, str]]:
    """Return ordered (metric, value) pairs for the Health Snapshot tab."""
    today = today or date.today()
    log = [r for r in log_rows if parse_date(r.get("Date"))]
    l7, l28 = _window(log, today, 7), _window(log, today, 28)

    dist7, dist28 = _sum(l7, "Distance (km)"), _sum(l28, "Distance (km)")
    time7, time28 = _sum(l7, "Moving Time (min)"), _sum(l28, "Moving Time (min)")
    weekly_avg = dist28 / 4
    acwr_dist = dist7 / weekly_avg if weekly_avg > 0 else None
    time_weekly_avg = time28 / 4
    acwr_time = time7 / time_weekly_avg if time_weekly_avg > 0 else None

    def load_status(acwr: float | None) -> str:
        if acwr is None:
            return "not enough history"
        if acwr > 1.5:
            return "SPIKE — acute load well above chronic; injury risk elevated"
        if acwr > 1.3:
            return "high — pushing; keep an eye on recovery"
        if acwr >= 0.8:
            return "sweet spot (0.8–1.3)"
        return "low — undertraining or recovering"

    longest = max(l28, key=lambda r: to_float(r.get("Distance (km)")) or 0.0, default=None)
    hr_vals = [to_float(r.get("Avg HR")) for r in l7]
    hr_vals = [h for h in hr_vals if h]
    mix: dict[str, float] = defaultdict(float)
    for r in l7:
        mix[str(r.get("Sport") or "Other")] += to_float(r.get("Moving Time (min)")) or 0.0
    mix_str = " · ".join(f"{k} {round(v)} min" for k, v in sorted(mix.items(), key=lambda kv: -kv[1])) or "—"

    last_dates = [parse_date(r.get("Date")) for r in log]
    last_dates = [d for d in last_dates if d]
    days_since = (today - max(last_dates)).days if last_dates else None

    # plan adherence over the last 7 days
    statuses = reconcile_plan(plan_rows, log, today)
    planned7 = done7 = 0
    upcoming7 = 0
    for row, status in zip(plan_rows, statuses):
        d = parse_date(row.get("Date"))
        if not d or status == "Rest":
            continue
        if today - timedelta(days=6) <= d <= today:
            planned7 += 1
            done7 += status.startswith("Done")
        if today < d <= today + timedelta(days=7):
            upcoming7 += 1
    adherence = f"{done7}/{planned7} sessions ({round(100 * done7 / planned7)}%)" if planned7 else "no planned sessions in window"

    h7 = _window([r for r in health_rows if parse_date(r.get("Date"))], today, 7)
    sleep_vals = [to_float(r.get("Sleep (hrs)")) for r in h7]
    sleep_vals = [s for s in sleep_vals if s]

    race_name = settings.get("Race Name", "").strip()
    race_date = parse_date(settings.get("Race Date"))
    race_dist = settings.get("Race Distance", "").strip()
    if race_date:
        delta = (race_date - today).days
        race_str = f"{race_name or race_dist or 'Race'} — {race_date.isoformat()} ({delta} days / {delta / 7:.1f} weeks)"
    else:
        race_str = "not set (fill Settings → Race Date)"

    def n(x: float | None, nd: int = 1) -> str:
        return "—" if x is None else f"{x:.{nd}f}"

    return [
        ("Activities (7d)", str(len(l7))),
        ("Distance 7d (km)", n(dist7)),
        ("Time 7d (min)", n(time7, 0)),
        ("Distance 28d (km)", n(dist28)),
        ("Time 28d (min)", n(time28, 0)),
        ("Avg weekly distance, 4 wks (km)", n(weekly_avg)),
        ("ACWR (distance)", n(acwr_dist, 2)),
        ("ACWR (time)", n(acwr_time, 2)),
        ("Load status", load_status(acwr_dist if acwr_dist is not None else acwr_time)),
        ("Longest session 28d", f"{to_float(longest.get('Distance (km)')) or 0:.1f} km {longest.get('Sport', '')} on {longest.get('Date')}" if longest else "—"),
        ("Avg HR 7d", n(sum(hr_vals) / len(hr_vals), 0) if hr_vals else "—"),
        ("Sport mix 7d", mix_str),
        ("Days since last activity", "—" if days_since is None else str(days_since)),
        ("Plan adherence 7d", adherence),
        ("Planned sessions next 7d", str(upcoming7)),
        ("Sleep avg 7d (hrs)", n(sum(sleep_vals) / len(sleep_vals)) if sleep_vals else "—"),
        ("Race", race_str),
    ]


def reconcile_plan(plan_rows: list[dict[str, Any]], log_rows: list[dict[str, Any]], today: date | None = None) -> list[str]:
    """One status per plan row: Done (…), Missed, Today, Upcoming, Rest."""
    today = today or date.today()
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in log_rows:
        d = parse_date(r.get("Date"))
        if d:
            by_date[d].append(r)

    out: list[str] = []
    for row in plan_rows:
        d = parse_date(row.get("Date"))
        wtype = str(row.get("Workout Type") or "").strip().lower()
        if wtype in REST_WORDS:
            out.append("Rest")
            continue
        if not d:
            out.append("")
            continue
        done = by_date.get(d)
        if done:
            parts = [f"{a.get('Sport', '')} {to_float(a.get('Distance (km)')) or 0:.1f} km".strip() for a in done]
            out.append(f"Done ({', '.join(parts)})")
        elif d < today:
            out.append("Missed")
        elif d == today:
            out.append("Today")
        else:
            out.append("Upcoming")
    return out


# ---------- deterministic log querying (so the model never eyeballs a 2-D array) ----------

LOG_COMPACT_FIELDS = ["_row", "Date", "Activity ID", "Name", "Sport", "Indoor", "Distance (km)", "Moving Time (min)",
                      "Avg Pace (min/km)", "Avg HR", "Max HR", "Elevation Gain (m)", "Temp (°C)", "Feels Like (°C)",
                      "Humidity (%)", "Wind (km/h)", "Conditions", "Heat Adj Pace (min/km)", "Notes"]
TRUTHY = {"y", "yes", "true", "1", "indoor"}


def compact_log_row(r: dict[str, Any], note_chars: int = 160) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in LOG_COMPACT_FIELDS:
        v = r.get(k)
        if v is None or str(v).strip() == "":
            continue
        if k == "Notes" and len(str(v)) > note_chars:
            v = str(v)[:note_chars] + "…"
        out[k] = v
    return out


def aggregate_log(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_sport: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "km": 0.0, "min": 0.0})
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "km": 0.0, "min": 0.0})
    hr: list[float] = []
    longest: dict[str, Any] | None = None
    for r in rows:
        km = to_float(r.get("Distance (km)")) or 0.0
        mins = to_float(r.get("Moving Time (min)")) or 0.0
        d = parse_date(r.get("Date"))
        for bucket in (by_sport[str(r.get("Sport") or "Other")], by_month[d.strftime("%Y-%m") if d else "?"]):
            bucket["count"] += 1
            bucket["km"] += km
            bucket["min"] += mins
        h = to_float(r.get("Avg HR"))
        if h:
            hr.append(h)
        if longest is None or km > (to_float(longest.get("Distance (km)")) or 0.0):
            longest = r
    rnd = lambda b: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in b.items()}
    return {
        "count": len(rows),
        "distance_km": round(sum(to_float(r.get("Distance (km)")) or 0.0 for r in rows), 1),
        "moving_time_min": round(sum(to_float(r.get("Moving Time (min)")) or 0.0 for r in rows), 0),
        "avg_hr": round(sum(hr) / len(hr)) if hr else None,
        "by_sport": {k: rnd(v) for k, v in sorted(by_sport.items())},
        "by_month": {k: rnd(v) for k, v in sorted(by_month.items())},
        "longest": compact_log_row(longest) if longest else None,
    }


def filter_log(rows: list[dict[str, Any]], *, sports: list[str] | None = None, date_from: date | None = None,
               date_to: date | None = None, indoor: bool | None = None, name_contains: str | None = None,
               sort: str = "desc", limit: int = 20) -> dict[str, Any]:
    """Filter + sort + aggregate Training Log rows. Deterministic; returns compact rows with sheet row numbers."""
    wanted = {s.lower() for s in sports} if sports else None
    picked: list[tuple[date, dict[str, Any]]] = []
    for r in rows:
        d = parse_date(r.get("Date"))
        if not d:
            continue
        if wanted and str(r.get("Sport") or "").lower() not in wanted:
            continue
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        if indoor is not None and (str(r.get("Indoor") or "").strip().lower() in TRUTHY) != indoor:
            continue
        if name_contains and name_contains.lower() not in str(r.get("Name") or "").lower():
            continue
        picked.append((d, r))
    picked.sort(key=lambda x: (x[0], str(x[1].get("Activity ID") or ""), x[1].get("_row", 0)), reverse=(sort != "asc"))
    matched = [r for _, r in picked]
    return {"matched": len(matched), "returned": min(limit, len(matched)),
            "rows": [compact_log_row(r) for r in matched[:limit]], "aggregates": aggregate_log(matched)}
