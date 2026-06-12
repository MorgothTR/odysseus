"""Regression: tool-block parsing must tolerate a non-string input.

`_normalize_dsml` did `if "DSML" not in text` (TypeError on None) and the public
`parse_tool_blocks`/`strip_tool_blocks` then ran regexes on it. Coercing a
non-string to "" in `_normalize_dsml` makes the whole chain safe.
"""
import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import _normalize_dsml, parse_tool_blocks, strip_tool_blocks


def test_non_string_does_not_crash():
    assert _normalize_dsml(None) == ""
    assert parse_tool_blocks(None) == []
    assert strip_tool_blocks(None) == ""


def test_plain_text_passes_through():
    assert strip_tool_blocks("hello world") == "hello world"
    assert parse_tool_blocks("no tools here") == []


def test_bare_ls_function_call_parses_as_dedicated_file_tool():
    blocks = parse_tool_blocks('ls("C:/Projects/framescan_backup/framescan")')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "ls"
    assert blocks[0].content == "C:/Projects/framescan_backup/framescan"


def test_bare_ls_shellish_line_parses_as_dedicated_file_tool():
    blocks = parse_tool_blocks('ls "C:/Projects/framescan_backup/framescan"')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "ls"
    assert blocks[0].content == "C:/Projects/framescan_backup/framescan"


def test_bare_file_tool_parser_handles_inline_code_ticks():
    blocks = parse_tool_blocks('`ls C:/Projects/framescan_backup/framescan`')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "ls"
    assert blocks[0].content == "C:/Projects/framescan_backup/framescan"


def test_bare_file_tool_parser_supports_json_style_glob():
    blocks = parse_tool_blocks('glob({"pattern": "**/*.py", "path": "C:/Projects/example"})')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "glob"
    assert '"pattern": "**/*.py"' in blocks[0].content
    assert '"path": "C:/Projects/example"' in blocks[0].content


def test_shellish_recursive_grep_parses_as_dedicated_grep_json():
    blocks = parse_tool_blocks('grep -r "TODO" C:/Projects/framescan_backup/framescan')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "grep"
    assert '"pattern": "TODO"' in blocks[0].content
    assert '"path": "C:/Projects/framescan_backup/framescan"' in blocks[0].content


def test_shellish_grep_with_glob_filter_parses_as_dedicated_grep_json():
    blocks = parse_tool_blocks('grep -i --glob "*.md" "todo" C:/Projects/example')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "grep"
    assert '"pattern": "todo"' in blocks[0].content
    assert '"path": "C:/Projects/example"' in blocks[0].content
    assert '"glob": "*.md"' in blocks[0].content
    assert '"ignore_case": true' in blocks[0].content


def test_ls_wildcard_path_parses_as_glob_tool():
    blocks = parse_tool_blocks('ls C:/Projects/framescan_backup/framescan/*.py')

    assert len(blocks) == 1
    assert blocks[0].tool_type == "glob"
    assert '"pattern": "*.py"' in blocks[0].content
    assert '"path": "C:/Projects/framescan_backup/framescan"' in blocks[0].content


def test_shellish_file_parser_rejects_pipes_and_redirects():
    assert parse_tool_blocks(
        'grep "TODO" C:/Projects/example/*.md 2>/dev/null || echo "No files matched"'
    ) == []
    assert parse_tool_blocks('ls C:/Projects/example/*.md | head -50') == []


def test_bare_file_tool_parser_does_not_add_power_tool_syntax():
    assert parse_tool_blocks('bash("dir C:/Projects/example /b")') == []
    assert parse_tool_blocks('python("print(1)")') == []


def test_bare_file_tool_parser_ignores_prose_mentions():
    assert parse_tool_blocks('I will use ls("C:/Projects/example") now.') == []
