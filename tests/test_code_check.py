import asyncio
import json

import pytest

from src.agent_tools import TOOL_TAGS
from src.code_check import check_code
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
    is_public_blocked_tool,
)


# ── Registration ─────────────────────────────────────────────────────────────

def test_check_code_is_registered():
    import src.agent_loop as agent_loop

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "check_code" in TOOL_TAGS
    assert "check_code" in schema_names
    assert "check_code" in agent_loop.TOOL_SECTIONS
    assert "check_code" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("check_code") is True
    # Read-only inspection — allowed in plan mode, NOT a mutator.
    assert "check_code" in PLAN_MODE_READONLY_TOOLS
    assert "check_code" not in _PLAN_MODE_KNOWN_MUTATORS


def test_check_code_aliases_and_structured_args():
    for alias in ("check_code", "lint", "typecheck", "diagnostics"):
        block = function_call_to_tool_block(alias, json.dumps({"path": "src/x.py"}))
        assert block is not None and block.tool_type == "check_code", alias


# ── Behaviour (real ruff) ────────────────────────────────────────────────────

def test_check_code_finds_python_errors(tmp_path):
    (tmp_path / "bad.py").write_text("import os\nx = undefined_thing\n", encoding="utf-8")
    result = asyncio.run(check_code("bad.py", workspace=str(tmp_path)))
    assert result["exit_code"] == 0
    diag = result["diagnostics"]
    assert "ruff" in diag["checker"].lower()
    codes = {i["code"] for i in diag["issues"]}
    assert "F821" in codes  # undefined name
    assert "F401" in codes  # unused import
    # Human-readable body has file:line references.
    assert "bad.py:" in result["output"]


def test_check_code_clean_file_reports_no_issues(tmp_path):
    (tmp_path / "good.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    result = asyncio.run(check_code("good.py", workspace=str(tmp_path)))
    assert result["exit_code"] == 0
    assert result["diagnostics"]["total"] == 0
    assert "No issues found" in result["output"]


def test_check_code_confined_to_workspace(tmp_path):
    # A path outside the workspace must be rejected by the confinement layer.
    result = asyncio.run(check_code("C:/Windows/System32", workspace=str(tmp_path)))
    assert result["exit_code"] == 1


def test_check_code_requires_a_path(tmp_path):
    result = asyncio.run(check_code("{}", workspace=str(tmp_path)))
    assert result["exit_code"] == 1
    assert "path" in result["error"]


def test_check_code_unknown_language(tmp_path):
    (tmp_path / "notes.txt").write_text("just some text", encoding="utf-8")
    result = asyncio.run(check_code("notes.txt", workspace=str(tmp_path)))
    assert result["exit_code"] == 1
    assert "no checker" in result["error"].lower()


def test_check_code_keyword_hint_surfaces_tool():
    from src.tool_index import ToolIndex

    ti = object.__new__(ToolIndex)
    ti.retrieve = lambda q, k=8: []
    for query in ["check the code for errors", "lint this file", "any type errors?"]:
        assert "check_code" in ToolIndex.get_tools_for_query(ti, query, 8), query
