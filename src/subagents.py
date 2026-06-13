"""Headless sub-agent driver.

Runs a confined, bounded agent loop with no client attached and returns only the
final text. This is the shared core for two phase-20 features:

  * the code review swarm's *agentic* reviewers (phase 20A) — each reviewer is a
    sub-agent that explores the repo with read-only tools instead of reviewing a
    fixed snapshot, and
  * the future `spawn_agent` tool (phase 20B).

It drives `stream_agent_loop` the same way `task_scheduler._run_agent_loop`
does headlessly: accumulate the streamed text, capture tool output as a
fallback, and grace-summarize if the model exhausts its rounds without writing a
final answer. It deliberately does NOT create sessions, spawn detached
processes, or stream to a UI — callers that need those compose them on top.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

Candidate = Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]

# Conservative default for a sub-agent's tool-loop budget. Enough rounds to list
# a tree, grep a few patterns, read the suspicious files, and write findings —
# not enough to wander. Callers raise it deliberately.
DEFAULT_SUBAGENT_ROUNDS = 6

# Read-only navigation tools: the safe default toolset for a sub-agent. No
# bash/python/write/edit/spawn — a sub-agent investigates, it does not mutate.
READONLY_TOOLSET: Set[str] = frozenset({"read_file", "grep", "glob", "ls"})

# The widest toolset a sub-agent may be granted (phase 20B, leaf-only): read-only
# code navigation plus read-only web lookup for research tasks. Everything else —
# bash, python, file writes, model serving, messaging, and crucially spawn_agent
# itself — is excluded, which is what enforces the flat one-level depth limit:
# a child literally cannot be handed the tool to spawn grandchildren.
SAFE_CHILD_TOOLS: Set[str] = frozenset(READONLY_TOOLSET | {"web_search", "web_fetch"})


def _all_tool_names() -> Set[str]:
    """Every built-in tool name, so the allowlist can be inverted to a denylist."""
    from src.agent_loop import TOOL_SECTIONS

    return set(TOOL_SECTIONS.keys())


def confine_child_toolset(requested: Optional[Set[str]]) -> Set[str]:
    """Intersect a requested toolset with SAFE_CHILD_TOOLS; default to read-only.

    A child can never be granted a tool outside the safe set — not bash, not a
    write tool, and not spawn_agent (so it cannot spawn grandchildren). An empty
    or fully-rejected request falls back to the read-only navigation set."""
    if not requested:
        return set(READONLY_TOOLSET)
    confined = {t for t in requested if t in SAFE_CHILD_TOOLS}
    return confined or set(READONLY_TOOLSET)


def resolve_subagent_candidates(model_spec: str, owner: Optional[str]) -> Tuple[List[Candidate], str]:
    """Resolve the model endpoint(s) a sub-agent runs on: an explicit override,
    else the configured Utility/Default model plus its fallback chain."""
    if model_spec:
        from src.ai_interaction import _resolve_model

        url, model, headers = _resolve_model(model_spec, owner=owner)
        return [(url, model, headers)], model

    from src.endpoint_resolver import resolve_endpoint, resolve_utility_fallback_candidates

    url, model, headers = resolve_endpoint("utility", owner=owner)
    candidates: List[Candidate] = []
    if url and model:
        candidates.append((url, model, headers))
    candidates.extend(resolve_utility_fallback_candidates(owner=owner))
    if not candidates:
        raise ValueError("No utility/default model endpoint is configured for sub-agents")
    return candidates, str(candidates[0][1] or "")


# ── Active sub-agent registry ────────────────────────────────────────────────
# Lightweight in-memory record of which sub-agents are running right now. Same
# shape as bg_jobs' store; drives observability (and, in a later phase, a live
# sub-agent tree + interrupt). Cleared on process restart — sub-agents are
# in-flight work, not durable state.
_active_subagents: Dict[str, Dict[str, Any]] = {}
_active_lock = threading.Lock()


def _register_subagent(label: str, model: Optional[str]) -> str:
    sid = uuid.uuid4().hex[:12]
    with _active_lock:
        _active_subagents[sid] = {"id": sid, "label": label[:80], "model": model, "status": "running"}
    return sid


def _unregister_subagent(sid: str) -> None:
    with _active_lock:
        _active_subagents.pop(sid, None)


def list_active_subagents() -> List[Dict[str, Any]]:
    with _active_lock:
        return [dict(rec) for rec in _active_subagents.values()]


async def run_subagent(
    *,
    goal: str,
    system_prompt: str,
    candidate: Candidate,
    root: Optional[str] = None,
    toolset: Set[str] = READONLY_TOOLSET,
    fallbacks: Optional[List[Candidate]] = None,
    max_rounds: int = DEFAULT_SUBAGENT_ROUNDS,
    owner: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Drive a confined, bounded agent loop and return its final text.

    The sub-agent gets a fresh context (``system_prompt`` + ``goal``), only the
    tools in ``toolset`` (everything else disabled so neither RAG nor the
    always-available set can hand it bash/write/spawn), and — when ``root`` is
    set — file access confined to that folder (same policy as the file tools).

    Returns the model's final text, or a grace summary of its tool activity if
    it ran out of rounds without a written answer (so the caller always gets
    something actionable). Returns "" only if even the grace summary fails.
    """
    from src.agent_loop import stream_agent_loop

    url, model, headers = candidate
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]
    # Allowlist -> denylist: disable everything not explicitly granted. This is
    # what actually confines the sub-agent — stream_agent_loop unions the
    # always-available set (bash, write_file, ...) into the prompt, so a bare
    # relevant_tools allowlist is not enough on its own.
    disabled = _all_tool_names() - set(toolset)

    full_text = ""
    tool_results: List[str] = []
    sid = _register_subagent(label or goal.splitlines()[0] if goal else "subagent", model)
    try:
        async for event_str in stream_agent_loop(
            endpoint_url=url,
            model=model,
            messages=messages,
            headers=headers or {},
            max_rounds=max_rounds,
            relevant_tools=set(toolset),
            disabled_tools=disabled,
            workspace=root,
            owner=owner,
            fallbacks=fallbacks or [],
        ):
            if not event_str.startswith("data: ") or event_str.startswith("data: [DONE]"):
                continue
            try:
                data = json.loads(event_str[6:])
            except (json.JSONDecodeError, ValueError):
                continue
            if "delta" in data:
                full_text += data["delta"]
            elif data.get("type") == "tool_output":
                summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                if isinstance(summary, str) and summary.strip():
                    tool_results.append(f"[{data.get('tool', '?')}] {summary[:400]}")
    finally:
        _unregister_subagent(sid)

    full_text = full_text.strip()
    if full_text:
        return full_text

    # Exhausted rounds (or never wrote a final answer) — grace-summarize so the
    # caller always gets something, mirroring task_scheduler._run_agent_loop.
    try:
        from src.llm_core import llm_call_async_with_fallback

        grace = "You ran out of steps. "
        if tool_results:
            grace += "Here is what your tools returned:\n" + "\n".join(tool_results[-6:])
        else:
            grace += "No tool results were captured."
        grace += "\n\nSummarize your findings concisely."
        candidates = [candidate] + list(fallbacks or [])
        text = await llm_call_async_with_fallback(
            candidates,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": grace},
            ],
            timeout=60,
            think=False,
        )
        return (text or "").strip()
    except Exception as exc:
        logger.warning("subagent grace summary failed: %s", exc)
        return ""
