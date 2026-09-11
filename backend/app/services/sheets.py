"""Google Sheets access via a service account (gspread).

Writes use USER_ENTERED so dates/numbers parse, and only ever touch *values* —
never formatting — except on tabs GridCoach itself creates (NFR-3).
Columns are located by header name, so users can reorder/insert columns freely.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

import gspread
from gspread.utils import ValueInputOption, a1_range_to_grid_range, rowcol_to_a1

# ---- canonical tab layout -------------------------------------------------

TAB_PLAN = "Training Plan"
TAB_LOG = "Training Log"
TAB_HEALTH = "Health Notes"
TAB_WEATHER = "Weather"
TAB_SNAPSHOT = "Health Snapshot"
TAB_COACH = "Coach Notes"
TAB_SETTINGS = "Settings"
TAB_MEMORY = "Coach Memory"
MEMORY_HEADERS = ["ID", "Category", "Fact", "Since"]

PLAN_HEADERS = ["Date", "Week", "Day", "Workout Type", "Description", "Target Distance (km)",
                "Target Duration (min)", "Target Intensity", "Notes", "Status"]
LOG_HEADERS = ["Date", "Activity ID", "Name", "Sport", "Indoor", "Distance (km)", "Moving Time (min)", "Elapsed Time (min)",
               "Avg Pace (min/km)", "Avg Speed (km/h)", "Avg HR", "Max HR", "Elevation Gain (m)", "Avg Cadence",
               "Suffer Score", "Start Lat", "Start Lng", "Temp (°C)", "Feels Like (°C)", "Humidity (%)",
               "Wind (km/h)", "Conditions", "Heat Adj Pace (min/km)", "Notes", "Strava Link"]
LOG_USER_COLUMNS = {"Notes"}  # never overwritten by sync
# (exact old header, insert-after column, new column) — applied only to untouched GridCoach-created logs
LOG_HEADER_MIGRATIONS = [
    ([h for h in LOG_HEADERS if h != "Indoor"], "Sport", "Indoor"),
]
HEALTH_HEADERS = ["Date", "Sleep (hrs)", "Sleep Quality (1-5)", "Resting HR", "Energy (1-5)", "Soreness (1-5)",
                  "Stress (1-5)", "Notes"]
WEATHER_HEADERS = ["Date", "Day", "Min Temp (°C)", "Max Temp (°C)", "Feels Like Max (°C)", "Precip Prob (%)",
                   "Precip (mm)", "Max Wind (km/h)", "Conditions", "Sunrise", "Sunset", "Training Tip"]
SNAPSHOT_HEADERS = ["Metric", "Value", "Updated"]
COACH_HEADERS = ["Date", "Source", "Activity", "Note"]
SETTINGS_HEADERS = ["Setting", "Value", "Notes"]
DEFAULT_SETTINGS = [
    ["Race Name", "", "e.g. City Half Marathon"],
    ["Race Date", "", "YYYY-MM-DD"],
    ["Race Distance", "", "e.g. Half Marathon / 21.1 km / Olympic tri"],
    ["Goal Time", "", "hh:mm:ss"],
    ["Home Latitude", "", "Used for the Weather tab forecast"],
    ["Home Longitude", "", ""],
    ["Units", "metric", "metric (km) — imperial display is on the roadmap"],
]

LAYOUT: dict[str, list[str]] = {
    TAB_PLAN: PLAN_HEADERS,
    TAB_LOG: LOG_HEADERS,
    TAB_HEALTH: HEALTH_HEADERS,
    TAB_WEATHER: WEATHER_HEADERS,
    TAB_SNAPSHOT: SNAPSHOT_HEADERS,
    TAB_COACH: COACH_HEADERS,
    TAB_SETTINGS: SETTINGS_HEADERS,
    TAB_MEMORY: MEMORY_HEADERS,
}

# Every tab is optional — the athlete picks what they want in their sheet. Features degrade gracefully.
TAB_DESCRIPTIONS: dict[str, str] = {
    TAB_LOG: "One row per Strava activity with weather + heat-adjusted pace. Required for Strava sync.",
    TAB_PLAN: "Structured training sessions the coach writes; Status auto-fills from your log.",
    TAB_SNAPSHOT: "Rolling 7d/28d load, ACWR, adherence, race countdown — recomputed after every sync.",
    TAB_HEALTH: "Sleep, resting HR, energy, soreness, stress and life notes for the coach to factor in.",
    TAB_WEATHER: "7-day forecast for your home location with a training tip per day.",
    TAB_COACH: "Dated observations the coach leaves after sessions and chats.",
    TAB_SETTINGS: "Race name/date/distance, goal time, home lat/lng.",
    TAB_MEMORY: "A visible mirror of what the coach remembers about you (facts persist in the backend either way).",
}


class SheetsError(Exception):
    pass


class MissingTabError(SheetsError):
    """A feature needs a tab the athlete hasn't opted into. Carries the tab name so UIs can offer to create it."""

    def __init__(self, tab: str, feature: str):
        self.tab = tab
        super().__init__(f"No '{tab}' tab — {feature} needs one. Create it (sidebar → Tabs, or ask the coach) and retry.")


