"""Per-spreadsheet state: Strava tokens + the last OpenAI response id (conversation thread).

One spreadsheet == one tenant. Tokens are never shared across spreadsheets (NFR-2).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    spreadsheet_id   TEXT PRIMARY KEY,
    athlete_id       INTEGER,
    athlete_name     TEXT,
    access_token     TEXT,
    refresh_token    TEXT,
    expires_at       INTEGER,
    last_response_id TEXT,
    last_sync_at     INTEGER,
    created_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tenants_athlete ON tenants(athlete_id);
CREATE TABLE IF NOT EXISTS memories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    spreadsheet_id TEXT NOT NULL,
    category       TEXT NOT NULL,
    fact           TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS memories_sheet ON memories(spreadsheet_id, active);
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    spreadsheet_id TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    role           TEXT NOT NULL,
    text           TEXT NOT NULL,
    meta           TEXT
);
CREATE INDEX IF NOT EXISTS messages_sheet ON messages(spreadsheet_id, id);
CREATE TABLE IF NOT EXISTS activities (
    spreadsheet_id TEXT NOT NULL,
    activity_id    INTEGER NOT NULL,
    start_date     TEXT,
    sport          TEXT,
    json           TEXT NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (spreadsheet_id, activity_id)
);
"""
# columns added after v1 — applied idempotently at startup
TENANT_MIGRATIONS = [
    "ALTER TABLE tenants ADD COLUMN last_job_id TEXT",
    "ALTER TABLE tenants ADD COLUMN cache_backfilled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tenants ADD COLUMN cache_synced_at INTEGER",
]


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for stmt in TENANT_MIGRATIONS:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._conn.commit()

    def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _exec(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def get_tenant(self, spreadsheet_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tenants WHERE spreadsheet_id = ?", (spreadsheet_id,))

    def find_by_athlete(self, athlete_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tenants WHERE athlete_id = ?", (athlete_id,))

    def ensure_tenant(self, spreadsheet_id: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO tenants (spreadsheet_id, created_at) VALUES (?, ?)",
            (spreadsheet_id, int(time.time())),
        )

    def save_strava_tokens(
        self,
        spreadsheet_id: str,
        *,
        athlete_id: int,
        athlete_name: str,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> None:
        self.ensure_tenant(spreadsheet_id)
        self._exec(
            """UPDATE tenants SET athlete_id=?, athlete_name=?, access_token=?, refresh_token=?, expires_at=?
               WHERE spreadsheet_id=?""",
            (athlete_id, athlete_name, access_token, refresh_token, expires_at, spreadsheet_id),
        )

    def update_access_token(self, spreadsheet_id: str, access_token: str, refresh_token: str, expires_at: int) -> None:
        self._exec(
            "UPDATE tenants SET access_token=?, refresh_token=?, expires_at=? WHERE spreadsheet_id=?",
            (access_token, refresh_token, expires_at, spreadsheet_id),
        )

    def disconnect_strava(self, spreadsheet_id: str) -> None:
        self._exec(
            "UPDATE tenants SET athlete_id=NULL, athlete_name=NULL, access_token=NULL, refresh_token=NULL, expires_at=NULL WHERE spreadsheet_id=?",
            (spreadsheet_id,),
        )

    def set_last_response(self, spreadsheet_id: str, response_id: str | None) -> None:
        self.ensure_tenant(spreadsheet_id)
        self._exec("UPDATE tenants SET last_response_id=? WHERE spreadsheet_id=?", (response_id, spreadsheet_id))

    def set_last_sync(self, spreadsheet_id: str) -> None:
        self.ensure_tenant(spreadsheet_id)
        self._exec("UPDATE tenants SET last_sync_at=? WHERE spreadsheet_id=?", (int(time.time()), spreadsheet_id))

    # ---- durable athlete memory (per spreadsheet) --------------------------------

    def list_memories(self, spreadsheet_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, category, fact, created_at FROM memories WHERE spreadsheet_id=? AND active=1 ORDER BY id",
                (spreadsheet_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_memory(self, spreadsheet_id: str, category: str, fact: str) -> dict[str, Any]:
        fact = fact.strip()
        for m in self.list_memories(spreadsheet_id):
            if m["fact"].lower() == fact.lower():
                return {**m, "duplicate": True}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (spreadsheet_id, category, fact, created_at) VALUES (?, ?, ?, ?)",
                (spreadsheet_id, category.strip().lower() or "other", fact, int(time.time())))
            self._conn.commit()
            mid = cur.lastrowid
        return {"id": mid, "category": category, "fact": fact, "created_at": int(time.time())}

    def forget_memory(self, spreadsheet_id: str, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE memories SET active=0 WHERE id=? AND spreadsheet_id=? AND active=1", (memory_id, spreadsheet_id))
            self._conn.commit()
        return cur.rowcount > 0

    # ---- chat transcript (so the sidebar can replay what the model already knows) ----------

    def add_message(self, spreadsheet_id: str, role: str, text: str, meta: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO messages (spreadsheet_id, ts, role, text, meta) VALUES (?, ?, ?, ?, ?)",
                                     (spreadsheet_id, int(time.time()), role, text, json.dumps(meta) if meta else None))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_messages(self, spreadsheet_id: str, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id, ts, role, text, meta FROM messages WHERE spreadsheet_id=? ORDER BY id DESC LIMIT ?",
                                      (spreadsheet_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
            out.append(d)
        return out

    def clear_messages(self, spreadsheet_id: str) -> None:
        self._exec("DELETE FROM messages WHERE spreadsheet_id=?", (spreadsheet_id,))

    def set_last_job(self, spreadsheet_id: str, job_id: str | None) -> None:
        self.ensure_tenant(spreadsheet_id)
        self._exec("UPDATE tenants SET last_job_id=? WHERE spreadsheet_id=?", (job_id, spreadsheet_id))

    # ---- Strava activity cache (full history, raw JSON) --------------------------------------

    def upsert_activities(self, spreadsheet_id: str, activities: list[dict[str, Any]]) -> int:
        now = int(time.time())
        rows = [(spreadsheet_id, int(a["id"]), a.get("start_date") or "", a.get("sport_type") or a.get("type") or "", json.dumps(a), now)
                for a in activities if a.get("id") is not None]
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO activities (spreadsheet_id, activity_id, start_date, sport, json, updated_at) VALUES (?, ?, ?, ?, ?, ?)", rows)
            self._conn.commit()
        return len(rows)

    def list_activities(self, spreadsheet_id: str, date_from: str | None = None, date_to: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT json FROM activities WHERE spreadsheet_id=?", [spreadsheet_id]
        if date_from:
            sql += " AND start_date >= ?"; params.append(date_from)
        if date_to:
            sql += " AND start_date <= ?"; params.append(date_to + "~")  # '~' sorts after any time suffix
        sql += " ORDER BY start_date"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def cache_info(self, spreadsheet_id: str) -> dict[str, Any]:
        t = self.get_tenant(spreadsheet_id) or {}
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) AS n, MIN(start_date) AS oldest, MAX(start_date) AS newest FROM activities WHERE spreadsheet_id=?",
                                   (spreadsheet_id,)).fetchone()
        return {"count": r["n"], "oldest": (r["oldest"] or "")[:10] or None, "newest": (r["newest"] or "")[:10] or None,
                "backfilled": bool(t.get("cache_backfilled")), "synced_at": t.get("cache_synced_at")}

    def mark_cache(self, spreadsheet_id: str, backfilled: bool | None = None, synced_at: int | None = None) -> None:
        self.ensure_tenant(spreadsheet_id)
        if backfilled is not None:
            self._exec("UPDATE tenants SET cache_backfilled=? WHERE spreadsheet_id=?", (int(backfilled), spreadsheet_id))
        self._exec("UPDATE tenants SET cache_synced_at=? WHERE spreadsheet_id=?", (synced_at or int(time.time()), spreadsheet_id))
