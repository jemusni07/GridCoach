from datetime import date

from app.services import analytics as an
from app.services.pipeline import activity_to_row


def test_parse_helpers():
    assert an.parse_date("2026-09-01") == date(2026, 9, 1)
    assert an.parse_date("9/1/2026") == date(2026, 9, 1)
    assert an.parse_date("2026-09-01T06:30:00Z") == date(2026, 9, 1)
    assert an.parse_date("") is None
    assert an.to_float("5:30") == 5.5
    assert an.to_float("1,234.5") == 1234.5
    assert an.fmt_pace(5.5) == "5:30"


def test_heat_adjustment():
    assert an.heat_adjustment_pct(10) == 0
    assert abs(an.heat_adjustment_pct(25) - 0.10) < 1e-9
    assert an.heat_adjustment_pct(60) == 0.25
    assert abs(an.heat_adjusted_pace(5.5, 25) - 5.0) < 1e-9


def _log(today: date):
    def row(days_ago, km, minutes, sport="Run", hr=150):
        d = date.fromordinal(today.toordinal() - days_ago).isoformat()
        return {"Date": d, "Sport": sport, "Distance (km)": km, "Moving Time (min)": minutes, "Avg HR": hr}
    return [row(1, 10, 55), row(3, 8, 45), row(5, 21.1, 120, "Run", 158), row(6, 0, 40, "WeightTraining", 110),
            row(10, 12, 65), row(14, 10, 55), row(20, 15, 80), row(26, 9, 50), row(40, 30, 160)]


def test_snapshot_and_reconcile():
    today = date(2026, 9, 11)
    log = _log(today)
    plan = [
        {"Date": "2026-09-10", "Workout Type": "Easy", "Status": ""},
        {"Date": "2026-09-09", "Workout Type": "Tempo", "Status": ""},
        {"Date": "2026-09-08", "Workout Type": "Rest"},
        {"Date": "2026-09-11", "Workout Type": "Easy"},
        {"Date": "2026-09-13", "Workout Type": "Long"},
    ]
    statuses = an.reconcile_plan(plan, log, today)
    assert statuses[0].startswith("Done (Run 10.0 km")
    assert statuses[1] == "Missed"
    assert statuses[2] == "Rest"
    assert statuses[3] == "Today"
    assert statuses[4] == "Upcoming"

    snap = dict(an.compute_snapshot(log, plan, [{"Date": "2026-09-10", "Sleep (hrs)": "6.5"}], {"Race Date": "2026-11-15", "Race Name": "City Half"}, today))
    assert snap["Activities (7d)"] == "4"
    assert snap["Distance 7d (km)"] == "39.1"
    assert snap["Distance 28d (km)"] == "85.1"
    assert snap["ACWR (distance)"] == f"{39.1 / (85.1 / 4):.2f}"
    assert "Run" in snap["Sport mix 7d"] and "WeightTraining" in snap["Sport mix 7d"]
    assert snap["Plan adherence 7d"].startswith("1/3")  # 10th done, 9th missed, 11th (today) not done
    assert snap["Planned sessions next 7d"] == "1"
    assert snap["Sleep avg 7d (hrs)"] == "6.5"
    assert snap["Race"].startswith("City Half — 2026-11-15 (65 days")
    assert snap["Longest session 28d"].startswith("21.1 km Run")


def test_analyze_streams_decoupling():
    n = 600
    t = list(range(n))
    d = [i * 3.0 for i in range(n)]  # steady 3 m/s
    hr = [140 + (10 if i >= n // 2 else 0) for i in range(n)]  # HR drifts up in 2nd half
    res = an.analyze_streams({"time": {"data": t}, "distance": {"data": d}, "heartrate": {"data": hr}, "cadence": {"data": [85] * n}})
    assert res["cardiac_drift_pct"] > 6
    assert res["aerobic_decoupling_pct"] > 6
    assert res["avg_cadence_spm"] == 170


def test_activity_to_row():
    a = {"id": 123, "name": "Morning Run", "sport_type": "Run", "distance": 10000, "moving_time": 3300, "elapsed_time": 3400,
         "start_date_local": "2026-09-10T06:30:00Z", "average_speed": 3.03, "average_heartrate": 151.4, "average_cadence": 84,
         "total_elevation_gain": 55, "start_latlng": [43.65, -79.38]}
    row = activity_to_row(a, {"temp_c": 26, "feels_like_c": 30, "humidity_pct": 70, "wind_kmh": 12, "conditions": "Clear"})
    assert row["Date"] == "2026-09-10"
    assert row["Avg Pace (min/km)"] == "'5:30"
    assert row["Heat Adj Pace (min/km)"] == "'4:47"  # 5.5 / 1.15
    assert row["Avg Cadence"] == 168
    assert row["Strava Link"].endswith("/123")
    assert "Notes" not in row
