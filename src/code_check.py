"""Code diagnostics tool (`check_code`) — catch errors without running the code.

The agent edits files but otherwise only discovers a typo, undefined name, or
type error by *running* the code. This runs a fast static checker over a file or
folder and returns structured `file:line` errors, so the agent can verify an
edit (alongside the project's tests, per phase 19) before reporting done.

- **Python:** `ruff` (bundled, zero-config) — syntax errors, undefined names,
  unused imports, and lint issues. Runs via the bundled interpreter, so it works
  with no project setup.
- **JS / TS:** the project's OWN `tsc --noEmit` (uses its tsconfig + installed
  TypeScript) — real type errors. Best-effort: needs Node available.

Confined to the workspace like the other file tools. Read-only: it never edits.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

# Cap returned diagnostics so a project with thousands of lint hits doesn't flood
# the agent — it gets the first N plus a total count.
MAX_DIAGNOSTICS = 80
_CHECK_TIMEOUT_S = 120

_PY_EXTS = {".py", ".pyi"}
_JSTS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def _parse_path(content: str) -> str:
    raw = (content or "").strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("check_code expects a JSON object or a bare path")
        return str(data.get("path") or data.get("file") or "").strip()
    return raw


def _resolve(path: str, workspace: Optional[str]) -> str:
    from src.tool_execution import _resolve_search_root

    root = _resolve_search_root(path, workspace)
    if not os.path.exists(root):
        raise ValueError(f"path '{path}' does not exist")
    return root


def _project_root_for(path: str, workspace: Optional[str], markers: tuple) -> str:
    """Nearest ancestor of `path` (not above the workspace) that holds a marker
    file like tsconfig.json / package.json — where a project checker should run."""
    start = path if os.path.isdir(path) else os.path.dirname(path)
    ceiling = os.path.realpath(workspace) if workspace else None
    cur = os.path.realpath(start)
    while True:
        if any(os.path.isfile(os.path.join(cur, m)) for m in markers):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or (ceiling and cur == ceiling):
            return start
        cur = parent


def _detect_language(path: str) -> Optional[str]:
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in _PY_EXTS:
            return "python"
        if ext in _JSTS_EXTS:
            return "jsts"
        return None
    # Directory: prefer a JS/TS project (tsconfig/package.json), else Python.
    for marker in ("tsconfig.json", "package.json"):
        if os.path.isfile(os.path.join(path, marker)):
            return "jsts"
    for dirpath, _dirs, files in os.walk(path):
        if "node_modules" in dirpath or "__pycache__" in dirpath:
            continue
        if any(f.endswith(".py") for f in files):
            return "python"
    return None


async def _run(argv: List[str], cwd: Optional[str]) -> tuple[str, str, Optional[int], bool]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_CHECK_TIMEOUT_S)
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode, False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "", None, True


async def _check_python(path: str) -> Dict[str, Any]:
    out, err, rc, timed_out = await _run(
        [sys.executable or "python", "-m", "ruff", "check", path, "--output-format=json"],
        cwd=None,
    )
    if timed_out:
        return {"error": f"check_code: ruff timed out after {_CHECK_TIMEOUT_S}s", "exit_code": 1}
    if rc not in (0, 1):
        hint = (err or "").strip()[:300]
        if "No module named ruff" in hint:
            hint = "ruff is not installed in this environment"
        return {"error": f"check_code: ruff failed: {hint or 'unknown error'}", "exit_code": 1}
    try:
        items = json.loads(out or "[]")
    except (json.JSONDecodeError, ValueError):
        return {"error": "check_code: could not parse ruff output", "exit_code": 1}

    diags = []
    for it in items:
        loc = it.get("location") or {}
        diags.append({
            "file": _rel(it.get("filename") or path, path),
            "line": loc.get("row"),
            "col": loc.get("column"),
            "code": it.get("code"),
            "message": it.get("message"),
        })
    return _format("ruff (Python)", path, diags)


async def _check_jsts(path: str, workspace: Optional[str]) -> Dict[str, Any]:
    from core.platform_compat import which_tool

    root = _project_root_for(path, workspace, ("tsconfig.json", "package.json"))
    npx = which_tool("npx")
    if not npx:
        return {
            "error": "check_code: Node/npx not found, so JS/TS type-checking is unavailable. "
                     "Install Node, or check Python files (ruff is bundled).",
            "exit_code": 1,
        }
    if not os.path.isfile(os.path.join(root, "tsconfig.json")):
        return {
            "error": f"check_code: no tsconfig.json under {root}; cannot type-check this JS/TS project.",
            "exit_code": 1,
        }
    out, err, rc, timed_out = await _run([npx, "tsc", "--noEmit"], cwd=root)
    if timed_out:
        return {"error": f"check_code: tsc timed out after {_CHECK_TIMEOUT_S}s", "exit_code": 1}

    # tsc prints "path(line,col): error TSxxxx: message" to stdout.
    diags = []
    pat = re.compile(r"^(.*?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.*)$")
    for line in (out + "\n" + err).splitlines():
        m = pat.match(line.strip())
        if m:
            diags.append({
                "file": _rel(os.path.join(root, m.group(1)), path),
                "line": int(m.group(2)),
                "col": int(m.group(3)),
                "code": m.group(4),
                "message": m.group(5),
            })
    return _format("tsc (TypeScript)", path, diags)


def _rel(filename: str, anchor: str) -> str:
    base = anchor if os.path.isdir(anchor) else os.path.dirname(anchor)
    try:
        return os.path.relpath(filename, base).replace("\\", "/")
    except ValueError:
        return filename.replace("\\", "/")


def _format(checker: str, path: str, diags: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(diags)
    shown = diags[:MAX_DIAGNOSTICS]
    if not total:
        body = f"# Diagnostics — {checker}\n\nNo issues found — the checker reported nothing."
    else:
        lines = [f"# Diagnostics — {checker}", f"{total} issue(s) found:\n"]
        for d in shown:
            loc = f"{d['file']}:{d.get('line', '?')}:{d.get('col', '?')}"
            lines.append(f"- {loc}  {d.get('code', '')}  {d.get('message', '')}".rstrip())
        if total > len(shown):
            lines.append(f"\n... and {total - len(shown)} more (showing first {MAX_DIAGNOSTICS}).")
        body = "\n".join(lines)
    return {
        "output": body,
        "exit_code": 0,
        "diagnostics": {"checker": checker, "total": total, "issues": shown},
    }


async def check_code(
    content: str,
    *,
    workspace: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    del owner
    try:
        path_arg = _parse_path(content)
        if not path_arg:
            raise ValueError('check_code needs a "path" (a file or folder to check)')
        path = _resolve(path_arg, workspace)
        lang = _detect_language(path)
        if lang == "python":
            return await _check_python(path)
        if lang == "jsts":
            return await _check_jsts(path, workspace)
        return {
            "error": f"check_code: no checker for '{path_arg}' — supported: Python (.py via ruff) "
                     "and JS/TS projects (.ts/.tsx/.js via tsc).",
            "exit_code": 1,
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"check_code: {exc}", "exit_code": 1}
    except Exception as exc:
        return {"error": f"check_code failed: {type(exc).__name__}: {exc}", "exit_code": 1}
