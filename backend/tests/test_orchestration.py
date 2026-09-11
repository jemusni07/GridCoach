"""Jobs/progress events, pending confirmations, memory, and the pandas sandbox."""
import asyncio
from types import SimpleNamespace

import pytest

from app.agent.tools import ToolRunner, describe_call, summarize_result
from app.services.jobs import JobManager, PendingActions
from app.services.sandbox import check_code, run_analysis
from app.services.state import Store


async def test_job_manager_streams_events_and_result():
    jm = JobManager()
    job = jm.create("sid", "chat")
    emit = jm.emitter(job.id)

    async def work():
        emit("tool_start", "Reading Training Log", tool="read_range")
        await asyncio.sleep(0.01)
        emit("tool_end", "55 rows", tool="read_range", ok=True)
        return {"reply": "hi"}

    jm.start(job, work())
    await asyncio.sleep(0.05)
    snap = jm.snapshot(job.id, after=0)
    assert snap["status"] == "done" and snap["result"] == {"reply": "hi"}
    assert [e["type"] for e in snap["events"]] == ["tool_start", "tool_end"] and snap["next"] == 2
    assert jm.snapshot(job.id, after=2)["events"] == []


async def test_job_failure_is_reported_not_lost():
    jm = JobManager()
    job = jm.create("sid", "chat")

    async def boom():
        raise RuntimeError("nope")

    jm.start(job, boom())
    await asyncio.sleep(0.02)
    assert jm.snapshot(job.id)["status"] == "error" and "nope" in jm.snapshot(job.id)["error"]


class FakeSheets:
    def __init__(self):
        self.deleted = []

    def delete_rows(self, sid, sheet, rows):
        self.deleted.append(rows)
        return {"sheet": sheet, "deleted_rows": rows}

    def read_tab_dicts(self, sid, sheet, with_row=False):
        return [{"Date": "2026-01-01"}]


async def test_destructive_tool_is_parked_until_confirmed():
    pending = PendingActions()
    events = []
    runner = ToolRunner("sid", FakeSheets(), pipeline=None, weather=None, on_event=lambda t, x, **k: events.append((t, x)), pending=pending)
    res = await runner.run("delete_rows", {"sheet": "Training Log", "rows": [62, 52]})
    assert "pending_confirmation" in res and runner.sheets.deleted == []
    assert runner.pending_actions[0]["description"] == "Delete rows [52, 62] from 'Training Log'"
    assert events[-1] == ("tool_end", "needs your confirmation")
    # confirmed → executes
    item = pending.pop(runner.pending_actions[0]["action_id"], "sid")
    confirmed = ToolRunner("sid", runner.sheets, pipeline=None, weather=None, pending=pending, bypass_confirm=True)
    out = await confirmed.run(item["tool"], item["args"])
    assert out["deleted_rows"] == [62, 52] and runner.sheets.deleted == [[62, 52]]
    # wrong spreadsheet can't pop someone else's action
    assert pending.pop("nope", "sid") is None


async def test_plan_replace_needs_confirmation_only_when_plan_exists():
    pending = PendingActions()
    runner = ToolRunner("sid", FakeSheets(), pipeline=None, weather=None, pending=pending)
    assert (await runner._needs_confirmation("write_training_plan", {"rows": [{}], "replace": True})).startswith("Replace the existing Training Plan (1 sessions)")
    assert await runner._needs_confirmation("write_training_plan", {"rows": [{}], "replace": False}) is None


def test_progress_descriptions():
    assert describe_call("query_log", {"sports": ["Run"], "indoor": False}) == "Querying log (Run, outdoor)"
    assert describe_call("sort_tab", {"sheet": "Training Log", "by_column": "Date", "order": "desc"}) == "Sorting Training Log by Date (desc)"
    assert summarize_result("sync_strava", {"fetched": 54, "appended": 2, "updated": 1}) == "54 activities · 2 new · 1 updated"
    assert summarize_result("read_range", {"records": [1, 2], "date_order": "ascending (oldest first)"}) == "2 rows · ascending (oldest first)"
    assert summarize_result("x", {"error": "boom"}) == "✗ boom"


def test_memory_store_roundtrip(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    a = s.add_memory("sid", "profile", "Lives in Dubai")
    dup = s.add_memory("sid", "profile", "lives in dubai")
    assert dup["id"] == a["id"] and dup.get("duplicate")
    b = s.add_memory("sid", "race", "Half marathon 2026-11-15")
    assert [m["fact"] for m in s.list_memories("sid")] == ["Lives in Dubai", "Half marathon 2026-11-15"]
    assert s.list_memories("other") == []
    assert s.forget_memory("sid", a["id"]) and not s.forget_memory("sid", a["id"])
    assert [m["id"] for m in s.list_memories("sid")] == [b["id"]]


def test_sandbox_rejects_dangerous_code():
    for bad in ["import os", "open('x')", "from subprocess import run", "df.to_csv('x')", "x.__class__", "__import__('os')"]:
        with pytest.raises(ValueError):
            check_code(bad)
    check_code("import pandas as pd\nresult = log[log['Sport']=='Run']['Distance (km)'].sum()")


async def test_sandbox_runs_pandas_with_coercion():
    tabs = {"Training Log": [
        ["Date", "Sport", "Indoor", "Distance (km)", "Avg Pace (min/km)", "Feels Like (°C)"],
        ["2026-09-02", "Run", "", "5.01", "5:30", "41.7"],
        ["2026-09-03", "Run", "Y", "8.06", "7:35", ""],
        ["2026-09-09", "Run", "", "4.81", "5:09", "41.9"],
        ["2026-09-08", "Workout", "Y", "0", "", ""],
    ]}
    code = """
runs = log[(log['Sport'] == 'Run') & (log['Indoor'].isna())]
hot = runs[runs['Feels Like (°C)'] >= 30]
print('outdoor runs:', len(runs))
result = {'hot_mean_pace_min': round(hot['Avg Pace (min/km)_min'].mean(), 2), 'total_km': float(runs['Distance (km)'].sum()),
          'by_month': runs.groupby(runs['Date'].dt.to_period('M').astype(str))['Distance (km)'].sum()}
"""
    out = await run_analysis(tabs, code)
    assert "error" not in out, out
    assert out["stdout"].strip() == "outdoor runs: 2"
    assert out["result"]["hot_mean_pace_min"] == 5.32 and out["result"]["total_km"] == 9.82
    assert out["result"]["by_month"]["values"] == {"2026-09": 9.82}
    assert "row" in out["columns"]["Training Log"] and "Avg Pace (min/km)_min" in out["columns"]["Training Log"]


async def test_sandbox_returns_traceback_for_model_to_fix():
    out = await run_analysis({"Training Log": [["Date", "x"], ["2026-01-01", "1"]]}, "result = log['nope'].sum()")
    assert out["error"].startswith("KeyError") and "traceback" in out
