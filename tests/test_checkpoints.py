import asyncio
import json
import os

from src.agent_tools import TOOL_TAGS
from src import checkpoints
from src.checkpoints import manage_checkpoints, record_edit, restore, list_edits, _parse
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
    is_public_blocked_tool,
)


# ── Registration ─────────────────────────────────────────────────────────────

def test_manage_checkpoints_is_registered():
    import src.agent_loop as agent_loop

    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "manage_checkpoints" in TOOL_TAGS
    assert "manage_checkpoints" in names
    assert "manage_checkpoints" in agent_loop.TOOL_SECTIONS
    assert "manage_checkpoints" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("manage_checkpoints") is True
    # Mutator (restore writes files): NOT plan-mode-readonly, IS a known mutator.
    assert "manage_checkpoints" not in PLAN_MODE_READONLY_TOOLS
    assert "manage_checkpoints" in _PLAN_MODE_KNOWN_MUTATORS


def test_aliases_and_structured_args():
    for alias in ("manage_checkpoints", "undo", "rollback", "checkpoints"):
        block = function_call_to_tool_block(alias, json.dumps({"action": "list"}))
        assert block is not None and block.tool_type == "manage_checkpoints", alias


def test_parse_bare_and_json():
    assert _parse("list")[0] == "list"
    assert _parse("undo") == ("restore", "last", 20)        # undo → restore last
    assert _parse("rollback")[0] == "restore"
    a, t, _ = _parse('{"action": "restore", "target": "last 3"}')
    assert a == "restore" and t == "last 3"


# ── Behaviour ────────────────────────────────────────────────────────────────

def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "_root", lambda: str(tmp_path / "ckpt"))
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    return ws


def test_record_and_restore_roundtrip(tmp_path, monkeypatch):
    ws = _isolate(tmp_path, monkeypatch)
    f = os.path.join(ws, "a.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("new content\n")
    cid = record_edit(f, "old content\n", "new content\n", tool="edit_file", workspace=ws)
    assert cid
    edits = list_edits(ws)
    assert len(edits) == 1 and edits[0]["rel"] == "a.py"

    res = restore("last", workspace=ws)
    assert len(res["restored"]) == 1 and not res["skipped"]
    with open(f, encoding="utf-8") as fh:
        assert fh.read() == "old content\n"   # rolled back to pre-edit state


def test_noop_edit_not_recorded(tmp_path, monkeypatch):
    ws = _isolate(tmp_path, monkeypatch)
    f = os.path.join(ws, "same.py")
    assert record_edit(f, "x\n", "x\n", tool="edit_file", workspace=ws) is None
    assert list_edits(ws) == []


def test_restore_last_n_unwinds_to_earliest_per_file(tmp_path, monkeypatch):
    ws = _isolate(tmp_path, monkeypatch)
    f = os.path.join(ws, "b.py")
    # two successive edits of the same file: "" -> "v1" -> "v2"
    record_edit(f, "", "v1\n", tool="write_file", workspace=ws)
    record_edit(f, "v1\n", "v2\n", tool="edit_file", workspace=ws)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("v2\n")
    res = restore("last 2", workspace=ws)
    assert len(res["restored"]) == 1                    # one file, restored once
    with open(f, encoding="utf-8") as fh:
        assert fh.read() == ""                          # back to the EARLIEST pre-state


def test_restore_confined_to_workspace(tmp_path, monkeypatch):
    ws = _isolate(tmp_path, monkeypatch)
    outside = str(tmp_path / "outside.txt")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("keep me")
    record_edit(outside, "secret\n", "changed\n", tool="write_file", workspace=ws)
    res = restore("last", workspace=ws)
    assert not res["restored"]
    assert res["skipped"] and "outside" in res["skipped"][0]["reason"]
    with open(outside, encoding="utf-8") as fh:
        assert fh.read() == "keep me"                   # untouched


def test_manage_checkpoints_list_then_undo(tmp_path, monkeypatch):
    ws = _isolate(tmp_path, monkeypatch)
    f = os.path.join(ws, "x.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("v2\n")
    record_edit(f, "v1\n", "v2\n", tool="edit_file", workspace=ws)

    out = asyncio.run(manage_checkpoints(json.dumps({"action": "list"}), workspace=ws))
    assert out["exit_code"] == 0 and "x.py" in out["output"]

    out2 = asyncio.run(manage_checkpoints("undo", workspace=ws))
    assert out2["exit_code"] == 0 and "Restored" in out2["output"]
    with open(f, encoding="utf-8") as fh:
        assert fh.read() == "v1\n"
