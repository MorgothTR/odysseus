"""Agent-facing background service manager (the `manage_processes` tool).

Built on src.bg_jobs — same detached launcher, restart-safe on-disk store,
and crash follow-ups. This adds the service-shaped lifecycle that the bash
`#!bg` path can't express: a dev server / file watcher / emulator is SUPPOSED
to keep running, so it must not be reaped as a runaway, and the agent needs
list / logs / stop to manage it while it runs.

Actions (JSON object in the tool block):
  {"action": "start", "command": "npm run dev", "cwd": "<allowed folder>", "name": "dev-server"}
  {"action": "list"}
  {"action": "logs", "id": "<job id>", "lines": 80}
  {"action": "stop", "id": "<job id>"}

The working directory is confined by the same policy as the file tools
(workspace when one is set, otherwise the allowed-folder allowlist).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from src import bg_jobs

# How long to wait after spawning before checking whether the service
# crashed instantly (command not found, port already bound, syntax error).
# Long enough for the wrapper script + interpreter to start and fail,
# short enough not to drag the chat turn.
_STARTUP_PROBE_S = 1.5
# Log lines included in start/stop/list outputs (logs action has its own cap).
_SNIPPET_LINES = 15

_VALID_ACTIONS = ("start", "list", "logs", "stop")

# Models reach for natural synonyms ("terminate", "kill", "tail") before
# reading error text — accept them instead of bouncing the call.
_ACTION_ALIASES = {
    "start": "start", "run": "start", "launch": "start", "spawn": "start",
    "list": "list", "ps": "list", "status": "list", "processes": "list",
    "logs": "logs", "log": "logs", "tail": "logs", "output": "logs",
    "stop": "stop", "kill": "stop", "terminate": "stop", "end": "stop", "shutdown": "stop",
}


def _parse_args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {"action": "list"}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("manage_processes expects a JSON object")
        return parsed
    # Bare-word convenience: ```manage_processes\nlist``` etc.
    word = raw.split()[0].lower()
    if word in _ACTION_ALIASES:
        action = _ACTION_ALIASES[word]
        args: Dict[str, Any] = {"action": action}
        rest = raw[len(word):].strip()
        if rest:
            args["id" if action in ("logs", "stop") else "command"] = rest
        return args
    raise ValueError(
        "manage_processes expects JSON like "
        '{"action": "start", "command": "npm run dev", "cwd": "<allowed folder>"}'
    )


def _resolve_cwd(raw_cwd: str, workspace: Optional[str]) -> str:
    from src.tool_execution import _resolve_search_root

    root = _resolve_search_root(raw_cwd, workspace)
    if not os.path.isdir(root):
        raise ValueError(f"cwd '{raw_cwd}' is not an existing directory")
    return root


def _uptime(rec: Dict[str, Any]) -> str:
    end = rec.get("ended_at") or time.time()
    seconds = max(0, int(end - rec.get("started_at", end)))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _describe(rec: Dict[str, Any]) -> str:
    kind = "service" if rec.get("service") else "job"
    name = f" ({rec['name']})" if rec.get("name") else ""
    cmd = (rec.get("command") or "").strip().splitlines()[0][:60]
    exit_part = ""
    if rec.get("status") not in ("running",) and rec.get("exit_code") is not None:
        exit_part = f" exit={rec.get('exit_code')}"
    return (
        f"{rec.get('id')}{name} [{kind}] {rec.get('status')}{exit_part} "
        f"uptime={_uptime(rec)} cmd: {cmd}"
    )


def _tail_lines(rec: Dict[str, Any], lines: int) -> str:
    text = bg_jobs.tail(rec["id"], lines=lines)
    return text or "(no output yet)"


def _find_job(ident: str) -> Optional[Dict[str, Any]]:
    """Resolve a job by id, falling back to service NAME — the model often
    addresses a service by the name it gave it ('stop expense-dev')."""
    rec = bg_jobs.get(ident)
    if rec:
        return rec
    ident_low = ident.lower()
    matches = [
        r for r in bg_jobs.refresh().values()
        if (r.get("name") or "").lower() == ident_low
    ]
    if not matches:
        return None
    # Prefer the running instance; otherwise the most recent one.
    matches.sort(key=lambda r: (r.get("status") == "running", r.get("started_at") or 0), reverse=True)
    return bg_jobs.get(matches[0]["id"])


def _same_dir(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left or "")) == os.path.normcase(os.path.realpath(right or ""))
    except OSError:
        return False


async def _start(args: Dict[str, Any], *, session_id: str, workspace: Optional[str]) -> Dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError('start needs a "command", e.g. {"action": "start", "command": "npm run dev"}')
    cwd = _resolve_cwd(str(args.get("cwd") or "").strip(), workspace)
    name = str(args.get("name") or "").strip()

    # Refuse to stack a second copy of an identical service. Restart loops
    # (the model misreads a quiet log or failed probe as "broken", starts it
    # "again") otherwise pile several binders onto one port — on Windows
    # SO_REUSEADDR lets them ALL bind, and connections then land on an
    # arbitrary, often half-dead, copy.
    for existing in bg_jobs.refresh().values():
        if (existing.get("status") == "running" and existing.get("service")
                and (existing.get("command") or "").strip() == command
                and _same_dir(existing.get("cwd") or "", cwd)):
            label = f" ({existing['name']})" if existing.get("name") else ""
            return {
                "error": (
                    f"manage_processes: this exact service is ALREADY RUNNING as "
                    f"`{existing['id']}`{label} — not starting a duplicate. Reuse it: check it "
                    f"with {{\"action\": \"logs\", \"id\": \"{existing['id']}\"}}; if you really "
                    f"want a fresh instance, first {{\"action\": \"stop\", \"id\": \"{existing['id']}\"}}. "
                    f"A quiet log usually means a healthy idle server, not a broken one.\n"
                    f"Current output:\n{_tail_lines(existing, _SNIPPET_LINES)}"
                ),
                "exit_code": 1,
                "process": {"id": existing["id"], "status": "running"},
            }

    rec = bg_jobs.launch(command, session_id=session_id, cwd=cwd, service=True, name=name)

    # Instant-crash probe: surface "command not found" / "port in use" NOW
    # instead of as a follow-up notification three messages later.
    await asyncio.sleep(_STARTUP_PROBE_S)
    current = bg_jobs.get(rec["id"]) or rec
    if current.get("status") != "running":
        snippet = _tail_lines(current, _SNIPPET_LINES)
        return {
            "error": (
                f"manage_processes: service exited immediately "
                f"(exit code {current.get('exit_code')}). Output:\n{snippet}"
            ),
            "exit_code": 1,
            "process": {"id": rec["id"], "status": current.get("status")},
        }

    label = name or command.splitlines()[0][:40]
    return {
        "output": (
            f"Started service `{rec['id']}`{f' ({name})' if name else ''} in {cwd}.\n"
            f"It runs detached and keeps running between turns. "
            f"Check it with {{\"action\": \"logs\", \"id\": \"{rec['id']}\"}}, "
            f"stop it with {{\"action\": \"stop\", \"id\": \"{rec['id']}\"}}. "
            f"You will be notified automatically if it exits or crashes.\n"
            f"First output:\n{_tail_lines(current, _SNIPPET_LINES)}"
        ),
        "exit_code": 0,
        "process": {
            "id": rec["id"],
            "name": label,
            "pid": rec.get("pid"),
            "cwd": cwd,
            "status": "running",
        },
    }


def _list() -> Dict[str, Any]:
    jobs = bg_jobs.refresh()
    if not jobs:
        return {"output": "No background jobs or services.", "exit_code": 0}
    records = sorted(jobs.values(), key=lambda r: r.get("started_at") or 0, reverse=True)
    lines = [_describe(rec) for rec in records]
    running = sum(1 for rec in records if rec.get("status") == "running")
    return {
        "output": f"{len(records)} background job(s), {running} running:\n" + "\n".join(lines),
        "exit_code": 0,
    }


def _logs(args: Dict[str, Any]) -> Dict[str, Any]:
    ident = str(args.get("id") or args.get("name") or "").strip()
    if not ident:
        raise ValueError('logs needs an "id" or "name" — use {"action": "list"} to find it')
    lines = args.get("lines") or 60
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 60
    rec = _find_job(ident)
    if not rec:
        raise ValueError(f"no background job or service matching '{ident}'")
    text = bg_jobs.tail(rec["id"], lines=lines) or "(no output yet)"
    return {
        "output": f"{_describe(rec)}\n--- last output ---\n{text}",
        "exit_code": 0,
    }


def _stop(args: Dict[str, Any]) -> Dict[str, Any]:
    ident = str(args.get("id") or args.get("name") or "").strip()
    if not ident:
        raise ValueError('stop needs an "id" or "name" — use {"action": "list"} to find it')
    rec = _find_job(ident)
    if not rec:
        raise ValueError(f"no background job or service matching '{ident}'")
    rec = bg_jobs.stop(rec["id"]) or rec
    return {
        "output": f"Stopped `{rec['id']}`.\n{_describe(rec)}",
        "exit_code": 0,
    }


async def manage_processes(
    content: str,
    *,
    session_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        args = _parse_args(content)
        raw_action = str(args.get("action") or "").strip().lower()
        action = _ACTION_ALIASES.get(raw_action)
        if action is None:
            raise ValueError(f'unknown action \'{raw_action}\' — use one of {", ".join(_VALID_ACTIONS)}')
        if action == "start":
            if not session_id:
                # Crash follow-ups are delivered per chat session; without one
                # a dead service would never be reported back to the agent.
                return {
                    "error": "manage_processes: starting a service requires an active chat session",
                    "exit_code": 1,
                }
            return await _start(args, session_id=session_id, workspace=workspace)
        if action == "list":
            return _list()
        if action == "logs":
            return _logs(args)
        return _stop(args)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"manage_processes: {exc}", "exit_code": 1}
    except Exception as exc:
        return {"error": f"manage_processes failed: {type(exc).__name__}: {exc}", "exit_code": 1}
