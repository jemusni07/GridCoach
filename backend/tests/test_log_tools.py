"""query_log filtering/aggregation, chart request building, and the upsert row-index fix."""
from datetime import date
from types import SimpleNamespace

from app.services import analytics as an
from app.services.sheets import SheetsService, build_chart_request

LOG = [
    {"_row": 2, "Date": "2026-09-02", "Activity ID": "1", "Name": "Evening Run", "Sport": "Run", "Distance (km)": "5.01", "Moving Time (min)": "30", "Temp (°C)": "34.6", "Avg HR": "150"},
    {"_row": 3, "Date": "2026-09-03", "Activity ID": "2", "Name": "Evening Run", "Sport": "Run", "Indoor": "Y", "Distance (km)": "8.06", "Moving Time (min)": "61.2"},
    {"_row": 4, "Date": "2026-09-08", "Activity ID": "3", "Name": "Stair master", "Sport": "Workout", "Distance (km)": "0", "Moving Time (min)": "40"},
    {"_row": 5, "Date": "2026-09-09", "Activity ID": "4", "Name": "Evening Run", "Sport": "Run", "Distance (km)": "4.81", "Moving Time (min)": "24.8", "Temp (°C)": "34.9", "Avg HR": "160"},
    {"_row": 6, "Date": "2026-08-25", "Activity ID": "", "Name": "Bench", "Sport": "WeightTraining", "Distance (km)": "0", "Notes": "x" * 300},
]


def test_query_last_outdoor_run_is_deterministic():
    res = an.filter_log(LOG, sports=["Run"], indoor=False, limit=1)
    assert res["matched"] == 2
    top = res["rows"][0]
    assert top["Date"] == "2026-09-09" and top["_row"] == 5 and top["Temp (°C)"] == "34.9"
    assert "Indoor" not in top  # empty cells omitted


def test_query_by_date_finds_existing_session_for_dedupe():
    res = an.filter_log(LOG, date_from=date(2026, 9, 8), date_to=date(2026, 9, 8))
    assert [r["Name"] for r in res["rows"]] == ["Stair master"]


def test_aggregates_and_month_buckets():
    res = an.filter_log(LOG, sports=["Run"], sort="asc", limit=10)
    agg = res["aggregates"]
    assert agg["count"] == 3 and agg["distance_km"] == 17.9 and agg["avg_hr"] == 155
    assert agg["by_month"]["2026-09"]["count"] == 3
    assert agg["longest"]["Distance (km)"] == "8.06"
    assert res["rows"][0]["Date"] == "2026-09-02"


def test_notes_truncated_in_compact_rows():
    res = an.filter_log(LOG, sports=["WeightTraining"])
    assert res["rows"][0]["Notes"].endswith("…") and len(res["rows"][0]["Notes"]) < 200


def test_chart_request_shapes():
    req = build_chart_request(42, "line", "Top set per lift", "A1:A10", ["B1:B10", "C1:C10"], anchor_cell="F3", y_title="kg")
    chart = req["addChart"]["chart"]
    bc = chart["spec"]["basicChart"]
    assert bc["chartType"] == "LINE" and bc["headerCount"] == 1 and len(bc["series"]) == 2
    assert bc["domains"][0]["domain"]["sourceRange"]["sources"][0] == {"sheetId": 42, "startRowIndex": 0, "endRowIndex": 10, "startColumnIndex": 0, "endColumnIndex": 1}
    pos = chart["position"]["overlayPosition"]["anchorCell"]
    assert pos == {"sheetId": 42, "rowIndex": 2, "columnIndex": 5}
    pie = build_chart_request(1, "pie", "Mix", "A1:A5", ["B1:B5"])
    assert "pieChart" in pie["addChart"]["chart"]["spec"]


class FakeWS:
    def __init__(self, values):
        self.values = values
        self.batch = []
        self.appended = []

    def get_all_values(self, **kw):
        return self.values

    def batch_update(self, data, value_input_option=None):
        self.batch.extend(data)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended.extend(rows)

    def insert_cols(self, cols, col=1, value_input_option=None):
        for row in self.values:
            row.insert(col - 1, cols[0][0] if row is self.values[0] else "")

    def format(self, *a, **k):
        pass

    def sort(self, *specs, range=None):
        self.sorted = (specs, range)

    def get(self, a1=None, value_render_option=None):
        return self.values

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updated = (values, range_name)

    def delete_rows(self, r):
        self.values.pop(r - 1)


def _svc(ws):
    svc = SheetsService("/nonexistent.json")
    svc.open = lambda sid: SimpleNamespace()
    svc._ws = lambda sh, title: ws
    return svc


