"""The `spawn_agent` tool (phase 20B) — delegate tasks to sub-agents.

The agent can hand off one or more focused tasks to sub-agents, each running in
its own fresh context with a confined, read-only toolset, and get back only
their final summaries — so the parent's context stays lean while children burn
context doing the work.

v1 is deliberately tight (see docs/dev/phase-20-agent-harness.md):
  * leaf-only — children CANNOT spawn their own sub-agents (spawn_agent is never
    in a child's toolset), giving a flat one-level depth limit with no counter,
  * read-only by default — children get nav/web-read tools, never bash/write,
  * parallel fan-out under a small concurrency cap,
  * round-capped per child,
  * summary-only returns.

It is built on src.subagents.run_subagent (the same confined headless driver the
code review swarm's agentic reviewers use), so confinement and result handling
are shared and already proven.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from src.subagents import (
    DEFAULT_SUBAGENT_ROUNDS,
    confine_child_toolset,
    resolve_subagent_candidates,
    run_subagent,
)

# Config-authoritative caps — a model-supplied value never raises these.
MAX_CONCURRENT_CHILDREN = 3
MAX_SPAWN_TASKS = 6
# Sized from a hermes-agent reference run on the same task: thorough audit
# sub-agents naturally finished in ~19-27 rounds (reading 7-10 files), never
# hitting a cap. A tight budget truncates the work mid-audit, so the default is
# generous (children stop when done) and the ceiling is a runaway backstop.
DEFAULT_SPAWN_ROUNDS = 20
MAX_SPAWN_ROUNDS = 40

_SYSTEM_PROMPT = (
    "You are a sub-agent delegated a single focused task by a parent agent. You "
    "have a fresh context and NO access to the parent's conversation, so work "
    "only from the goal and context you are given.\n"
    "Your ONLY tools are read_file, grep, glob, ls (and possibly "
    "web_search/web_fetch). bash, python, and any write/edit tools are NOT "
    "available — do not attempt them, it only wastes your limited steps.\n"
    "read_file truncates large files. If a file is truncated, call read_file "
    "again with an offset to page through the REST — do not stop at the first "
    'page (e.g. {"path": "big.py", "offset": 400}). Use grep to jump to '
    "relevant lines, but when the task targets a specific file, read that whole "
    "file before concluding.\n"
    "Be THOROUGH on exactly what you were asked. If the task is to audit or "
    "review a file, read the ENTIRE file and address every aspect the goal "
    "names (bugs, edge cases, leaks, error handling) with file:line references "
    "— do not answer from a partial read, and do not wander into unrelated "
    "code. When you have genuinely covered the goal, STOP and write the answer. "
    "Return a concise, self-contained deliverable the parent can use directly — "
    "it sees only your final message, not your steps. Do not narrate your "
    "process; give the result."
)


def _clamp_rounds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SPAWN_ROUNDS
    return max(1, min(MAX_SPAWN_ROUNDS, parsed))


def _subagent_timeout() -> Optional[float]:
    """Per-child wall-clock cap from settings (0/unset disables it)."""
    try:
        from src.settings import get_setting

        secs = float(get_setting("subagent_timeout_seconds", 600) or 0)
    except Exception:
        secs = 600.0
    return secs if secs > 0 else None


def _normalize_tasks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept either a single task ({goal, context, tools}) or {"tasks": [...]}."""
    raw_tasks = data.get("tasks")
    if isinstance(raw_tasks, list) and raw_tasks:
        items = raw_tasks
    else:
        # Single-task form: the top-level object IS the task.
        items = [data]

    tasks: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Accept the field names a model reaches for — goal/task/prompt/
        # instructions/description all mean "what to do". A wrong key used to
        # bounce the call ("needs a goal") before the model found the right one.
        goal = str(
            item.get("goal") or item.get("task") or item.get("prompt")
            or item.get("instructions") or item.get("description") or ""
        ).strip()
        if not goal:
            continue
        context = str(item.get("context") or "").strip()
        requested = item.get("tools") or item.get("toolset")
        toolset = confine_child_toolset(set(requested) if isinstance(requested, list) else None)
        label = str(item.get("label") or item.get("name") or goal).strip()[:60]
        tasks.append({"goal": goal, "context": context, "toolset": toolset, "label": label})
    return tasks


def _build_goal(task: Dict[str, Any]) -> str:
    goal = task["goal"]
    if task["context"]:
        return f"Task:\n{goal}\n\nContext you need (the parent has no memory to share beyond this):\n{task['context']}"
    return f"Task:\n{goal}"


async def spawn_agent(
    content: str,
    *,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    del session_id  # children are summary-only in v1; no per-child sessions yet.
    start = time.time()
    try:
        raw = (content or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {"goal": raw}
        if not isinstance(data, dict):
            raise ValueError("spawn_agent expects a JSON object")

        tasks = _normalize_tasks(data)
        if not tasks:
            return {
                "error": 'spawn_agent needs at least one task with a "goal" (a single object, or {"tasks": [...]})',
                "exit_code": 1,
            }
        if len(tasks) > MAX_SPAWN_TASKS:
            return {
                "error": f"spawn_agent: too many tasks ({len(tasks)}); the limit is {MAX_SPAWN_TASKS} per call",
                "exit_code": 1,
            }

        max_rounds = _clamp_rounds(data.get("max_rounds"))
        candidates, selected_model = resolve_subagent_candidates(str(data.get("model") or "").strip(), owner)
        timeout = _subagent_timeout()

        sem = asyncio.Semaphore(MAX_CONCURRENT_CHILDREN)

        async def run_one(index: int, task: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    summary = await run_subagent(
                        goal=_build_goal(task),
                        system_prompt=_SYSTEM_PROMPT,
                        candidate=candidates[0],
                        root=workspace,
                        toolset=task["toolset"],
                        fallbacks=candidates[1:],
                        max_rounds=max_rounds,
                        owner=owner,
                        label=task["label"],
                        timeout=timeout,
                    )
                except Exception as exc:  # one child failing must not sink the rest
                    return {"task_index": index, "label": task["label"], "status": "error",
                            "summary": f"sub-agent failed: {type(exc).__name__}: {exc}"}
                summary = (summary or "").strip()
                return {
                    "task_index": index,
                    "label": task["label"],
                    "status": "completed" if summary else "empty",
                    "summary": summary or "(sub-agent produced no output)",
                    "tools": sorted(task["toolset"]),
                }

        results = await asyncio.gather(*(run_one(i, t) for i, t in enumerate(tasks)))
        completed = sum(1 for r in results if r["status"] == "completed")

        return {
            "output": _render_report(results, selected_model),
            "exit_code": 0,
            "spawn": {
                "tasks": len(tasks),
                "completed": completed,
                "model": selected_model,
                "max_rounds": max_rounds,
                "duration_s": round(time.time() - start, 2),
                "results": results,
            },
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"spawn_agent: {exc}", "exit_code": 1}
    except Exception as exc:
        return {"error": f"spawn_agent failed: {type(exc).__name__}: {exc}", "exit_code": 1}


def _render_report(results: List[Dict[str, Any]], model: str) -> str:
    parts = [f"# Sub-agents ({len(results)}, model: {model or 'utility/default'})\n"]
    for r in results:
        parts.append(f"## [{r['task_index']}] {r['label']} — {r['status']}\n{r['summary']}")
    return "\n\n".join(parts)
