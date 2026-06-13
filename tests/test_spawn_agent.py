import asyncio
import json

import pytest

from src.agent_tools import TOOL_TAGS
from src.spawn_agent import MAX_SPAWN_TASKS, spawn_agent
from src.subagents import READONLY_TOOLSET, SAFE_CHILD_TOOLS, confine_child_toolset
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
    is_public_blocked_tool,
)


# ── Registration ─────────────────────────────────────────────────────────────

def test_spawn_agent_is_registered():
    import src.agent_loop as agent_loop

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "spawn_agent" in TOOL_TAGS
    assert "spawn_agent" in schema_names
    assert "spawn_agent" in agent_loop.TOOL_SECTIONS
    assert "spawn_agent" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("spawn_agent") is True
    # Spawning is a side effect — plan-mode mutator, never read-only.
    assert "spawn_agent" in _PLAN_MODE_KNOWN_MUTATORS
    assert "spawn_agent" not in PLAN_MODE_READONLY_TOOLS


def test_spawn_agent_aliases_and_structured_args():
    block = function_call_to_tool_block("delegate", json.dumps({"goal": "investigate X"}))
    assert block is not None and block.tool_type == "spawn_agent"

    block2 = function_call_to_tool_block("spawn_agent", json.dumps({"tasks": [{"goal": "a"}, {"goal": "b"}]}))
    assert block2 is not None and block2.tool_type == "spawn_agent"


# ── Confinement (the depth limit) ────────────────────────────────────────────

def test_child_toolset_is_confined_and_cannot_spawn():
    # Default is read-only nav.
    assert confine_child_toolset(None) == set(READONLY_TOOLSET)
    # spawn_agent / bash / write are stripped no matter what is requested — this
    # is the flat one-level depth guard: a child can't get the tool to recurse.
    dangerous = confine_child_toolset({"spawn_agent", "bash", "write_file", "read_file", "grep"})
    assert dangerous == {"read_file", "grep"}
    assert "spawn_agent" not in SAFE_CHILD_TOOLS
    assert "bash" not in SAFE_CHILD_TOOLS
    # Web read tools are allowed for research children.
    assert confine_child_toolset({"web_search"}) == {"web_search"}
    # A fully-rejected request falls back to read-only nav, never empty.
    assert confine_child_toolset({"bash", "spawn_agent"}) == set(READONLY_TOOLSET)


# ── Behaviour (driver mocked) ────────────────────────────────────────────────

def _patch_candidates(monkeypatch):
    monkeypatch.setattr(
        "src.spawn_agent.resolve_subagent_candidates",
        lambda model, owner: ([("http://local/v1/chat/completions", "m", {})], "m"),
    )


def test_spawn_agent_fans_out_in_parallel(monkeypatch):
    _patch_candidates(monkeypatch)
    seen = []

    async def fake_run_subagent(*, goal, system_prompt, candidate, root, toolset, fallbacks, max_rounds, owner, label):
        seen.append({"label": label, "toolset": set(toolset), "max_rounds": max_rounds, "root": root})
        return f"summary for {label}"

    monkeypatch.setattr("src.spawn_agent.run_subagent", fake_run_subagent)

    result = asyncio.run(spawn_agent(
        json.dumps({"tasks": [
            {"goal": "audit auth.py", "label": "auth"},
            {"goal": "map sessions", "label": "sessions", "tools": ["read_file", "web_search"]},
        ], "max_rounds": 10}),
        owner="admin",
        workspace="C:/proj",
    ))

    assert result["exit_code"] == 0
    assert result["spawn"]["tasks"] == 2
    assert result["spawn"]["completed"] == 2
    labels = {s["label"] for s in seen}
    assert labels == {"auth", "sessions"}
    # Per-task toolset confinement: the web task got web_search, the other didn't.
    by_label = {s["label"]: s for s in seen}
    assert by_label["auth"]["toolset"] == set(READONLY_TOOLSET)
    assert "web_search" in by_label["sessions"]["toolset"]
    assert all(s["max_rounds"] == 10 for s in seen)
    assert all(s["root"] == "C:/proj" for s in seen)
    assert "summary for auth" in result["output"]


def test_spawn_agent_single_task_form(monkeypatch):
    _patch_candidates(monkeypatch)

    async def fake_run_subagent(**kwargs):
        return "did the thing"

    monkeypatch.setattr("src.spawn_agent.run_subagent", fake_run_subagent)
    result = asyncio.run(spawn_agent(json.dumps({"goal": "go do a thing"}), owner="admin"))
    assert result["exit_code"] == 0
    assert result["spawn"]["tasks"] == 1
    assert result["spawn"]["results"][0]["status"] == "completed"


def test_spawn_agent_requires_a_goal(monkeypatch):
    _patch_candidates(monkeypatch)
    result = asyncio.run(spawn_agent(json.dumps({"context": "no goal here"}), owner="admin"))
    assert result["exit_code"] == 1
    assert "goal" in result["error"]


def test_spawn_agent_caps_task_count(monkeypatch):
    _patch_candidates(monkeypatch)
    tasks = [{"goal": f"task {i}"} for i in range(MAX_SPAWN_TASKS + 2)]
    result = asyncio.run(spawn_agent(json.dumps({"tasks": tasks}), owner="admin"))
    assert result["exit_code"] == 1
    assert "too many tasks" in result["error"]


def test_spawn_agent_one_child_failure_does_not_sink_others(monkeypatch):
    _patch_candidates(monkeypatch)

    async def flaky(*, goal, label, **kwargs):
        if label == "bad":
            raise RuntimeError("endpoint exploded")
        return "ok"

    monkeypatch.setattr("src.spawn_agent.run_subagent", flaky)
    result = asyncio.run(spawn_agent(
        json.dumps({"tasks": [{"goal": "x", "label": "good"}, {"goal": "y", "label": "bad"}]}),
        owner="admin",
    ))
    assert result["exit_code"] == 0
    statuses = {r["label"]: r["status"] for r in result["spawn"]["results"]}
    assert statuses == {"good": "completed", "bad": "error"}
