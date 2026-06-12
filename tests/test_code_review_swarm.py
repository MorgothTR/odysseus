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
