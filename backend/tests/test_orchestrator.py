"""Exercise the Responses tool loop with a fake OpenAI client — no network, no sheets."""
from types import SimpleNamespace

import httpx
from openai import BadRequestError

from app.agent.orchestrator import Orchestrator
from app.services.state import Store


class FakeSheets:
    def __init__(self):
        self.appended = []

    def read_range(self, sid, sheet, a1=None, max_rows=200, max_cols=40):
        return {"sheet": sheet, "values": [["Metric", "Value"], ["ACWR (distance)", "1.12"]], "row_count": 2, "truncated": False, "range": "all"}

    def add_coach_note(self, sid, source, activity, note):
        self.appended.append((source, activity, note))
        return {"sheet": "Coach Notes", "range": "A5:D5", "rows_appended": 1}


def fc(name, args, call_id):
    return SimpleNamespace(type="function_call", name=name, arguments=args, call_id=call_id)


class FakeResponses:
    """Scripted responses: first turn asks for three tools (one unknown), second turn answers."""
    def __init__(self, fail_prev=False):
        self.calls = []
        self.fail_prev = fail_prev

    async def create(self, **kw):
        self.calls.append(kw)
        if self.fail_prev and kw.get("previous_response_id") == "stale":
            resp = httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
            raise BadRequestError("Previous response with id 'stale' not found.", response=resp, body=None)
        if not any(i.get("type") == "function_call_output" for i in kw["input"]):
            return SimpleNamespace(id="r1", output=[
                fc("read_range", '{"sheet": "Health Snapshot"}', "c1"),
                fc("add_coach_note", '{"note": "ACWR 1.12 — sweet spot"}', "c2"),
                fc("nope", "{}", "c3"),
            ], output_text="")
        outs = {i["call_id"]: i["output"] for i in kw["input"]}
        assert "ACWR" in outs["c1"] and "Coach Notes" in outs["c2"] and "unknown tool" in outs["c3"]
        return SimpleNamespace(id="r2", output=[SimpleNamespace(type="message")], output_text="You're in the sweet spot.")


def make(tmp_path, fail_prev=False):
    settings = SimpleNamespace(openai_api_key="x", openai_model="gpt-4.1")
    store = Store(str(tmp_path / "t.db"))
    orch = Orchestrator(settings, store, FakeSheets(), weather=None, pipeline=None)
    orch.client = SimpleNamespace(responses=FakeResponses(fail_prev))
    return orch, store


async def test_tool_loop_and_thread_persistence(tmp_path):
    orch, store = make(tmp_path)
    res = await orch.chat("sheet1", "how am I doing?", {"active_sheet": "Training Log", "sheet_names": ["Training Log"]})
    assert res.reply == "You're in the sweet spot."
    assert res.touched_ranges == ["Coach Notes!A5:D5"]
    assert [t["tool"] for t in res.tool_log] == ["read_range", "add_coach_note", "nope"]
    assert [t["ok"] for t in res.tool_log] == [True, True, False]
    assert store.get_tenant("sheet1")["last_response_id"] == "r2"
    first = orch.client.responses.calls[0]
    assert "<sheet_context>" in first["input"][0]["content"] and "Training Log" in first["input"][0]["content"]
    assert first["previous_response_id"] is None and first["instructions"].startswith("You are GridCoach")
    assert orch.client.responses.calls[1]["previous_response_id"] == "r1"
    assert orch.sheets.appended[0][0] == "sidebar"


async def test_stale_thread_recovers(tmp_path):
    orch, store = make(tmp_path, fail_prev=True)
    store.set_last_response("sheet1", "stale")
    res = await orch.chat("sheet1", "hi", None)
    assert res.reply and store.get_tenant("sheet1")["last_response_id"] == "r2"
    assert orch.client.responses.calls[0]["previous_response_id"] == "stale"
    assert "previous_response_id" not in orch.client.responses.calls[1]
