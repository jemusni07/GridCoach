"""Tabs are opt-in: recompute must only *write* to tabs that exist, and sync must refuse without a log tab."""
from types import SimpleNamespace

import pytest

from app.services.pipeline import Pipeline
from app.services.sheets import TAB_LOG, TAB_PLAN, TAB_SNAPSHOT, SheetsError
from app.services.state import Store


class FakeSheets:
    def __init__(self, tabs, plan_rows=None, log_rows=None):
        self.tabs = list(tabs)
        self.plan_rows = plan_rows or []
        self.log_rows = log_rows or []
        self.snapshot_writes = 0
        self.status_writes = []

    def tab_titles(self, sid):
        return self.tabs

    def has_tab(self, sid, title):
        return title in self.tabs

    def read_tab_dicts(self, sid, sheet):
        if sheet not in self.tabs:
            return []
        return {TAB_PLAN: self.plan_rows, TAB_LOG: self.log_rows}.get(sheet, [])

    def get_settings(self, sid):
        return {}

    def write_snapshot(self, sid, metrics):
        self.snapshot_writes += 1
        return {"sheet": TAB_SNAPSHOT, "range": "A2:C20"}

    def write_plan_statuses(self, sid, statuses):
        self.status_writes.append(statuses)
        return {"written": len(statuses)}


def make(tabs, **kw):
    settings = SimpleNamespace(default_lookback_days=90)
    sheets = FakeSheets(tabs, **kw)
    return Pipeline(settings, Store(":memory:"), sheets, strava=None, weather=None), sheets


async def test_recompute_without_optional_tabs_computes_but_does_not_write():
    p, sheets = make([TAB_LOG], log_rows=[{"Date": "2026-09-10", "Sport": "Run", "Distance (km)": "10", "Moving Time (min)": "55"}])
    out = await p.recompute("sid")
    assert out["snapshot_written"] is False and out["snapshot_range"] is None
    assert out["plan_rows_reconciled"] == 0
    assert out["metrics"]["Distance 7d (km)"] or "Distance 7d (km)" in out["metrics"]  # metrics still available for chat
    assert sheets.snapshot_writes == 0 and sheets.status_writes == []


async def test_recompute_writes_only_to_present_tabs():
    p, sheets = make([TAB_LOG, TAB_PLAN, TAB_SNAPSHOT], plan_rows=[{"Date": "2026-01-01", "Workout Type": "Easy"}])
    out = await p.recompute("sid")
    assert out["snapshot_written"] is True and out["snapshot_range"] == f"{TAB_SNAPSHOT}!A2:C20"
    assert out["plan_rows_reconciled"] == 1 and sheets.status_writes == [["Missed"]]
    assert sheets.snapshot_writes == 1


async def test_sync_refuses_without_log_tab():
    p, _ = make([TAB_PLAN])
    with pytest.raises(SheetsError, match="Training Log"):
        await p.sync("sid")


async def test_webhook_caches_but_skips_sheet_without_log_tab():
    p, _ = make([])

    class FakeStrava:
        async def get_activity(self, sid, aid):
            return {"id": aid, "name": "Run", "sport_type": "Run", "distance": 5000, "moving_time": 1500, "start_date": "2026-09-10T05:00:00Z"}

    p.strava = FakeStrava()
    out = await p.process_activity("sid", 123)
    assert out["skipped"] and out["appended"] == 0
    assert p.store.cache_info("sid")["count"] == 1  # full-history cache stays current even without a log tab
