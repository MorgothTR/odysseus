import asyncio
import json

from src.agent_tools import TOOL_TAGS, ToolBlock
from src.tool_execution import execute_tool_block
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS, is_public_blocked_tool


def test_code_review_swarm_is_registered_as_builtin_tool():
    import src.agent_loop as agent_loop

    schema_names = {schema["function"]["name"] for schema in FUNCTION_TOOL_SCHEMAS}

    assert "run_code_review_swarm" in TOOL_TAGS
    assert "run_code_review_swarm" in schema_names
    assert "run_code_review_swarm" in BUILTIN_TOOL_DESCRIPTIONS
    assert "run_code_review_swarm" in agent_loop.TOOL_SECTIONS
    assert "run_code_review_swarm" in NON_ADMIN_BLOCKED_TOOLS
    assert "run_code_review_swarm" in PLAN_MODE_READONLY_TOOLS
    assert is_public_blocked_tool("run_code_review_swarm") is True


def test_code_review_swarm_function_call_preserves_structured_args():
    args = {"path": "C:/Projects/example", "goal": "review code quality", "agent_count": 5}

    block = function_call_to_tool_block("run_code_review_swarm", json.dumps(args))

    assert block is not None
    assert block.tool_type == "run_code_review_swarm"
    assert json.loads(block.content) == args


def test_code_review_swarm_aliases_are_accepted():
    block = function_call_to_tool_block(
        "code_review_swarm",
        json.dumps({"path": "C:/Projects/example"}),
    )

    assert block is not None
    assert block.tool_type == "run_code_review_swarm"


def test_code_review_swarm_keyword_hint_surfaces_tool():
    matching_hints = [
        tools for keywords, tools in ToolIndex._KEYWORD_HINTS.items()
        if "agent swarm" in keywords
    ]

    assert matching_hints
    assert "run_code_review_swarm" in matching_hints[0]


def test_code_review_swarm_blocked_for_non_admin(monkeypatch):
    monkeypatch.setattr("src.tool_execution.owner_is_admin_or_single_user", lambda owner: False)

    desc, result = asyncio.run(
        execute_tool_block(
            ToolBlock("run_code_review_swarm", json.dumps({"path": "C:/Projects/example"})),
            owner="regular-user",
        )
    )

    assert desc == "run_code_review_swarm: BLOCKED"
    assert result["exit_code"] == 1
    assert "admin users" in result["error"]