def _a1_block(row: int, col: int, n_rows: int, n_cols: int) -> str:
    return f"{rowcol_to_a1(row, col)}:{rowcol_to_a1(row + n_rows - 1, col + n_cols - 1)}"


def _norm_cell(v: Any) -> str:
    """Compare a value we'd write against what the sheet shows: strip the text-forcing apostrophe, unify 60 / 60.0."""
    s = str(v).strip().lstrip("'")
    try:
        f = float(s.replace(",", ""))
        return str(int(f)) if f == int(f) else str(round(f, 6))
    except ValueError:
        return s


def date_order(dates: list[Any]) -> str:
    """Deterministic ordering verdict for a Date column: ascending / descending / unsorted (n out of order)."""
    from .analytics import parse_date
    parsed = [parse_date(d) for d in dates]
    parsed = [d for d in parsed if d]
    if len(parsed) < 2:
        return "n/a"
    asc_breaks = sum(1 for a, b in zip(parsed, parsed[1:]) if b < a)
    desc_breaks = sum(1 for a, b in zip(parsed, parsed[1:]) if b > a)
    if asc_breaks == 0:
        return "ascending (oldest first)"
    if desc_breaks == 0:
        return "descending (newest first)"
    return f"unsorted ({min(asc_breaks, desc_breaks)} rows out of order)"


def _is_number(s: str) -> bool:
    try:
        float(str(s).replace(",", ""))
        return True
    except ValueError:
        return False


CHART_TYPES = {"line": "LINE", "column": "COLUMN", "bar": "BAR", "area": "AREA", "scatter": "SCATTER", "pie": "PIE"}


def build_chart_request(sheet_id: int, chart_type: str, title: str, domain_range: str, series_ranges: list[str],
                        anchor_cell: str = "H2", x_title: str = "", y_title: str = "", width_px: int = 640, height_px: int = 380) -> dict[str, Any]:
    """Pure builder for a Sheets API `addChart` request (embedded chart anchored on the same tab)."""
    ct = CHART_TYPES.get(chart_type.lower())
    if not ct:
        raise ValueError(f"chart_type must be one of {sorted(CHART_TYPES)}")

    def src(a1: str) -> dict[str, Any]:
        return {"sourceRange": {"sources": [a1_range_to_grid_range(a1, sheet_id)]}}

    if ct == "PIE":
        spec: dict[str, Any] = {"title": title, "pieChart": {"legendPosition": "RIGHT_LEGEND", "domain": src(domain_range),
                                                             "series": src(series_ranges[0]), "pieHole": 0.35}}
    else:
        spec = {"title": title, "basicChart": {
            "chartType": ct, "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
            "axis": [{"position": "BOTTOM_AXIS", "title": x_title}, {"position": "LEFT_AXIS", "title": y_title}],
            "domains": [{"domain": src(domain_range)}],
            "series": [{"series": src(s), "targetAxis": "LEFT_AXIS"} for s in series_ranges],
        }}
        if ct == "LINE":
            spec["basicChart"]["lineSmoothing"] = False
    anchor = a1_range_to_grid_range(anchor_cell, sheet_id)
    return {"addChart": {"chart": {"spec": spec, "position": {"overlayPosition": {
        "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor.get("startRowIndex", 0), "columnIndex": anchor.get("startColumnIndex", 0)},
        "widthPixels": width_px, "heightPixels": height_px}}}}}


