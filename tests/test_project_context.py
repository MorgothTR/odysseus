from src.project_context import (
    MAX_CONTEXT_CHARS,
    find_context_file,
    load_project_context,
)


def test_no_workspace_or_missing_returns_none(tmp_path):
    assert load_project_context(None) is None
    assert load_project_context("") is None
    assert load_project_context(str(tmp_path)) is None  # empty dir, no file
    assert load_project_context(str(tmp_path / "nope")) is None  # not a dir


def test_priority_order_prefers_dedicated_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents conventions", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude conventions", encoding="utf-8")
    # AGENTS.md wins over CLAUDE.md.
    assert find_context_file(str(tmp_path)).endswith("AGENTS.md")

    (tmp_path / ".odysseus.md").write_text("odysseus-specific guidance", encoding="utf-8")
    # The dedicated file wins over both.
    assert find_context_file(str(tmp_path)).endswith(".odysseus.md")
    out = load_project_context(str(tmp_path))
    assert "odysseus-specific guidance" in out
    assert ".odysseus.md" in out
    # Header nudges conventions + test-after-edit.
    assert "run its tests" in out.lower()


def test_large_file_is_capped_with_pointer(tmp_path):
    big = "x" * (MAX_CONTEXT_CHARS * 3)
    (tmp_path / "AGENTS.md").write_text(big, encoding="utf-8")
    out = load_project_context(str(tmp_path))
    # Body capped (plus header + truncation note), not the full 18 KB.
    assert len(out) < MAX_CONTEXT_CHARS + 600
    assert "truncated" in out
    assert "read_file" in out


def test_empty_file_returns_none(tmp_path):
    (tmp_path / "AGENTS.md").write_text("   \n  ", encoding="utf-8")
    assert load_project_context(str(tmp_path)) is None
