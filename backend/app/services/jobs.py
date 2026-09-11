"""Background jobs with live progress events, and pending (unconfirmed) destructive actions.

The sidebar can't hold a long HTTP request (Apps Script caps at ~6 min and gives no streaming), so every
agent turn runs as a job: the client gets a job id immediately and polls for events + the final result.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("gridcoach.jobs")

EventFn = Callable[..., None]


@dataclass
class Job:
    id: str
    spreadsheet_id: str
    kind: str
    status: str = "running"  # running | done | error
    created: float = field(default_factory=time.time)
    finished: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None


class JobManager:
    def __init__(self, ttl_seconds: int = 3600):
        self.jobs: dict[str, Job] = {}
        self.ttl = ttl_seconds

    def create(self, spreadsheet_id: str, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], spreadsheet_id=spreadsheet_id, kind=kind)
        self.jobs[job.id] = job
        return job

    def emitter(self, job_id: str) -> EventFn:
        def emit(type: str, text: str, **extra: Any) -> None:
            self.emit(job_id, type, text, **extra)
        return emit

    def emit(self, job_id: str, type: str, text: str, **extra: Any) -> None:
        job = self.jobs.get(job_id)
        if job:
            job.events.append({"i": len(job.events), "t": round(time.time(), 3), "type": type, "text": text, **extra})

    def start(self, job: Job, coro: Awaitable[dict[str, Any]]) -> Job:
        asyncio.create_task(self._run(job, coro))
        return job

    async def _run(self, job: Job, coro: Awaitable[dict[str, Any]]) -> None:
        try:
            job.result = await coro
            job.status = "done"
        except Exception as e:  # surfaced to the client, never lost
            log.exception("job %s (%s) failed", job.id, job.kind)
            job.error = f"{type(e).__name__}: {e}"
            job.status = "error"
        finally:
            job.finished = time.time()
            self._gc()

    def snapshot(self, job_id: str, after: int = 0) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        return {"id": job.id, "kind": job.kind, "status": job.status, "events": job.events[after:], "next": len(job.events),
                "result": job.result if job.status == "done" else None, "error": job.error}

    def _gc(self) -> None:
        cutoff = time.time() - self.ttl
        for jid in [j for j, job in self.jobs.items() if job.finished and job.finished < cutoff]:
            self.jobs.pop(jid, None)


class PendingActions:
    """Destructive tool calls parked here until the athlete presses Confirm in the sidebar."""

    def __init__(self, ttl_seconds: int = 1800):
        self.items: dict[str, dict[str, Any]] = {}
        self.ttl = ttl_seconds

    def add(self, spreadsheet_id: str, tool: str, args: dict[str, Any], description: str) -> dict[str, Any]:
        self._gc()
        item = {"action_id": uuid.uuid4().hex[:10], "spreadsheet_id": spreadsheet_id, "tool": tool, "args": args,
                "description": description, "created": time.time()}
        self.items[item["action_id"]] = item
        return item

    def pop(self, action_id: str, spreadsheet_id: str) -> dict[str, Any] | None:
        item = self.items.get(action_id)
        if item and item["spreadsheet_id"] == spreadsheet_id:
            return self.items.pop(action_id)
        return None

    def _gc(self) -> None:
        cutoff = time.time() - self.ttl
        for k in [k for k, v in self.items.items() if v["created"] < cutoff]:
            self.items.pop(k, None)
