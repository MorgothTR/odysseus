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
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

Candidate = Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]

# Conservative default for a sub-agent's tool-loop budget. Enough rounds to list
# a tree, grep a few patterns, read the suspicious files, and write findings —
# not enough to wander. Callers raise it deliberately.
DEFAULT_SUBAGENT_ROUNDS = 6

# Read-only navigation tools: the safe default toolset for a sub-agent. No
# bash/python/write/edit/spawn — a sub-agent investigates, it does not mutate.
READONLY_TOOLSET: Set[str] = frozenset({"read_file", "grep", "glob", "ls"})


def _all_tool_names() -> Set[str]:
    """Every built-in tool name, so the allowlist can be inverted to a denylist."""
    from src.agent_loop import TOOL_SECTIONS

    return set(TOOL_SECTIONS.keys())


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
