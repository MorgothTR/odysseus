import json

import pytest

from src.code_review_swarm import run_code_review_swarm


async def _fake_llm(_candidates, messages, *, max_tokens):
    user = messages[-1]["content"]
    if "Reviewer outputs:" in user:
        return "## Summary\nSynthesized swarm findings."
    role = "reviewer"
    for line in user.splitlines():
        if line.startswith("Your specialist role:"):
            role = line.split(":", 1)[1].strip()
            break
    return f"- {role}: no high-risk issues found in the supplied snapshot."


@pytest.mark.asyncio
async def test_code_review_swarm_defaults_to_five_read_only_reviewers(monkeypatch, tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "README.md").write_text("# Example\n", encoding="utf-8")
    before = {p.relative_to(repo).as_posix() for p in repo.rglob("*")}

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.code_review_swarm._call_llm", _fake_llm)

    result = await run_code_review_swarm(
        json.dumps({"path": str(repo), "goal": "Review code quality."}),
        owner="admin",
    )

    after = {p.relative_to(repo).as_posix() for p in repo.rglob("*")}
    assert result["exit_code"] == 0
    assert result["swarm"]["agent_count"] == 5
    assert result["swarm"]["read_only"] is True
    assert "Code Review Swarm" in result["output"]
    assert before == after


@pytest.mark.asyncio
async def test_code_review_swarm_caps_agent_count_at_ten(monkeypatch, tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.code_review_swarm._call_llm", _fake_llm)

    result = await run_code_review_swarm(
        json.dumps({"path": str(repo), "agent_count": 50}),
        owner="admin",
    )

    assert result["exit_code"] == 0
    assert result["swarm"]["agent_count"] == 10
    assert len(result["swarm"]["agents"]) == 10


@pytest.mark.asyncio
async def test_code_review_swarm_skips_sensitive_files_before_llm(monkeypatch, tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (repo / ".env").write_text("API_KEY=super-secret\n", encoding="utf-8")
    (repo / "private.pem").write_text("SECRET KEY MATERIAL\n", encoding="utf-8")
    captured_prompts = []

    async def fake_llm(_candidates, messages, *, max_tokens):
        captured_prompts.append(messages[-1]["content"])
        return "ok"

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.code_review_swarm._call_llm", fake_llm)

    result = await run_code_review_swarm(json.dumps({"path": str(repo)}), owner="admin")

    joined = "\n".join(captured_prompts)
    assert result["exit_code"] == 0
    assert result["swarm"]["sensitive_files_skipped"] >= 2
    assert "super-secret" not in joined
    assert "SECRET KEY MATERIAL" not in joined


def test_snapshot_chars_arg_widens_and_clamps():
    from src.code_review_swarm import (
        MAX_SNAPSHOT_CHARS,
        MAX_SNAPSHOT_CHARS_CEILING,
        MAX_SNIPPET_CHARS_CEILING,
        _parse_args,
    )

    default = _parse_args(json.dumps({"path": "C:/x"}))
    assert default.snapshot_chars == MAX_SNAPSHOT_CHARS
    assert default.snippet_chars == 3_500

    wide = _parse_args(json.dumps({"path": "C:/x", "snapshot_chars": 150_000}))
    assert wide.snapshot_chars == 150_000
    # Per-file cap scales with the budget unless pinned explicitly.
    assert wide.snippet_chars == 15_000

    clamped = _parse_args(json.dumps({"path": "C:/x", "snapshot_chars": 10_000_000, "snippet_chars": 99_999}))
    assert clamped.snapshot_chars == MAX_SNAPSHOT_CHARS_CEILING
    assert clamped.snippet_chars == MAX_SNIPPET_CHARS_CEILING


def test_snapshot_budget_controls_collected_text(tmp_path):
    from src.code_review_swarm import _collect_snapshot

    repo = tmp_path / "project"
    repo.mkdir()
    for i in range(8):
        (repo / f"mod_{i}.py").write_text(f"# module {i}\n" + ("x = 1\n" * 3000), encoding="utf-8")

    small = _collect_snapshot(str(repo), snapshot_chars=8_000, snippet_chars=4_000)
    large = _collect_snapshot(str(repo), snapshot_chars=120_000, snippet_chars=12_000)

    small_total = sum(len(s.text) for s in small.samples)
    large_total = sum(len(s.text) for s in large.samples)
    assert small_total <= 8_000 + 100  # budget + truncation marker slack
    assert large_total > small_total * 4
    assert max(len(s.text) for s in large.samples) > 4_000  # deeper per-file excerpts


@pytest.mark.asyncio
async def test_code_review_swarm_reports_unallowed_path(monkeypatch, tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")

    other_root = tmp_path / "other"
    other_root.mkdir()
    monkeypatch.setattr("src.tool_execution._tool_path_roots", lambda: [str(other_root)])

    result = await run_code_review_swarm(json.dumps({"path": str(repo)}), owner="admin")

    assert result["exit_code"] == 1
    assert "outside the allowed roots" in result["error"] or "not an existing directory" in result["error"]
