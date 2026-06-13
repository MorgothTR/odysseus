import asyncio
import json

import pytest

from src.agent_tools import TOOL_TAGS
from src.code_search import semantic_code_search, _chunk_lines, _parse_args, _ws_id
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
    is_public_blocked_tool,
)


# ── Registration (nine hooks) ────────────────────────────────────────────────

def test_semantic_code_search_is_registered():
    import src.agent_loop as agent_loop

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "semantic_code_search" in TOOL_TAGS
    assert "semantic_code_search" in schema_names
    assert "semantic_code_search" in agent_loop.TOOL_SECTIONS
    assert "semantic_code_search" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("semantic_code_search") is True
    # Read-only search — allowed in plan mode, never a mutator.
    assert "semantic_code_search" in PLAN_MODE_READONLY_TOOLS
    assert "semantic_code_search" not in _PLAN_MODE_KNOWN_MUTATORS


def test_semantic_code_search_aliases_and_structured_args():
    for alias in ("semantic_code_search", "code_search", "search_code", "find_code"):
        block = function_call_to_tool_block(alias, json.dumps({"query": "auth"}))
        assert block is not None and block.tool_type == "semantic_code_search", alias


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_parse_args_json_and_bare_query():
    q, k, p = _parse_args('{"query": "load settings", "k": 5, "path": "src"}')
    assert q == "load settings" and k == 5 and p == "src"
    q2, k2, p2 = _parse_args("where are tokens validated")
    assert q2 == "where are tokens validated" and p2 == ""
    _, k3, _ = _parse_args('{"query": "x", "k": 999}')   # clamped to the cap
    assert 1 <= k3 <= 15


def test_chunk_lines_carry_line_ranges_and_overlap():
    text = "\n".join(f"line {i}" for i in range(1, 201))
    chunks = list(_chunk_lines(text))
    assert chunks
    assert chunks[0][0] == 1                      # first chunk is 1-based
    assert chunks[1][0] <= chunks[0][1]           # windows overlap
    assert all(s <= e for (s, e, _t) in chunks)   # start <= end always


def test_ws_id_is_stable_and_path_specific(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert _ws_id(str(a)) == _ws_id(str(a))
    assert _ws_id(str(a)) != _ws_id(str(b))


# ── Guard paths (no embeddings needed — return before indexing) ───────────────

def test_semantic_code_search_requires_a_query(tmp_path):
    res = asyncio.run(semantic_code_search("{}", workspace=str(tmp_path)))
    assert res["exit_code"] == 1
    assert "query" in res["error"]


def test_semantic_code_search_confined_to_workspace(tmp_path):
    res = asyncio.run(semantic_code_search(
        json.dumps({"query": "anything", "path": "C:/Windows/System32"}),
        workspace=str(tmp_path),
    ))
    assert res["exit_code"] == 1


# ── End-to-end (real embeddings; skips cleanly if the backend isn't available) ─

def test_semantic_code_search_finds_relevant_code(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def validate_token(token):\n"
        "    '''Verify a JWT's signature and expiry before trusting the caller.'''\n"
        "    return verify_jwt_signature(token)\n",
        encoding="utf-8",
    )
    (tmp_path / "geometry.py").write_text(
        "def area_of_circle(radius):\n"
        "    import math\n"
        "    return math.pi * radius * radius\n",
        encoding="utf-8",
    )
    res = asyncio.run(semantic_code_search(
        json.dumps({"query": "where are authentication tokens validated"}),
        workspace=str(tmp_path),
    ))
    if res.get("error") == "embeddings unavailable":
        pytest.skip("no embedding backend available in this environment")
    assert res["exit_code"] == 0, res
    files = [r["file"] for r in res["results"]]
    assert any("auth.py" in f for f in files), files
    # The auth file should rank first for an auth query.
    assert res["results"][0]["file"].endswith("auth.py"), res["results"]
    # Results carry real line ranges.
    assert res["results"][0]["start_line"] == 1
