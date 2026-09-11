"""Open-Meteo client (free, no key). Historical hourly for Strava enrichment, daily/hourly forecast for planning."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,precipitation,weather_code"
DAILY_VARS = ("temperature_2m_max,temperature_2m_min,apparent_temperature_max,precipitation_sum,"
              "precipitation_probability_max,wind_speed_10m_max,weather_code,sunrise,sunset")

WEATHER_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
}


def describe_code(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), f"code {code}")
    except (TypeError, ValueError):
        return ""


class WeatherService:
    def __init__(self, timeout: float = 20.0):
        self.http = httpx.AsyncClient(timeout=timeout)
        self._sem = asyncio.Semaphore(2)
        self._hourly_cache: dict[tuple[float, float, str, str], dict[str, dict[str, Any]]] = {}

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # Open-Meteo's free tier rate-limits bursts (429). Serialise requests and back off.
        async with self._sem:
            for attempt in range(5):
                r = await self.http.get(url, params=params)
                if r.status_code == 429 and attempt < 4:
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                r.raise_for_status()
                return r.json()
        raise RuntimeError("unreachable")

    async def hourly_for_dates(self, lat: float, lng: float, dates: set[date], max_gap_days: int = 10) -> dict[str, dict[str, Any]]:
        """Hourly weather covering only the given dates, fetched as a few contiguous clusters rather than one giant span."""
        ordered = sorted(dates)
        clusters: list[list[date]] = []
        for d in ordered:
            if clusters and (d - clusters[-1][-1]).days <= max_gap_days:
                clusters[-1].append(d)
            else:
                clusters.append([d])
        out: dict[str, dict[str, Any]] = {}
        for c in clusters:
            out.update(await self.hourly_range(lat, lng, c[0], c[-1]))
        return out

    @staticmethod
    def _index_hourly(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        h = payload.get("hourly") or {}
        times = h.get("time") or []
        out: dict[str, dict[str, Any]] = {}
        for i, t in enumerate(times):
            def pick(key: str) -> Any:
                arr = h.get(key) or []
                return arr[i] if i < len(arr) else None
            out[t] = {
                "temp_c": pick("temperature_2m"),
                "feels_like_c": pick("apparent_temperature"),
                "humidity_pct": pick("relative_humidity_2m"),
                "wind_kmh": pick("wind_speed_10m"),
                "precip_mm": pick("precipitation"),
                "conditions": describe_code(pick("weather_code")),
            }
        return out

    async def hourly_range(self, lat: float, lng: float, start: date, end: date) -> dict[str, dict[str, Any]]:
        """Hourly weather (local time at the location) keyed 'YYYY-MM-DDTHH:00' for [start, end]."""
        key = (round(lat, 2), round(lng, 2), start.isoformat(), end.isoformat())
        if key in self._hourly_cache:
            return self._hourly_cache[key]

        today = date.today()
        result: dict[str, dict[str, Any]] = {}
        base = {"latitude": lat, "longitude": lng, "hourly": HOURLY_VARS, "timezone": "auto"}
        recent_cutoff = today - timedelta(days=88)  # forecast API serves up to 92 past days

        if start < recent_cutoff:
            archive_end = min(end, recent_cutoff - timedelta(days=1))
            payload = await self._get(ARCHIVE_URL, {**base, "start_date": start.isoformat(), "end_date": archive_end.isoformat()})
            result.update(self._index_hourly(payload))
        if end >= recent_cutoff:
            recent_start = max(start, recent_cutoff)
            past_days = max(0, (today - recent_start).days + 1)
            forecast_days = max(1, min(16, (end - today).days + 1)) if end >= today else 1
            payload = await self._get(FORECAST_URL, {**base, "past_days": min(92, past_days), "forecast_days": forecast_days})
            result.update(self._index_hourly(payload))

        self._hourly_cache[key] = result
        return result

    @staticmethod
    def lookup(hourly: dict[str, dict[str, Any]], local_dt: datetime) -> dict[str, Any] | None:
        key = local_dt.strftime("%Y-%m-%dT%H:00")
        return hourly.get(key)

    async def at(self, lat: float, lng: float, local_dt: datetime) -> dict[str, Any] | None:
        hourly = await self.hourly_range(lat, lng, local_dt.date(), local_dt.date())
        return self.lookup(hourly, local_dt)

    async def daily_forecast(self, lat: float, lng: float, days: int = 7) -> list[dict[str, Any]]:
        payload = await self._get(FORECAST_URL, {
            "latitude": lat, "longitude": lng, "daily": DAILY_VARS, "timezone": "auto",
            "forecast_days": max(1, min(16, days)),
        })
        d = payload.get("daily") or {}
        out = []
        for i, day in enumerate(d.get("time") or []):
            def pick(key: str) -> Any:
                arr = d.get(key) or []
                return arr[i] if i < len(arr) else None
            out.append({
                "date": day,
                "min_c": pick("temperature_2m_min"),
                "max_c": pick("temperature_2m_max"),
                "feels_like_max_c": pick("apparent_temperature_max"),
                "precip_prob_pct": pick("precipitation_probability_max"),
                "precip_mm": pick("precipitation_sum"),
                "wind_max_kmh": pick("wind_speed_10m_max"),
                "weather_code": pick("weather_code"),
                "conditions": describe_code(pick("weather_code")),
                "sunrise": (pick("sunrise") or "")[-5:],
                "sunset": (pick("sunset") or "")[-5:],
            })
        return out

    async def hourly_forecast(self, lat: float, lng: float, days: int = 3) -> list[dict[str, Any]]:
        payload = await self._get(FORECAST_URL, {
            "latitude": lat, "longitude": lng, "hourly": HOURLY_VARS, "timezone": "auto",
            "forecast_days": max(1, min(7, days)),
        })
        indexed = self._index_hourly(payload)
        # keep it compact for the model: 05:00–21:00 only
        return [{"time": t, **v} for t, v in indexed.items() if 5 <= int(t[11:13]) <= 21]
