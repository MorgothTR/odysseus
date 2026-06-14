"""read_file should find the path no matter which key the model used.

Models (kimi etc.) sometimes emit a native read_file call with the path under
`file`/`file_path`/`filename` instead of `path`, which produced empty content and
a wasted 'path is required' retry round. The args→content mapping now tolerates
those keys (and any string value as a last resort)."""

import json

import src.agent_tools  # noqa: F401  — prime the agent_tools↔tool_schemas import cluster
from src.tool_schemas import function_call_to_tool_block


def test_read_file_accepts_alternate_path_keys():
    for key in ("path", "file", "file_path", "filename", "filepath", "filePath"):
        b = function_call_to_tool_block("read_file", json.dumps({key: "README.md"}))
        assert b is not None and b.tool_type == "read_file", key
        assert b.content == "README.md", f"key={key} -> {b.content!r}"


def test_read_file_falls_back_to_first_string_arg():
    # The model invents a key; read_file's only other args are offset/limit (ints),
    # so the lone string value IS the path.
    b = function_call_to_tool_block("read_file", json.dumps({"target": "src/x.py"}))
    assert b is not None and b.content == "src/x.py"


def test_read_file_plain_path_still_works():
    b = function_call_to_tool_block("read_file", json.dumps({"path": "a/b.py"}))
    assert b.content == "a/b.py"


def test_read_file_line_range_preserves_alt_key_as_json():
    # With a line range the content is JSON; the handler resolves the alt key too.
    b = function_call_to_tool_block("read_file", json.dumps({"file": "a.py", "limit": 50}))
    d = json.loads(b.content)
    assert d.get("file") == "a.py" and d.get("limit") == 50