class SheetsService:
    def __init__(self, service_account_file: str):
        self._file = service_account_file
        self._gc: gspread.Client | None = None
        self._lock = threading.Lock()

    # ---- client / open -------------------------------------------------

    @property
    def gc(self) -> gspread.Client:
        with self._lock:
            if self._gc is None:
                try:
                    self._gc = gspread.service_account(filename=self._file)
                except FileNotFoundError as e:
                    raise SheetsError(
                        f"Service account file not found at {self._file}. "
                        "Set GOOGLE_SERVICE_ACCOUNT_FILE in .env."
                    ) from e
            return self._gc

    def service_account_email(self) -> str | None:
        gc = self.gc
        for creds in (getattr(gc, "auth", None), getattr(getattr(gc, "http_client", None), "auth", None)):
            email = getattr(creds, "service_account_email", None)
            if email:
                return email
        return None

    def open(self, spreadsheet_id: str) -> gspread.Spreadsheet:
        try:
            return self.gc.open_by_key(spreadsheet_id)
        except gspread.exceptions.APIError as e:
            email = self.service_account_email()
            raise SheetsError(
                f"Cannot open spreadsheet {spreadsheet_id}: {e}. "
                f"Share the sheet with the service account{f' ({email})' if email else ''} as Editor."
            ) from e

    def _ws(self, sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
        try:
            return sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound as e:
            raise SheetsError(f"Tab '{title}' not found. Available: {[w.title for w in sh.worksheets()]}") from e

    # ---- layout ------------------------------------------------------------

    def tab_titles(self, spreadsheet_id: str) -> list[str]:
        return [w.title for w in self.open(spreadsheet_id).worksheets()]

    def has_tab(self, spreadsheet_id: str, title: str) -> bool:
        return title in self.tab_titles(spreadsheet_id)

    def create_tab(self, spreadsheet_id: str, title: str, headers: list[str] | None = None) -> dict[str, Any]:
        """Create one tab (idempotent). Standard GridCoach tabs get their canonical headers; custom tabs get `headers`.

        Tabs are opt-in: the athlete decides what lives in their sheet, so nothing calls this unasked.
        """
        sh = self.open(spreadsheet_id)
        existing = {w.title: w for w in sh.worksheets()}
        headers = LAYOUT.get(title) or headers or []
        if title in existing:
            ws = existing[title]
            if headers and not any(ws.row_values(1)):
                ws.update(values=[headers], range_name="A1", value_input_option=ValueInputOption.user_entered)
            return {"title": title, "created": False, "headers": headers}
        ws = sh.add_worksheet(title=title, rows=200 if title in (TAB_SNAPSHOT, TAB_SETTINGS) else 1000, cols=max(len(headers), 12))
        if headers:
            ws.update(values=[headers], range_name="A1", value_input_option=ValueInputOption.user_entered)
            ws.format(f"A1:{rowcol_to_a1(1, len(headers))}", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        if title == TAB_SETTINGS:
            ws.update(values=DEFAULT_SETTINGS, range_name="A2", value_input_option=ValueInputOption.user_entered)
        return {"title": title, "created": True, "headers": headers}

    def ensure_layout(self, spreadsheet_id: str, tabs: list[str] | None = None) -> dict[str, Any]:
        """Create only the requested GridCoach tabs (idempotent). With no `tabs`, just report state."""
        created = [t for t in (tabs or []) if t in LAYOUT and self.create_tab(spreadsheet_id, t)["created"]]
        titles = self.tab_titles(spreadsheet_id)
        return {"created": created, "tabs": titles, "missing": [t for t in LAYOUT if t not in titles]}

    @staticmethod
    def tab_catalog() -> list[dict[str, Any]]:
        return [{"name": t, "description": TAB_DESCRIPTIONS[t], "headers": h} for t, h in LAYOUT.items()]

    # ---- generic ops (agent tools) ----------------------------------------

    def list_sheets(self, spreadsheet_id: str) -> list[dict[str, Any]]:
        sh = self.open(spreadsheet_id)
        out = []
        for ws in sh.worksheets():
            headers = ws.row_values(1)
            out.append({"title": ws.title, "rows": ws.row_count, "cols": ws.col_count, "headers": headers[:40]})
        return out

    def read_range(self, spreadsheet_id: str, sheet: str, a1: str | None = None, max_rows: int = 200, max_cols: int = 40) -> dict[str, Any]:
        """Read a tab. When the first row looks like a header, return header-keyed records with sheet row numbers —
        far more reliable for the model than a positional 2-D array. Empty cells are omitted from records."""
        ws = self._ws(self.open(spreadsheet_id), sheet)
        values = [list(r) for r in (ws.get(a1) if a1 else ws.get_all_values())]
        m = re.search(r"[A-Za-z]+(\d+)", (a1 or "").split(":")[0])
        first_row = int(m.group(1)) if m else 1
        total = len(values)
        truncated = total > max_rows or any(len(r) > max_cols for r in values)
        values = [r[:max_cols] for r in values[:max_rows]]
        out: dict[str, Any] = {"sheet": sheet, "range": a1 or "all", "row_count": total, "truncated": truncated}
        header = [str(h).strip() for h in values[0]] if values else []
        named = [h for h in header if h]
        looks_like_header = bool(named) and len(named) == len(set(named)) and not any(_is_number(h) for h in named)
        if looks_like_header and len(values) > 1:
            records = []
            for i, r in enumerate(values[1:], start=first_row + 1):
                if not any(str(c).strip() for c in r):
                    continue
                rec: dict[str, Any] = {"row": i}
                rec.update({h: r[j] for j, h in enumerate(header) if h and j < len(r) and str(r[j]).strip() != ""})
                records.append(rec)
            out.update(headers=header, records=records, note="row = sheet row number; a missing key means that cell is empty")
            if "Date" in header:
                out["date_order"] = date_order([rec.get("Date") for rec in records])
        else:
            out["values"] = values
        return out

    # ---- data movement (server-side, so the model never re-types rows) --------

    def _last_cell(self, ws: gspread.Worksheet, values: list[list[Any]]) -> tuple[int, int]:
        n_rows = len(values)
        n_cols = max((len(r) for r in values), default=1)
        return n_rows, n_cols

    def sort_tab(self, spreadsheet_id: str, sheet: str, by_column: str, order: str = "asc", has_header: bool = True) -> dict[str, Any]:
        """Sort a tab in place by a column (header name or letter). The header row stays put."""
        ws = self._ws(self.open(spreadsheet_id), sheet)
        values = ws.get_all_values()
        n_rows, n_cols = self._last_cell(ws, values)
        header = [h.strip() for h in values[0]] if values else []
        if by_column in header:
            col = header.index(by_column) + 1
        elif re.fullmatch(r"[A-Za-z]{1,2}", by_column):
            col = a1_range_to_grid_range(f"{by_column.upper()}1")["startColumnIndex"] + 1
        else:
            raise SheetsError(f"Column '{by_column}' not found in '{sheet}'. Headers: {header}")
        first = 2 if has_header else 1
        if n_rows <= first:
            return {"sheet": sheet, "sorted_by": by_column, "order": order, "rows": 0}
        rng = _a1_block(first, 1, n_rows - first + 1, n_cols)
        ws.sort((col, "des" if order.lower().startswith("d") else "asc"), range=rng)
        return {"sheet": sheet, "sorted_by": by_column, "order": "desc" if order.lower().startswith("d") else "asc", "rows": n_rows - first + 1, "range": rng}

    def copy_range(self, spreadsheet_id: str, from_sheet: str, a1: str | None, to_sheet: str, to_cell: str = "A1") -> dict[str, Any]:
        """Copy values (formulas preserved) from one tab to another in a single write."""
        sh = self.open(spreadsheet_id)
        src = self._ws(sh, from_sheet)
        dst = self._ws(sh, to_sheet)
        values = [list(r) for r in (src.get(a1, value_render_option="FORMULA") if a1 else src.get_all_values(value_render_option="FORMULA"))]
        if not values:
            return {"copied_rows": 0}
        dst.update(values=values, range_name=to_cell, value_input_option=ValueInputOption.user_entered)
        m = re.match(r"([A-Za-z]+)(\d+)", to_cell)
        start_row = int(m.group(2)) if m else 1
        start_col = a1_range_to_grid_range(to_cell)["startColumnIndex"] + 1
        width = max(len(r) for r in values)
        return {"from": f"{from_sheet}!{a1 or 'all'}", "to": to_sheet, "range": _a1_block(start_row, start_col, len(values), width), "copied_rows": len(values)}

    def raw_values(self, spreadsheet_id: str, sheet: str) -> list[list[Any]]:
        return [list(r) for r in self._ws(self.open(spreadsheet_id), sheet).get_all_values()]

    def block_has_values(self, spreadsheet_id: str, sheet: str, a1: str) -> bool:
        """True if the tab has any non-empty cell at/after the given top-left cell (used to gate overwrites)."""
        ws = self._ws(self.open(spreadsheet_id), sheet)
        values = ws.get_all_values()
        g = a1_range_to_grid_range(a1)
        r0, c0 = g.get("startRowIndex", 0), g.get("startColumnIndex", 0)
        return any(str(c).strip() for row in values[r0:] for c in row[c0:])

    def write_memory_tab(self, spreadsheet_id: str, memories: list[dict[str, Any]]) -> None:
        ws = self._ws(self.open(spreadsheet_id), TAB_MEMORY)
        ws.batch_clear([f"A2:D{max(len(memories) + 20, 50)}"])
        if memories:
            rows = [[m["id"], m["category"], m["fact"], datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d")] for m in memories]
            ws.update(values=rows, range_name="A2", value_input_option=ValueInputOption.user_entered)

    def delete_rows(self, spreadsheet_id: str, sheet: str, rows: list[int]) -> dict[str, Any]:
        """Delete specific sheet rows (1-based). Deletes bottom-up so indices stay valid. Never deletes the header row."""
        ws = self._ws(self.open(spreadsheet_id), sheet)
        targets = sorted({int(r) for r in rows if int(r) >= 2}, reverse=True)
        for r in targets:
            ws.delete_rows(r)
        return {"sheet": sheet, "deleted_rows": sorted(targets)}

    def append_records(self, spreadsheet_id: str, sheet: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Append rows given as {header: value} — mapped by header name so columns can never misalign."""
        ws = self._ws(self.open(spreadsheet_id), sheet)
        header = [h.strip() for h in ws.row_values(1)]
        if not header:
            raise SheetsError(f"Tab '{sheet}' has no header row; use append_rows with plain arrays instead.")
        unknown = sorted({k for rec in records for k in rec} - set(header))
        rows = [[rec.get(h, "") for h in header] for rec in records]
        res = self.append_rows(spreadsheet_id, sheet, rows)
        if unknown:
            res["ignored_keys"] = unknown
        return res

    def write_range(self, spreadsheet_id: str, sheet: str, a1: str, values: list[list[Any]]) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), sheet)
        ws.update(values=values, range_name=a1, value_input_option=ValueInputOption.user_entered)
        return {"sheet": sheet, "range": a1, "rows_written": len(values)}

    def append_rows(self, spreadsheet_id: str, sheet: str, rows: list[list[Any]]) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), sheet)
        first_free = len(ws.get_all_values()) + 1
        ws.append_rows(rows, value_input_option=ValueInputOption.user_entered, table_range="A1")
        width = max((len(r) for r in rows), default=1)
        return {"sheet": sheet, "range": _a1_block(first_free, 1, len(rows), width), "rows_appended": len(rows)}

    def read_tab_dicts(self, spreadsheet_id: str, sheet: str, with_row: bool = False) -> list[dict[str, Any]]:
        from .analytics import rows_to_dicts
        sh = self.open(spreadsheet_id)
        try:
            ws = sh.worksheet(sheet)
        except gspread.exceptions.WorksheetNotFound:
            return []
        return rows_to_dicts(ws.get_all_values(), with_row=with_row)

    def get_settings(self, spreadsheet_id: str) -> dict[str, str]:
        return {str(r.get("Setting", "")).strip(): str(r.get("Value", "")).strip() for r in self.read_tab_dicts(spreadsheet_id, TAB_SETTINGS)}

    # ---- GridCoach-specific writers -----------------------------------------

    @staticmethod
    def _migrate_log_header(ws: Any, header: list[str]) -> list[str]:
        """Bring a GridCoach-created log up to the current schema — only when its header is exactly an older
        GridCoach version (so athlete-customised layouts are never touched). Returns the columns added."""
        for old, insert_after, new_col in LOG_HEADER_MIGRATIONS:
            if header == old:
                col = header.index(insert_after) + 2  # 1-based, after the anchor column
                ws.insert_cols([[new_col]], col=col, value_input_option=ValueInputOption.user_entered)
                try:
                    ws.format(rowcol_to_a1(1, col), {"textFormat": {"bold": True}})
                except Exception:
                    pass
                return [new_col]
        return []

    def upsert_log_rows(self, spreadsheet_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert Training Log rows keyed by 'Activity ID'. User-owned columns are preserved."""
        sh = self.open(spreadsheet_id)
        ws = self._ws(sh, TAB_LOG)
        values = ws.get_all_values()
        header = [h.strip() for h in values[0]] if values else []
        if "Activity ID" not in header:
            raise SheetsError(f"'{TAB_LOG}' needs an 'Activity ID' header column.")
        migrated = self._migrate_log_header(ws, header)
        if migrated:
            values = ws.get_all_values()
            header = [h.strip() for h in values[0]]
        col_idx = {h: i for i, h in enumerate(header)}
        id_col = col_idx["Activity ID"]
        # index into `values` (0-based; sheet row = index + 1)
        existing_idx_for_id = {str(r[id_col]).strip(): i for i, r in enumerate(values) if i > 0 and len(r) > id_col and str(r[id_col]).strip()}

        updates: list[dict[str, Any]] = []
        appends: list[list[Any]] = []
        touched: list[str] = []
        for row in rows:
            aid = str(row.get("Activity ID", "")).strip()
            if aid and aid in existing_idx_for_id:
                idx = existing_idx_for_id[aid]
                current = list(values[idx]) + [""] * (len(header) - len(values[idx]))
                new = list(current)
                for h, i in col_idx.items():
                    if h in LOG_USER_COLUMNS or h not in row:
                        continue
                    new[i] = row[h]
                if [_norm_cell(v) for v in new] != [_norm_cell(v) for v in current]:
                    rng = _a1_block(idx + 1, 1, 1, len(header))
                    updates.append({"range": rng, "values": [new]})
                    touched.append(rng)
            else:
                appends.append([row.get(h, "") for h in header])
        if updates:
            ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        if appends:
            appends.sort(key=lambda r: str(r[col_idx.get("Date", 0)]))
            first_free = len(values) + 1
            ws.append_rows(appends, value_input_option=ValueInputOption.user_entered, table_range="A1")
            touched.append(_a1_block(first_free, 1, len(appends), len(header)))
        return {"updated": len(updates), "appended": len(appends), "touched": touched, "added_columns": migrated}

    def sort_log_by_date(self, spreadsheet_id: str) -> None:
        try:
            self.sort_tab(spreadsheet_id, TAB_LOG, "Date", "asc")
        except SheetsError:
            pass  # no Date column — the athlete's layout, leave it

    def write_snapshot(self, spreadsheet_id: str, metrics: list[tuple[str, str]]) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), TAB_SNAPSHOT)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        values = [[m, v, stamp] for m, v in metrics]
        ws.batch_clear([f"A2:C{max(len(values) + 30, 60)}"])
        ws.update(values=values, range_name="A2", value_input_option=ValueInputOption.user_entered)
        return {"sheet": TAB_SNAPSHOT, "range": _a1_block(2, 1, len(values), 3), "metrics": len(values)}

    def write_plan_statuses(self, spreadsheet_id: str, statuses: list[str]) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), TAB_PLAN)
        header = [h.strip() for h in ws.row_values(1)]
        if "Status" not in header or not statuses:
            return {"written": 0}
        col = header.index("Status") + 1
        rng = _a1_block(2, col, len(statuses), 1)
        ws.update(values=[[s] for s in statuses], range_name=rng, value_input_option=ValueInputOption.user_entered)
        return {"written": len(statuses), "range": rng}

    def write_plan(self, spreadsheet_id: str, rows: list[dict[str, Any]], replace: bool) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), TAB_PLAN)
        header = [h.strip() for h in ws.row_values(1)] or PLAN_HEADERS
        values = [[row.get(h, "") for h in header] for row in rows]
        if replace:
            ws.batch_clear([f"A2:{rowcol_to_a1(ws.row_count, len(header))}"])
            start = 2
            ws.update(values=values, range_name="A2", value_input_option=ValueInputOption.user_entered)
        else:
            start = len(ws.get_all_values()) + 1
            ws.append_rows(values, value_input_option=ValueInputOption.user_entered, table_range="A1")
        return {"sheet": TAB_PLAN, "range": _a1_block(start, 1, len(values), len(header)), "rows": len(values), "replaced": replace}

    def write_weather(self, spreadsheet_id: str, rows: list[list[Any]]) -> dict[str, Any]:
        ws = self._ws(self.open(spreadsheet_id), TAB_WEATHER)
        ws.batch_clear([f"A2:L{max(len(rows) + 20, 40)}"])
        ws.update(values=rows, range_name="A2", value_input_option=ValueInputOption.user_entered)
        return {"sheet": TAB_WEATHER, "range": _a1_block(2, 1, len(rows), len(WEATHER_HEADERS)), "days": len(rows)}

    def create_chart(self, spreadsheet_id: str, sheet: str, chart_type: str, title: str, domain_range: str,
                     series_ranges: list[str], anchor_cell: str = "H2", x_title: str = "", y_title: str = "") -> dict[str, Any]:
        sh = self.open(spreadsheet_id)
        ws = self._ws(sh, sheet)
        req = build_chart_request(ws.id, chart_type, title, domain_range, series_ranges, anchor_cell, x_title, y_title)
        resp = sh.batch_update({"requests": [req]})
        chart_id = ((resp.get("replies") or [{}])[0].get("addChart") or {}).get("chart", {}).get("chartId")
        return {"sheet": sheet, "chart_id": chart_id, "chart_type": chart_type, "title": title, "anchor": anchor_cell,
                "domain": domain_range, "series": series_ranges}

    def add_coach_note(self, spreadsheet_id: str, source: str, activity: str, note: str) -> dict[str, Any]:
        return self.append_rows(spreadsheet_id, TAB_COACH, [[datetime.now().strftime("%Y-%m-%d %H:%M"), source, activity, note]])
