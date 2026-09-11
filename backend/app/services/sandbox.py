"""`analyze_data`: run a model-written pandas snippet over the athlete's tabs in an isolated subprocess.

Why: fixed aggregates can't answer nuanced questions ("pace on hot vs cool days", "weekly trend since June",
"runs after <6 h sleep"). Letting the model write a few lines of pandas — executed by us, deterministically —
gives it real analytical reach without letting it do arithmetic in its head.

Safety: AST-whitelisted imports, no builtins that touch the OS, a fresh `python -I` process, no network use
(nothing to call), stdin/stdout JSON only, hard timeout.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

RUNNER = Path(__file__).with_name("sandbox_runner.py")
ALLOWED_MODULES = {"pandas", "numpy", "math", "statistics", "datetime", "json", "re", "collections", "itertools", "functools"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "input", "breakpoint",
                   "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib", "ctypes", "signal"}


def check_code(code: str) -> None:
    """Reject anything outside a small pure-computation surface. Raises ValueError with a model-readable reason."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"SyntaxError: {e.msg} (line {e.lineno})") from e
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] not in ALLOWED_MODULES:
                    raise ValueError(f"import of '{n}' is not allowed; allowed: {sorted(ALLOWED_MODULES)}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"'{node.id}' is not allowed in analysis code")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in {"to_csv", "to_excel", "to_pickle", "read_csv", "read_pickle"}):
            raise ValueError(f"attribute '{node.attr}' is not allowed in analysis code")


async def run_analysis(tabs: dict[str, list[list[Any]]], code: str, timeout: float = 30.0) -> dict[str, Any]:
    check_code(code)
    payload = json.dumps({"tabs": tabs, "code": code}, default=str).encode()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(RUNNER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0", "MPLBACKEND": "Agg"},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"analysis timed out after {timeout:.0f}s — simplify the code or filter fewer rows"}
    if proc.returncode != 0 and not out:
        return {"error": f"analysis process failed: {err.decode(errors='replace')[-800:]}"}
    try:
        return json.loads(out.decode())
    except json.JSONDecodeError:
        return {"error": "analysis produced no JSON", "stdout": out.decode(errors="replace")[-2000:], "stderr": err.decode(errors="replace")[-800:]}
