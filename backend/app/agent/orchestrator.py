"""The agent loop: OpenAI Responses API + function tools, one conversation thread per spreadsheet.

Statefulness: the last `response_id` per spreadsheet is chained with `previous_response_id`, so the model keeps
the whole conversation; durable athlete facts live in the memory store and are injected every turn.
Every turn can emit progress events (tool started/finished) and can park destructive tool calls as
pending actions that the athlete confirms from the sidebar.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from openai import AsyncOpenAI, BadRequestError, NotFoundError

from ..config import Settings
from ..services.jobs import EventFn, PendingActions
from ..services.pipeline import Pipeline
from ..services.sheets import SheetsService
from ..services.state import Store
from ..services.weather import WeatherService
from . import prompts
from .tools import ANALYST_TOOLS, TOOLS, ToolRunner, dumps

log = logging.getLogger("gridcoach.agent")


@dataclass
class ChatResult:
    reply: str
    response_id: str | None
    touched_ranges: list[str] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, sheets: SheetsService, weather: WeatherService,
                 pipeline: Pipeline, pending: PendingActions | None = None):
        self.settings = settings
        self.store = store
        self.sheets = sheets
        self.weather = weather
        self.pipeline = pipeline
        self.pending = pending or PendingActions()
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    def _model_kwargs(self, effort: str | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.settings.openai_model}
        if self.settings.openai_model.startswith(("gpt-5", "o")):
            kw["reasoning"] = {"effort": effort or self.settings.openai_reasoning_effort}
        return kw

    def _runner(self, spreadsheet_id: str, source: str, on_event: EventFn | None, bypass_confirm: bool = False) -> ToolRunner:
        return ToolRunner(spreadsheet_id, self.sheets, self.pipeline, self.weather, source=source,
                          on_event=on_event, pending=self.pending, store=self.store, bypass_confirm=bypass_confirm)

    async def _run(self, *, instructions: str, input_items: list[dict[str, Any]], runner: ToolRunner,
                   tools: list[dict[str, Any]], previous_response_id: str | None, max_turns: int = 16,
                   effort: str | None = None) -> ChatResult:
        base = {**self._model_kwargs(effort), "instructions": instructions, "tools": tools, "store": True}
        emit = runner.on_event

        async def create(inp: list[dict[str, Any]], prev: str | None):
            try:
                return await self.client.responses.create(input=inp, previous_response_id=prev, **base)
            except (BadRequestError, NotFoundError) as e:
                msg = str(e).lower().replace("_", " ")
                if prev and "previous response" in msg:  # expired/unknown thread → start fresh rather than fail
                    log.warning("previous_response_id %s rejected, starting a new thread", prev)
                    return await self.client.responses.create(input=inp, **base)
                raise

        if emit:
            emit("status", "Thinking…")
        resp = await create(input_items, previous_response_id)
        for _ in range(max_turns):
            calls = [o for o in resp.output if getattr(o, "type", None) == "function_call"]
            if not calls:
                break
            outputs = []
            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await runner.run(call.name, args)
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": dumps(result)})
            if emit:
                emit("status", "Thinking…")
            resp = await create(outputs, resp.id)

        text = (resp.output_text or "").strip()
        if not text:
            text = "Done — I updated the sheet." if runner.touched else "I couldn't produce a reply; try rephrasing."
        return ChatResult(reply=text, response_id=resp.id, touched_ranges=runner.touched, tool_log=runner.log,
                          pending_actions=runner.pending_actions)

    # ---- public entry points ----------------------------------------------------

    async def chat(self, spreadsheet_id: str, message: str, context: dict[str, Any] | None, reset: bool = False,
                   on_event: EventFn | None = None) -> ChatResult:
        tenant = self.store.get_tenant(spreadsheet_id)
        prev = None if reset else (tenant or {}).get("last_response_id")
        runner = self._runner(spreadsheet_id, "sidebar", on_event)
        memories = self.store.list_memories(spreadsheet_id)
        content = f"{prompts.context_block(context, tenant, memories)}\n\n{message.strip()}"
        result = await self._run(instructions=prompts.system_prompt(), input_items=[{"role": "user", "content": content}],
                                 runner=runner, tools=TOOLS, previous_response_id=prev)
        self.store.set_last_response(spreadsheet_id, result.response_id)
        return result

    async def resolve_action(self, spreadsheet_id: str, action_id: str, approve: bool, on_event: EventFn | None = None) -> ChatResult:
        """Athlete pressed Confirm/Cancel on a parked destructive action: execute (or not), then let the coach wrap up."""
        action = self.pending.pop(action_id, spreadsheet_id)
        if not action:
            return ChatResult(reply="That action has expired or was already handled.", response_id=None)
        if approve:
            runner = self._runner(spreadsheet_id, "sidebar", on_event, bypass_confirm=True)
            result = await runner.run(action["tool"], action["args"])
            note = (f"[The athlete pressed CONFIRM on: {action['description']}. It has now been executed. "
                    f"Result: {dumps(result, 3000)}] Reply in one or two lines with what changed.")
            touched = runner.touched
        else:
            note = f"[The athlete pressed CANCEL on: {action['description']}. Nothing was changed.] Acknowledge in one line."
            touched = []
        tenant = self.store.get_tenant(spreadsheet_id) or {}
        follow = self._runner(spreadsheet_id, "sidebar", on_event)
        res = await self._run(instructions=prompts.system_prompt(), input_items=[{"role": "user", "content": note}],
                              runner=follow, tools=TOOLS, previous_response_id=tenant.get("last_response_id"), max_turns=4, effort="low")
        self.store.set_last_response(spreadsheet_id, res.response_id)
        res.touched_ranges = touched + res.touched_ranges
        return res

    async def background_analysis(self, spreadsheet_id: str, processed: dict[str, Any]) -> ChatResult:
        """Webhook path: fresh thread, analyst posture, writes one Coach Note."""
        runner = self._runner(spreadsheet_id, "strava webhook", None)
        analysis: dict[str, Any] = {}
        aid = (processed.get("activity") or {}).get("Activity ID")
        if aid:
            try:
                analysis = await self.pipeline.analyze_activity(spreadsheet_id, aid)
            except Exception as e:
                analysis = {"error": str(e)}
        payload = {"activity": processed.get("activity"), "snapshot": processed.get("snapshot"), "analysis": analysis,
                   "athlete_memory": self.store.list_memories(spreadsheet_id)}
        return await self._run(instructions=prompts.analyst_prompt(),
                               input_items=[{"role": "user", "content": f"New activity synced. Data:\n{dumps(payload, 12000)}"}],
                               runner=runner, tools=ANALYST_TOOLS, previous_response_id=None, max_turns=6, effort="low")