def test_sort_tab_by_header_keeps_header_row():
    ws = FakeWS([["Date", "Name"], ["2026-09-09", "b"], ["2026-09-02", "a"], ["2026-09-10", "c"]])
    res = _svc(ws).sort_tab("sid", "Training Log", "Date", "desc")
    assert ws.sorted == (((1, "des"),), "A2:B4") and res["rows"] == 3


def test_sort_tab_by_letter_and_unknown_column():
    import pytest
    from app.services.sheets import SheetsError
    ws = FakeWS([["x", "y"], ["1", "2"], ["3", "4"]])
    assert _svc(ws).sort_tab("sid", "T", "B", "asc")["sorted_by"] == "B"
    with pytest.raises(SheetsError):
        _svc(ws).sort_tab("sid", "T", "Nope")


def test_copy_range_single_write_and_delete_rows_bottom_up():
    ws = FakeWS([["Date", "Name"], ["2026-09-09", "b"], ["2026-09-02", "a"], ["2026-09-10", "c"]])
    svc = _svc(ws)
    res = svc.copy_range("sid", "Training Log", None, "Sorted", "A1")
    assert res["copied_rows"] == 4 and ws.updated[1] == "A1" and res["range"] == "A1:B4"
    res = svc.delete_rows("sid", "Training Log", [4, 1, 2])  # header (1) protected; deletes 4 then 2
    assert res["deleted_rows"] == [2, 4] and ws.values == [["Date", "Name"], ["2026-09-02", "a"]]


def test_date_order_verdicts():
    from app.services.sheets import date_order
    assert date_order(["2026-01-01", "2026-02-01", "2026-03-01"]).startswith("ascending")
    assert date_order(["2026-03-01", "2026-02-01"]).startswith("descending")
    assert date_order(["2026-01-01", "2026-03-01", "2026-02-01", "2026-04-01"]).startswith("unsorted (1")

    def batch_update(self, data, value_input_option=None):
        self.batch.extend(data)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended.extend(rows)


def test_upsert_updates_correct_row_and_skips_unchanged():
    header = ["Date", "Activity ID", "Name", "Sport", "Moving Time (min)", "Temp (°C)", "Notes"]
    ws = FakeWS([header,
                 ["2026-09-09", "4", "Evening Run", "Run", "24.8", "", "my note"],
                 ["2026-09-10", "5", "Evening Run", "Run", "60", "", ""]])
    svc = SheetsService("/nonexistent.json")
    svc.open = lambda sid: SimpleNamespace()
    svc._ws = lambda sh, title: ws
    rows = [
        {"Date": "2026-09-10", "Activity ID": "5", "Name": "Evening Run", "Sport": "Run", "Moving Time (min)": 60.0, "Temp (°C)": ""},  # unchanged (60 vs 60.0)
        {"Date": "2026-09-09", "Activity ID": "4", "Name": "Evening Run", "Sport": "Run", "Moving Time (min)": 24.8, "Temp (°C)": 34.9},  # weather filled in
        {"Date": "2026-09-11", "Activity ID": "6", "Name": "Morning Run", "Sport": "Run", "Moving Time (min)": 30, "Temp (°C)": 30},  # new
    ]
    res = svc.upsert_log_rows("sid", rows)
    assert res["updated"] == 1 and res["appended"] == 1
    assert ws.batch[0]["range"] == "A2:G2"                      # the LAST existing row no longer crashes / mis-targets
    assert ws.batch[0]["values"][0][6] == "my note"             # user Notes preserved
    assert ws.batch[0]["values"][0][5] == 34.9
    assert ws.appended[0][1] == "6"
    assert res["added_columns"] == []  # custom header → never migrated


def test_upsert_migrates_untouched_v1_log_header():
    from app.services.sheets import LOG_HEADERS
    v1 = [h for h in LOG_HEADERS if h != "Indoor"]
    ws = FakeWS([list(v1), ["2026-09-10", "5", "Evening Run", "Run"] + [""] * (len(v1) - 4)])
    svc = SheetsService("/nonexistent.json")
    svc.open = lambda sid: SimpleNamespace()
    svc._ws = lambda sh, title: ws
    res = svc.upsert_log_rows("sid", [{"Date": "2026-09-10", "Activity ID": "5", "Name": "Evening Run", "Sport": "Run", "Indoor": "Y"}])
    assert res["added_columns"] == ["Indoor"] and ws.values[0] == LOG_HEADERS
    assert res["updated"] == 1 and ws.batch[0]["values"][0][LOG_HEADERS.index("Indoor")] == "Y"
