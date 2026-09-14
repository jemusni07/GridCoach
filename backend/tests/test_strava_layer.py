"""Strava cache (backfill → incremental), transcript store, stats/gear simplification."""
import time
from datetime import date

from app.services import analytics as an
from app.services.state import Store
from app.services.strava_cache import StravaCache, simplify_gear, simplify_stats


def act(i, day, sport="Run", km=10.0, trainer=False):
    return {"id": i, "name": f"Act {i}", "sport_type": sport, "distance": km * 1000, "moving_time": 3000, "elapsed_time": 3100,
            "start_date": f"{day}T05:00:00Z", "start_date_local": f"{day}T09:00:00Z", "average_speed": 3.0, "trainer": trainer,
            "kudos_count": i, "gear_id": "g1"}


class FakeStrava:
    def __init__(self, acts):
        self.acts = acts
        self.calls = []

    async def list_activities(self, sid, after_ts=None, before_ts=None, max_items=400, on_page=None):
        self.calls.append(after_ts)
        out = [a for a in self.acts if after_ts is None or time.mktime(time.strptime(a["start_date"], "%Y-%m-%dT%H:%M:%SZ")) >= after_ts]
        if on_page:
            on_page(len(out))
        return out[:max_items]


async def test_cache_backfills_once_then_incremental(tmp_path):
    store = Store(str(tmp_path / "c.db"))
    strava = FakeStrava([act(1, "2024-06-01", "Ride", 40), act(2, "2025-08-10"), act(3, "2026-09-09", trainer=True)])
    cache = StravaCache(store, strava)
    events = []
    info = await cache.ensure("sid", progress=lambda t, x, **k: events.append(x))
    assert info["mode"] == "backfill" and info["fetched"] == 3 and info["oldest"] == "2024-06-01" and info["newest"] == "2026-09-09"
    assert strava.calls == [None] and any("Backfilling" in e for e in events)
    # fresh → no API call
    info = await cache.ensure("sid")
    assert info["mode"] == "fresh" and strava.calls == [None]
    # stale → incremental with overlap
    store.mark_cache("sid", synced_at=int(time.time()) - 3600)
    strava.acts.append(act(4, date.today().isoformat()))  # inside the 3-day overlap on any day the test runs
    info = await cache.ensure("sid")
    assert info["mode"] == "incremental" and info["count"] == 4 and len(strava.calls) == 2 and strava.calls[1] is not None

    rows = cache.rows("sid")
    assert [r["Activity ID"] for r in rows] == ["1", "2", "3", "4"] and rows[2]["Indoor"] == "Y" and rows[0]["Kudos"] == 1
    q = an.filter_log(rows, sports=["Run"], indoor=False, limit=5)
    assert q["matched"] == 2 and q["aggregates"]["by_month"] == {"2025-08": {"count": 1, "km": 10.0, "min": 50.0}, "2026-09": {"count": 1, "km": 10.0, "min": 50.0}}
    table = cache.table("sid")
    assert table[0][:4] == ["Date", "Activity ID", "Name", "Sport"] and "Kudos" in table[0] and len(table) == 5
    recent = {a["id"] for a in cache.activities("sid", days_back=30)}
    assert 4 in recent and not {1, 2} & recent  # window filter drops the 2024/2025 activities


def test_transcript_store_roundtrip(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.add_message("sid", "user", "hi")
    s.add_message("sid", "coach", "hello", {"tool_log": [{"tool": "query_log"}], "pending_actions": [{"action_id": "a1"}]})
    s.add_message("other", "user", "nope")
    msgs = s.list_messages("sid")
    assert [m["role"] for m in msgs] == ["user", "coach"] and msgs[1]["meta"]["pending_actions"][0]["action_id"] == "a1"
    s.set_last_job("sid", "job9")
    assert s.get_tenant("sid")["last_job_id"] == "job9"
    s.clear_messages("sid")
    assert s.list_messages("sid") == [] and len(s.list_messages("other")) == 1


def test_simplify_stats_and_gear():
    stats = {"biggest_ride_distance": 101234.0, "ytd_run_totals": {"count": 45, "distance": 251200, "moving_time": 109560, "elevation_gain": 812.4},
             "all_ride_totals": {"count": 3, "distance": 150000, "moving_time": 20000, "elevation_gain": 900}, "recent_swim_totals": {"count": 0, "distance": 0, "moving_time": 0, "elevation_gain": 0}}
    out = simplify_stats(stats)
    assert out["year_to_date"]["run"] == {"count": 45, "distance_km": 251.2, "moving_time_h": 30.4, "elevation_m": 812}
    assert out["all_time"]["ride"]["distance_km"] == 150.0 and out["last_4_weeks"]["swim"]["count"] == 0
    assert out["biggest_ride_distance"] == 101.2
    gear = simplify_gear({"shoes": [{"id": "g1", "name": "Pegasus", "distance": 612345, "primary": True}], "bikes": []})
    assert gear["shoes"][0] == {"name": "Pegasus", "distance_km": 612.3, "primary": True, "retired": False, "id": "g1"}
