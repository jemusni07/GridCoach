"""OpenAI function-tool schemas + the dispatcher that runs them against one spreadsheet."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..services import analytics as an
from ..services.pipeline import Pipeline
from ..services.sheets import LAYOUT, PLAN_HEADERS, TAB_COACH, TAB_LOG, TAB_MEMORY, SheetsService
from ..services.strava import StravaError
from ..services.weather import WeatherService

log = logging.getLogger("gridcoach.tools")

PLAN_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "week": {"type": ["integer", "string"], "description": "Plan week number (1-based)"},
        "day": {"type": "string", "description": "Mon/Tue/..."},
        "workout_type": {"type": "string", "description": "Easy, Long, Tempo, Intervals, Race Pace, Recovery, Strength, Cross-Train, Swim, Bike, Brick, Rest"},
        "description": {"type": "string", "description": "What to do, e.g. '6 x 800m @ 5k pace, 400m jog'"},
        "target_distance_km": {"type": ["number", "string", "null"]},
        "target_duration_min": {"type": ["number", "string", "null"]},
        "target_intensity": {"type": ["string", "null"], "description": "Pace, HR zone, RPE, e.g. 'Z2', '5:10/km', 'RPE 7'"},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["date", "workout_type", "description"],
}

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "name": "list_sheets", "description": "List all tabs in the spreadsheet with their header rows. Use to discover custom tabs/columns.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "query_log",
     "description": ("Deterministic query over the Training Log: filter by sport(s), date range, indoor/outdoor, name; sorted by date "
                     "(desc = most recent first). Returns compact rows WITH sheet row numbers plus aggregates (count, km, minutes, "
                     "by sport, by month, longest). ALWAYS use this for 'last/most recent X', 'how many', 'summary of', 'what did I do on <date>', "
                     "and to check for existing entries before logging a manual session. Never derive these by eye from read_range."),
     "parameters": {"type": "object", "properties": {
         "sports": {"type": ["array", "null"], "items": {"type": "string"}, "description": "e.g. ['Run','TrailRun'] — Strava sport_type names; null = all"},
         "date_from": {"type": ["string", "null"], "description": "YYYY-MM-DD inclusive"},
         "date_to": {"type": ["string", "null"], "description": "YYYY-MM-DD inclusive"},
         "indoor": {"type": ["boolean", "null"], "description": "true = only indoor (treadmill/trainer), false = only outdoor, null = both"},
         "name_contains": {"type": ["string", "null"]},
         "sort": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
         "limit": {"type": "integer", "default": 20, "maximum": 200},
     }}},
    {"type": "function", "name": "read_range",
     "description": ("Read a tab. If the first row is a header you get `records` (header→value objects with `row` = sheet row number; "
                     "missing key = empty cell), otherwise a 2-D `values` array. Omit range for the whole tab. For the Training Log prefer query_log."),
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"},
         "range": {"type": ["string", "null"], "description": "A1 notation like 'A1:F50'; null for the whole tab"},
         "max_rows": {"type": "integer", "default": 200},
     }, "required": ["sheet"]}},
    {"type": "function", "name": "write_range",
     "description": "Overwrite a rectangular block of cells. Use for Settings values or edits the athlete explicitly asked for. Values are 2-D (rows of cells). Formulas allowed.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"},
         "range": {"type": "string", "description": "Top-left cell or full A1 range, e.g. 'B3' or 'B3:D3'"},
         "values": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "null"]}}},
     }, "required": ["sheet", "range", "values"]}},
    {"type": "function", "name": "append_rows",
     "description": ("Append rows to the bottom of a tab. PREFER objects keyed by the tab's header names ({\"Date\": ..., \"Sport\": ...}) — "
                     "they are mapped by header so columns can never misalign; unknown keys are reported back. Plain arrays are accepted "
                     "only for tabs without a header row."),
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"},
         "rows": {"type": "array", "items": {"anyOf": [
             {"type": "object", "additionalProperties": {"type": ["string", "number", "null"]}},
             {"type": "array", "items": {"type": ["string", "number", "null"]}},
         ]}},
     }, "required": ["sheet", "rows"]}},
    {"type": "function", "name": "sort_tab",
     "description": "Sort a tab IN PLACE by a column (header name or column letter). Header row stays. One call sorts everything — use this for 'sort/organize/order my log'.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "by_column": {"type": "string", "description": "Header name like 'Date', or a letter like 'A'"},
         "order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
     }, "required": ["sheet", "by_column"]}},
    {"type": "function", "name": "copy_range",
     "description": "Copy a block (or a whole tab) to another tab in ONE server-side write — formulas preserved. Use this to move/duplicate data; never read rows and re-append them yourself.",
     "parameters": {"type": "object", "properties": {
         "from_sheet": {"type": "string"}, "range": {"type": ["string", "null"], "description": "A1 range; null = whole tab"},
         "to_sheet": {"type": "string"}, "to_cell": {"type": "string", "default": "A1"},
     }, "required": ["from_sheet", "to_sheet"]}},
    {"type": "function", "name": "delete_rows",
     "description": "Delete specific rows (1-based sheet row numbers, header row is protected). DESTRUCTIVE: only when the athlete explicitly asked to remove/dedupe those rows; name the rows in your reply.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "rows": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
     }, "required": ["sheet", "rows"]}},
    {"type": "function", "name": "create_chart",
     "description": ("Insert a real embedded chart on a tab (line, column, bar, area, scatter, pie). domain_range is the x-axis column "
                     "(e.g. 'A1:A20' with the header in row 1); series_ranges are y columns (e.g. ['B1:B20','C1:C20']) — first cell of each is "
                     "the series name. Tip: first write a tidy table (one row per x value, one column per series) with write_range, then chart it."),
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"},
         "chart_type": {"type": "string", "enum": ["line", "column", "bar", "area", "scatter", "pie"]},
         "title": {"type": "string"},
         "domain_range": {"type": "string"},
         "series_ranges": {"type": "array", "items": {"type": "string"}, "minItems": 1},
         "anchor_cell": {"type": "string", "default": "H2", "description": "Top-left cell where the chart is placed"},
         "x_title": {"type": ["string", "null"]}, "y_title": {"type": ["string", "null"]},
     }, "required": ["sheet", "chart_type", "title", "domain_range", "series_ranges"]}},
    {"type": "function", "name": "write_training_plan",
     "description": "Write structured sessions to the Training Plan tab. Deterministic column mapping. replace=true clears the existing plan first.",
     "parameters": {"type": "object", "properties": {
         "rows": {"type": "array", "items": PLAN_ROW_SCHEMA, "minItems": 1},
         "replace": {"type": "boolean", "default": False},
     }, "required": ["rows"]}},
    {"type": "function", "name": "sync_strava",
     "description": ("WRITES to the sheet: pull the last N days of activities into the Training Log (weather-enriched), recompute snapshot and plan "
                     "statuses. Only when the athlete asks to sync/update/log. For QUESTIONS about history or totals use strava_query / strava_stats instead."),
     "parameters": {"type": "object", "properties": {"days_back": {"type": "integer", "description": "Default 90", "minimum": 1, "maximum": 730}}}},
    {"type": "function", "name": "get_activity_analysis",
     "description": "Deep-dive one activity: km splits, first/second half pace & HR, aerobic decoupling, cardiac drift, cadence, weather. Get the Activity ID from the Training Log.",
     "parameters": {"type": "object", "properties": {"activity_id": {"type": "string"}}, "required": ["activity_id"]}},
    {"type": "function", "name": "get_weather_forecast",
     "description": "Forecast for a location (defaults to Settings → Home Latitude/Longitude). hourly=true returns 05:00–21:00 hourly rows for up to 3 days; otherwise daily rows for up to 14 days.",
     "parameters": {"type": "object", "properties": {
         "lat": {"type": ["number", "null"]}, "lng": {"type": ["number", "null"]},
         "days": {"type": "integer", "default": 7}, "hourly": {"type": "boolean", "default": False},
     }}},
    {"type": "function", "name": "refresh_weather_tab",
     "description": "Rewrite the Weather tab with the daily forecast for the home location (or last activity location).",
     "parameters": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}}},
    {"type": "function", "name": "recompute_snapshot",
     "description": "Recompute the Health Snapshot metrics and Training Plan statuses from the current sheet contents (no Strava call). Use after editing the plan/log.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "add_coach_note",
     "description": "Append a dated observation to the Coach Notes tab (only if the athlete has that tab).",
     "parameters": {"type": "object", "properties": {
         "note": {"type": "string"}, "activity": {"type": ["string", "null"], "description": "Activity ID or name this note is about"},
     }, "required": ["note"]}},
    {"type": "function", "name": "strava_stats",
     "description": ("Strava's official totals for the athlete — year-to-date, last 4 weeks, and all-time — per sport (run/ride/swim): "
                     "count, km, hours, elevation. Instant and exact; no sheet writes. Use for 'my 2026 summary', 'how much have I run this year', "
                     "'all-time totals'."),
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "strava_query",
     "description": ("Query the athlete's FULL Strava history (all years; cached locally, backfilled on first use) WITHOUT touching the sheet. "
                     "Same filters and aggregates as query_log (by sport, date range, indoor/outdoor, name; sorted by date; count/km/min by "
                     "sport and by month; longest). Use for anything outside the Training Log's window, cross-year comparisons, or when the "
                     "athlete asks about 'Strava' rather than 'my log'. Rows carry Activity ID, Kudos, Avg Watts, Gear ID, Start Local."),
     "parameters": {"type": "object", "properties": {
         "sports": {"type": ["array", "null"], "items": {"type": "string"}},
         "date_from": {"type": ["string", "null"], "description": "YYYY-MM-DD inclusive"},
         "date_to": {"type": ["string", "null"], "description": "YYYY-MM-DD inclusive"},
         "indoor": {"type": ["boolean", "null"]},
         "name_contains": {"type": ["string", "null"]},
         "sort": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
         "limit": {"type": "integer", "default": 20, "maximum": 300},
     }}},
    {"type": "function", "name": "strava_zones",
     "description": "The athlete's configured heart-rate zones (and power zones if set) from Strava. Use for zone-based advice and to interpret Avg HR.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "strava_gear",
     "description": "Shoes and bikes from the Strava profile with cumulative distance, primary/retired flags. Use for 'how many km on my shoes', gear rotation advice.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "analyze_data",
     "description": ("Run a short pandas snippet over the athlete's tabs for nuanced analysis (comparisons, trends, conditional stats, "
                     "correlations, weekly/rolling views). In scope: `log` (Training Log), `plan`, `health`, `tabs['Any Tab']`, `pd`, `np`. "
                     "Columns are the sheet headers; numbers are coerced, `Date` is datetime, `row` is the sheet row number, and m:ss pace "
                     "columns also exist as '<col>_min' decimals (e.g. 'Avg Pace (min/km)_min'). `strava` = the athlete's FULL Strava history "
                     "(all years, no weather columns) for cross-year or long-range questions. Assign your answer to `result` "
                     "(DataFrame / Series / dict / number); print() is captured. No files, no network, ≤30 s. "
                     "If it errors you get the traceback — fix and retry. Use query_log for simple lookups; use this when the question has nuance."),
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string"},
         "tabs": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Tabs to load; default Training Log, Training Plan, Health Notes, Strava (the full-history cache)"},
     }, "required": ["code"]}},
    {"type": "function", "name": "remember",
     "description": ("Save a durable fact about the athlete so it is available in every future conversation: home location, race goals, "
                     "injuries/limits, preferences ('hates treadmill'), weekly schedule constraints, equipment. Not for transient details."),
     "parameters": {"type": "object", "properties": {
         "fact": {"type": "string", "description": "One concise sentence, e.g. 'Lives in Dubai (25.2N, 55.3E); trains evenings'"},
         "category": {"type": "string", "enum": ["profile", "race", "health", "preference", "schedule", "equipment", "other"]},
     }, "required": ["fact", "category"]}},
    {"type": "function", "name": "forget",
     "description": "Remove a remembered fact by its id (ids are shown in the athlete_memory block) when it is wrong or no longer true.",
     "parameters": {"type": "object", "properties": {"memory_id": {"type": "integer"}}, "required": ["memory_id"]}},
    {"type": "function", "name": "create_tab",
     "description": ("Create a tab. Tabs are opt-in — the athlete decides what lives in their sheet — so only call this when they "
                     "asked for it or agreed to it. Standard GridCoach tabs (Training Log, Training Plan, Health Snapshot, Health Notes, "
                     "Weather, Coach Notes, Settings) get their canonical headers automatically; for a custom tab pass headers."),
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"},
         "headers": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Header row for a custom tab; ignored for standard tabs"},
     }, "required": ["name"]}},
]

ANALYST_TOOL_NAMES = {"read_range", "query_log", "strava_query", "analyze_data", "add_coach_note", "get_activity_analysis", "list_sheets"}
ANALYST_TOOLS = [t for t in TOOLS if t["name"] in ANALYST_TOOL_NAMES]


def describe_call(name: str, a: dict[str, Any]) -> str:
    """Human-readable progress line for the sidebar."""
    n = lambda k: len(a.get(k) or [])
    q = []
    if a.get("sports"): q.append("/".join(a["sports"]))
    if a.get("indoor") is True: q.append("indoor")
    if a.get("indoor") is False: q.append("outdoor")
    if a.get("date_from") or a.get("date_to"): q.append(f"{a.get('date_from') or '…'}→{a.get('date_to') or '…'}")
    return {
        "list_sheets": "Listing tabs",
        "read_range": f"Reading {a.get('sheet')}" + (f"!{a['range']}" if a.get("range") else ""),
        "query_log": "Querying log" + (f" ({', '.join(q)})" if q else ""),
        "write_range": f"Writing {a.get('sheet')}!{a.get('range')}",
        "append_rows": f"Appending {n('rows')} row(s) to {a.get('sheet')}",
        "write_training_plan": f"Writing {n('rows')} plan sessions" + (" (replacing plan)" if a.get("replace") else ""),
        "sync_strava": f"Syncing Strava (last {a.get('days_back') or 90} days)",
        "get_activity_analysis": f"Analysing activity {a.get('activity_id')}",
        "get_weather_forecast": "Fetching weather forecast",
        "refresh_weather_tab": "Refreshing Weather tab",
        "recompute_snapshot": "Recomputing snapshot metrics",
        "add_coach_note": "Adding a coach note",
        "create_tab": f"Creating tab '{a.get('name')}'",
        "sort_tab": f"Sorting {a.get('sheet')} by {a.get('by_column')} ({a.get('order', 'asc')})",
        "copy_range": f"Copying {a.get('from_sheet')} → {a.get('to_sheet')}",
        "delete_rows": f"Deleting rows {a.get('rows')} from {a.get('sheet')}",
        "create_chart": f"Creating chart '{a.get('title')}' on {a.get('sheet')}",
        "analyze_data": "Running analysis (pandas)",
        "strava_stats": "Fetching Strava totals",
        "strava_query": "Querying Strava history" + (f" ({', '.join(q)})" if q else ""),
        "strava_zones": "Fetching HR/power zones",
        "strava_gear": "Fetching gear",
        "remember": "Saving to memory",
        "forget": "Forgetting a memory",
    }.get(name, name.replace("_", " "))


def summarize_result(name: str, r: dict[str, Any]) -> str:
    if "error" in r:
        return "✗ " + str(r["error"])[:140]
    if "pending_confirmation" in r:
        return "needs your confirmation"
    if name == "sync_strava":
        return f"{r.get('fetched')} activities · {r.get('appended')} new · {r.get('updated')} updated"
    if name in ("query_log", "strava_query"):
        return f"{r.get('matched')} match(es)"
    if name == "read_range":
        return f"{len(r.get('records') or r.get('values') or [])} rows" + (f" · {r['date_order']}" if r.get("date_order") else "")
    if name == "analyze_data":
        return "ok" if r.get("result") is not None or r.get("stdout") else "no result"
    if name in ("append_rows", "write_range", "copy_range", "sort_tab", "write_training_plan", "create_chart", "create_tab"):
        return r.get("range") or "done"
    return "done"


# Tools that change or destroy existing data; parked until the athlete presses Confirm in the sidebar.
CONFIRM_ALWAYS = {"delete_rows"}


class ToolRunner:
    def __init__(self, spreadsheet_id: str, sheets: SheetsService, pipeline: Pipeline, weather: WeatherService, source: str = "sidebar",
                 on_event: Any = None, pending: Any = None, store: Any = None, bypass_confirm: bool = False):
        self.sid = spreadsheet_id
        self.sheets = sheets
        self.pipeline = pipeline
        self.weather = weather
        self.source = source
        self.on_event = on_event
        self.pending = pending
        self.store = store
        self.bypass_confirm = bypass_confirm
        self.touched: list[str] = []
        self.log: list[dict[str, Any]] = []
        self.pending_actions: list[dict[str, Any]] = []

    def _touch(self, sheet: str, rng: str | None) -> None:
        if rng:
            self.touched.append(f"{sheet}!{rng}")

    def _emit(self, type: str, text: str, **extra: Any) -> None:
        if self.on_event:
            try:
                self.on_event(type, text, **extra)
            except Exception:
                pass

    async def _needs_confirmation(self, name: str, args: dict[str, Any]) -> str | None:
        """Return a plain-language description if this call would destroy/overwrite existing data."""
        if self.bypass_confirm or self.pending is None:
            return None
        if name in CONFIRM_ALWAYS:
            return f"Delete rows {sorted(set(args.get('rows') or []))} from '{args.get('sheet')}'"
        if name == "write_training_plan" and args.get("replace"):
            existing = await asyncio.to_thread(self.sheets.read_tab_dicts, self.sid, "Training Plan")
            if existing:
                return f"Replace the existing Training Plan ({len(existing)} sessions) with {len(args.get('rows') or [])} new sessions"
        if name == "write_range" and args.get("sheet") not in ("Settings",):
            try:
                current = await asyncio.to_thread(self.sheets.read_range, self.sid, args["sheet"], args["range"], 50, 40)
            except Exception:
                return None
            cells = current.get("values") or [[v for k, v in rec.items() if k != "row"] for rec in current.get("records") or []]
            filled = sum(1 for row in cells for c in row if str(c).strip())
            # header-row-only reads come back as records=[]; count the header too
            filled += sum(1 for h in current.get("headers") or [] if str(h).strip())
            if filled:
                return f"Overwrite {filled} non-empty cell(s) in '{args['sheet']}'!{args['range']}"
        if name == "copy_range":
            try:
                if await asyncio.to_thread(self.sheets.block_has_values, self.sid, args["to_sheet"], args.get("to_cell") or "A1"):
                    return f"Overwrite existing data in '{args['to_sheet']}' starting at {args.get('to_cell') or 'A1'}"
            except Exception:
                return None
        return None

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        self._emit("tool_start", describe_call(name, args), tool=name)
        try:
            handler = getattr(self, f"t_{name}", None)
            if handler is None:
                result: dict[str, Any] = {"error": f"unknown tool {name}"}
            else:
                description = await self._needs_confirmation(name, args)
                if description:
                    item = self.pending.add(self.sid, name, args, description)
                    self.pending_actions.append({k: item[k] for k in ("action_id", "tool", "description")})
                    result = {"pending_confirmation": {"action_id": item["action_id"], "description": description},
                              "instruction": ("NOT executed. Tell the athlete exactly what this would do and that a Confirm button "
                                              "is waiting in the sidebar. Do not claim it is done. Continue with anything non-destructive.")}
                else:
                    result = await handler(**args)
        except StravaError as e:
            result = {"error": str(e), "hint": "Ask the athlete to click 'Connect Strava' in the sidebar."}
        except TypeError as e:  # bad/missing arguments from the model — let it retry with the schema
            result = {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:  # tool errors go back to the model, never crash the turn
            log.exception("tool %s failed", name)
            result = {"error": f"{type(e).__name__}: {e}"}
        ms = round((time.time() - t0) * 1000)
        self.log.append({"tool": name, "args": args, "ok": "error" not in result, "ms": ms})
        self._emit("tool_end", summarize_result(name, result), tool=name, ok="error" not in result, ms=ms)
        return result

    # ---- memory + analysis ----------------------------------------------------

    async def t_remember(self, fact: str, category: str = "other") -> dict[str, Any]:
        if self.store is None:
            return {"error": "memory store unavailable"}
        m = self.store.add_memory(self.sid, category, fact)
        await self._mirror_memory()
        return m

    async def t_forget(self, memory_id: int) -> dict[str, Any]:
        if self.store is None:
            return {"error": "memory store unavailable"}
        ok = self.store.forget_memory(self.sid, int(memory_id))
        await self._mirror_memory()
        return {"forgotten": ok, "memory_id": memory_id}

    async def _mirror_memory(self) -> None:
        try:
            if await asyncio.to_thread(self.sheets.has_tab, self.sid, TAB_MEMORY):
                await asyncio.to_thread(self.sheets.write_memory_tab, self.sid, self.store.list_memories(self.sid))
        except Exception:
            log.warning("could not mirror memory to sheet", exc_info=True)

    async def t_analyze_data(self, code: str, tabs: list[str] | None = None) -> dict[str, Any]:
        from ..services.sandbox import run_analysis
        wanted = tabs or [TAB_LOG, "Training Plan", "Health Notes", "Strava"]
        present = set(await asyncio.to_thread(self.sheets.tab_titles, self.sid))
        data: dict[str, list[list[Any]]] = {}
        for t in wanted:
            if t.lower() == "strava" and self.pipeline is not None:
                try:
                    await self.pipeline.cache.ensure(self.sid, progress=self._emit)
                    data["Strava"] = await asyncio.to_thread(self.pipeline.cache.table, self.sid)
                except StravaError as e:
                    if tabs and any(x.lower() == "strava" for x in tabs):
                        return {"error": str(e)}
            elif t in present:
                data[t] = await asyncio.to_thread(self.sheets.raw_values, self.sid, t)
        if not data:
            return {"error": f"none of the requested tabs exist: {wanted}"}
        try:
            return await run_analysis(data, code)
        except ValueError as e:
            return {"error": str(e)}

    # ---- handlers -----------------------------------------------------------

    async def t_list_sheets(self) -> dict[str, Any]:
        sheets = await asyncio.to_thread(self.sheets.list_sheets, self.sid)
        present = {s["title"] for s in sheets}
        return {"sheets": sheets, "standard_tabs_missing": [t for t in LAYOUT if t not in present]}

    async def t_create_tab(self, name: str, headers: list[str] | None = None) -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.create_tab, self.sid, name, headers)
        if res["created"]:
            self._touch(name, "A1")
        return res

    async def t_read_range(self, sheet: str, range: str | None = None, max_rows: int = 200) -> dict[str, Any]:
        return await asyncio.to_thread(self.sheets.read_range, self.sid, sheet, range, max(1, min(int(max_rows or 200), 500)))

    async def t_write_range(self, sheet: str, range: str, values: list[list[Any]]) -> dict[str, Any]:
        values = [[("" if c is None else c) for c in row] for row in values]
        res = await asyncio.to_thread(self.sheets.write_range, self.sid, sheet, range, values)
        self._touch(sheet, res["range"])
        return res

    async def t_append_rows(self, sheet: str, rows: list[Any]) -> dict[str, Any]:
        if rows and isinstance(rows[0], dict):
            records = [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]
            res = await asyncio.to_thread(self.sheets.append_records, self.sid, sheet, records)
        else:
            rows = [[("" if c is None else c) for c in row] for row in rows]
            res = await asyncio.to_thread(self.sheets.append_rows, self.sid, sheet, rows)
        self._touch(sheet, res["range"])
        return res

    async def t_query_log(self, sports: list[str] | None = None, date_from: str | None = None, date_to: str | None = None,
                          indoor: bool | None = None, name_contains: str | None = None, sort: str = "desc", limit: int = 20) -> dict[str, Any]:
        rows = await asyncio.to_thread(self.sheets.read_tab_dicts, self.sid, TAB_LOG, True)
        if not rows:
            return {"matched": 0, "rows": [], "note": f"'{TAB_LOG}' is missing or empty."}
        return an.filter_log(rows, sports=sports, date_from=an.parse_date(date_from), date_to=an.parse_date(date_to),
                             indoor=indoor, name_contains=name_contains, sort=sort, limit=max(1, min(int(limit or 20), 200)))

    # ---- Strava data layer (full history, no sheet writes) ------------------------

    async def t_strava_stats(self) -> dict[str, Any]:
        from ..services.strava_cache import simplify_stats
        return simplify_stats(await self.pipeline.strava.athlete_stats(self.sid))

    async def t_strava_query(self, sports: list[str] | None = None, date_from: str | None = None, date_to: str | None = None,
                             indoor: bool | None = None, name_contains: str | None = None, sort: str = "desc", limit: int = 20) -> dict[str, Any]:
        info = await self.pipeline.cache.ensure(self.sid, progress=self._emit)
        rows = await asyncio.to_thread(self.pipeline.cache.rows, self.sid)
        res = an.filter_log(rows, sports=sports, date_from=an.parse_date(date_from), date_to=an.parse_date(date_to),
                            indoor=indoor, name_contains=name_contains, sort=sort, limit=max(1, min(int(limit or 20), 300)))
        res["cache"] = {k: info.get(k) for k in ("count", "oldest", "newest")}
        return res

    async def t_strava_zones(self) -> dict[str, Any]:
        z = await self.pipeline.strava.athlete_zones(self.sid)
        out: dict[str, Any] = {}
        for kind in ("heart_rate", "power"):
            if z.get(kind):
                zones = z[kind].get("zones") or []
                out[kind] = {"custom": bool(z[kind].get("custom_zones")),
                             "zones": [{"zone": i + 1, "min": zz.get("min"), "max": zz.get("max")} for i, zz in enumerate(zones)]}
        return out or {"note": "no zones configured on Strava"}

    async def t_strava_gear(self) -> dict[str, Any]:
        from ..services.strava_cache import simplify_gear
        return simplify_gear(await self.pipeline.strava.athlete(self.sid))

    async def t_sort_tab(self, sheet: str, by_column: str, order: str = "asc") -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.sort_tab, self.sid, sheet, by_column, order)
        self._touch(sheet, res.get("range"))
        return res

    async def t_copy_range(self, from_sheet: str, to_sheet: str, range: str | None = None, to_cell: str = "A1") -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.copy_range, self.sid, from_sheet, range, to_sheet, to_cell or "A1")
        self._touch(to_sheet, res.get("range"))
        return res

    async def t_delete_rows(self, sheet: str, rows: list[int]) -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.delete_rows, self.sid, sheet, rows)
        self._touch(sheet, f"A{min(rows)}" if rows else None)
        return res

    async def t_create_chart(self, sheet: str, chart_type: str, title: str, domain_range: str, series_ranges: list[str],
                             anchor_cell: str = "H2", x_title: str | None = None, y_title: str | None = None) -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.create_chart, self.sid, sheet, chart_type, title, domain_range,
                                      series_ranges, anchor_cell or "H2", x_title or "", y_title or "")
        self._touch(sheet, anchor_cell or "H2")
        return res

    async def t_write_training_plan(self, rows: list[dict[str, Any]], replace: bool = False) -> dict[str, Any]:
        mapped = []
        for r in rows:
            d = an.parse_date(r.get("date"))
            mapped.append({
                "Date": d.isoformat() if d else str(r.get("date", "")),
                "Week": r.get("week", ""),
                "Day": r.get("day") or (d.strftime("%a") if d else ""),
                "Workout Type": r.get("workout_type", ""),
                "Description": r.get("description", ""),
                "Target Distance (km)": r.get("target_distance_km") or "",
                "Target Duration (min)": r.get("target_duration_min") or "",
                "Target Intensity": r.get("target_intensity") or "",
                "Notes": r.get("notes") or "",
                "Status": "",
            })
        mapped.sort(key=lambda m: m["Date"])
        res = await asyncio.to_thread(self.sheets.write_plan, self.sid, mapped, bool(replace))
        self._touch(res["sheet"], res["range"])
        recompute = await self.pipeline.recompute(self.sid)
        return {**res, "columns": PLAN_HEADERS, "plan_rows_reconciled": recompute["plan_rows_reconciled"]}

    async def t_sync_strava(self, days_back: int | None = None) -> dict[str, Any]:
        res = await self.pipeline.sync(self.sid, days_back)
        for rng in res.pop("touched", []):
            self.touched.append(rng if "!" in rng else f"{TAB_LOG}!{rng}")
        return res

    async def t_get_activity_analysis(self, activity_id: str) -> dict[str, Any]:
        return await self.pipeline.analyze_activity(self.sid, str(activity_id).strip())

    async def t_get_weather_forecast(self, lat: float | None = None, lng: float | None = None, days: int = 7, hourly: bool = False) -> dict[str, Any]:
        if lat is None or lng is None:
            settings = await asyncio.to_thread(self.sheets.get_settings, self.sid)
            lat, lng = an.to_float(settings.get("Home Latitude")), an.to_float(settings.get("Home Longitude"))
        if lat is None or lng is None:
            return {"error": "No location. Ask the athlete for a city/coordinates and write Home Latitude/Longitude to Settings, or pass lat/lng."}
        if hourly:
            return {"lat": lat, "lng": lng, "hourly": await self.weather.hourly_forecast(lat, lng, min(int(days or 3), 3))}
        return {"lat": lat, "lng": lng, "daily": await self.weather.daily_forecast(lat, lng, min(int(days or 7), 14))}

    async def t_refresh_weather_tab(self, days: int = 7) -> dict[str, Any]:
        res = await self.pipeline.refresh_weather(self.sid, int(days or 7))
        self._touch(res["sheet"], res["range"])
        return {k: v for k, v in res.items() if k != "forecast"} | {"forecast": res["forecast"][:7]}

    async def t_recompute_snapshot(self) -> dict[str, Any]:
        res = await self.pipeline.recompute(self.sid)
        self.touched.append(res["snapshot_range"])
        return res

    async def t_add_coach_note(self, note: str, activity: str | None = None) -> dict[str, Any]:
        res = await asyncio.to_thread(self.sheets.add_coach_note, self.sid, self.source, activity or "", note)
        self._touch(TAB_COACH, res["range"])
        return res


def dumps(obj: Any, limit: int = 24000) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + '... [truncated — use a smaller range or max_rows]"}'
