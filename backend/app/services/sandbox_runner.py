"""Subprocess entry point for analyze_data. stdin: {"tabs": {name: [[header], [row]...]}, "code": str} → stdout: JSON.

Builds one DataFrame per tab with sensible coercion (numbers, dates, m:ss paces) and executes the code with
`log`/`plan`/`health`/`tabs` in scope. The code sets `result` (DataFrame / Series / dict / list / scalar) and may print().
"""
from __future__ import annotations

import io
import json
import re
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any

import numpy as np
import pandas as pd

MAX_ROWS = 200
PACE_RE = re.compile(r"^\d{1,2}:\d{2}$")


def build_frame(values: list[list[Any]]) -> pd.DataFrame:
    if not values:
        return pd.DataFrame()
    header, rows = [str(h).strip() for h in values[0]], values[1:]
    seen: dict[str, int] = {}
    cols = []
    for h in header:  # de-duplicate blank/duplicate headers
        h = h or "col"
        seen[h] = seen.get(h, 0) + 1
        cols.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    width = len(cols)
    rows = [list(r)[:width] + [""] * (width - len(r)) for r in rows if any(str(c).strip() for c in r)]
    df = pd.DataFrame(rows, columns=cols)
    df.insert(0, "row", range(2, 2 + len(df)))  # sheet row numbers
    for c in cols:
        s = df[c].astype(str).str.strip()
        non_empty = s[s != ""]
        if non_empty.empty:
            continue
        if "date" in c.lower():
            df[c] = pd.to_datetime(s.replace("", np.nan), errors="coerce")
            continue
        if non_empty.str.match(PACE_RE).mean() > 0.6:  # m:ss pace → keep text, add decimal minutes
            parts = s.str.extract(r"^(\d{1,2}):(\d{2})$").astype(float)
            df[f"{c}_min"] = parts[0] + parts[1] / 60
            continue
        num = pd.to_numeric(non_empty.str.replace(",", ""), errors="coerce")
        if num.notna().mean() > 0.8:
            df[c] = pd.to_numeric(s.str.replace(",", "").replace("", np.nan), errors="coerce")
        else:
            df[c] = s.replace("", np.nan)
    return df


def serialize(obj: Any) -> Any:
    if isinstance(obj, pd.DataFrame):
        df = obj.head(MAX_ROWS).copy()
        if isinstance(df.index, pd.MultiIndex) or df.index.name is not None or not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index()
        return {"type": "table", "rows_total": int(len(obj)), "columns": [str(c) for c in df.columns],
                "records": json.loads(df.to_json(orient="records", date_format="iso"))}
    if isinstance(obj, pd.Series):
        return {"type": "series", "name": str(obj.name), "values": json.loads(obj.head(MAX_ROWS).to_json(date_format="iso"))}
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, dict):
        return {str(k): serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj[:MAX_ROWS]]
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return obj


def main() -> None:
    payload = json.loads(sys.stdin.read())
    tabs = {name: build_frame(values) for name, values in (payload.get("tabs") or {}).items()}
    ns: dict[str, Any] = {"pd": pd, "np": np, "tabs": tabs,
                          "log": tabs.get("Training Log", pd.DataFrame()), "plan": tabs.get("Training Plan", pd.DataFrame()),
                          "health": tabs.get("Health Notes", pd.DataFrame()), "strava": tabs.get("Strava", pd.DataFrame()), "result": None}
    buf = io.StringIO()
    out: dict[str, Any] = {"columns": {name: [str(c) for c in df.columns] for name, df in tabs.items()},
                           "shapes": {name: [int(df.shape[0]), int(df.shape[1])] for name, df in tabs.items()}}
    try:
        with redirect_stdout(buf):
            exec(compile(payload["code"], "<analysis>", "exec"), ns)  # noqa: S102 — code is AST-checked by the parent
        out["result"] = serialize(ns.get("result"))
    except Exception as e:  # send the traceback back so the model can fix its code
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()[-1500:]
    out["stdout"] = buf.getvalue()[-6000:]
    sys.stdout.write(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
