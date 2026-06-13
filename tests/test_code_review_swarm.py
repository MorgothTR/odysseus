import json
import os

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


def _role_of(messages):
    """Pull the reviewer role out of a review prompt, or None for synthesis."""
    user = messages[-1]["content"]
    for line in user.splitlines():
        if line.startswith("Your specialist role:"):
            return line.split(":", 1)[1].strip()
    return None


@pytest.mark.asyncio
async def test_swarm_synthesizes_from_surviving_reviewers(monkeypatch, tmp_path):
    # Some reviewers return empty (thinking model burned its budget); the swarm
    # must synthesize from the survivors and report the shortfall, not break.
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    empty_roles = {"security", "tests"}
    synth_inputs = {}

    async def fake_llm(_candidates, messages, *, max_tokens):
        role = _role_of(messages)
        if role is None:  # synthesis call
            synth_inputs["text"] = messages[-1]["content"]
            return "## Summary\nSynthesized from the reviewers that responded."
        if role in empty_roles:
            return "   "  # whitespace-only → treated as empty
        return f"- {role}: a concrete finding with evidence."

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.code_review_swarm._call_llm", fake_llm)

    result = await run_code_review_swarm(json.dumps({"path": str(repo)}), owner="admin")

    assert result["exit_code"] == 0
    swarm = result["swarm"]
    assert swarm["reviewers_succeeded"] == 3
    assert set(swarm["reviewers_failed"]) == empty_roles
    # Header is honest about the shortfall.
    assert "3/5 produced findings" in result["output"]
    # Synthesis only saw the survivors — no empty placeholders fed in.
    assert "security" not in synth_inputs["text"]
    assert "(reviewer returned an empty response)" not in synth_inputs["text"]


@pytest.mark.asyncio
async def test_agentic_mode_runs_subagent_reviewers(monkeypatch, tmp_path):
    # agentic=true routes each reviewer through run_subagent (read-only tools)
    # instead of the snapshot _call_llm path.
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    subagent_calls = []

    async def fake_run_subagent(*, goal, system_prompt, candidate, root, toolset, fallbacks, max_rounds, owner, label=None, timeout=None):
        subagent_calls.append({"root": root, "toolset": set(toolset), "max_rounds": max_rounds})
        role = "reviewer"
        for line in system_prompt.splitlines():
            if "specialist role:" in line.lower():
                role = line.split(":", 1)[1].strip().rstrip(".")
                break
        return f"- {role}: investigated app.py and found a concrete issue."

    async def fake_synth(_candidates, messages, *, max_tokens):
        return "## Summary\nSynthesized agentic findings."

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.subagents.run_subagent", fake_run_subagent)
    # Only the synthesis goes through _call_llm in agentic mode.
    monkeypatch.setattr("src.code_review_swarm._call_llm", fake_synth)

    result = await run_code_review_swarm(
        json.dumps({"path": str(repo), "agentic": True, "agent_count": 3}),
        owner="admin",
    )

    assert result["exit_code"] == 0
    assert result["swarm"]["mode"] == "agentic"
    assert result["swarm"]["reviewers_succeeded"] == 3
    assert "Mode: agentic" in result["output"]
    # Each reviewer ran as a confined read-only sub-agent over the repo root.
    assert len(subagent_calls) == 3
    for call in subagent_calls:
        assert call["toolset"] == {"read_file", "grep", "glob", "ls"}
        assert os.path.realpath(call["root"]) == os.path.realpath(str(repo))


def test_tree_only_snapshot_skips_file_bodies():
    # Agentic mode collects the tree but no file bodies (reviewers read files
    # themselves), so samples stays empty while the tree is still populated.
    from src.code_review_swarm import _collect_snapshot
    import tempfile
    import os as _os

    repo = tempfile.mkdtemp()
    with open(_os.path.join(repo, "a.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n" * 100)
    with open(_os.path.join(repo, "b.py"), "w", encoding="utf-8") as f:
        f.write("y = 2\n" * 100)

    full = _collect_snapshot(repo)
    tree = _collect_snapshot(repo, tree_only=True)

    assert len(full.samples) >= 1
    assert tree.samples == []           # no bodies read
    assert tree.files_listed            # tree still populated
    assert tree.files_seen == full.files_seen


def test_agentic_arg_parses_with_aliases():
    from src.code_review_swarm import _parse_args

    assert _parse_args(json.dumps({"path": "C:/x"})).agentic is False
    assert _parse_args(json.dumps({"path": "C:/x", "agentic": True})).agentic is True
    # Models reach for these natural variants; all must work (a wrong key used
    # to silently fall back to snapshot mode).
    assert _parse_args(json.dumps({"path": "C:/x", "agentic_mode": True})).agentic is True
    assert _parse_args(json.dumps({"path": "C:/x", "agent_mode": True})).agentic is True
    assert _parse_args(json.dumps({"path": "C:/x", "deep": True})).agentic is True
    # String form (some models emit "true" not true).
    assert _parse_args(json.dumps({"path": "C:/x", "agentic": "true"})).agentic is True
    assert _parse_args(json.dumps({"path": "C:/x", "agentic": "false"})).agentic is False


@pytest.mark.asyncio
async def test_swarm_fails_loudly_when_all_reviewers_empty(monkeypatch, tmp_path):
    # If every reviewer comes back blank, return an error instead of an
    # authoritative-looking but empty report.
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")

    async def empty_llm(_candidates, messages, *, max_tokens):
        return ""

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [str(tmp_path)])
    monkeypatch.setattr("src.code_review_swarm._resolve_candidates", lambda model, owner: ([("http://local/v1/chat/completions", "review-model", {})], "review-model"))
    monkeypatch.setattr("src.code_review_swarm._call_llm", empty_llm)

    result = await run_code_review_swarm(json.dumps({"path": str(repo)}), owner="admin")

    assert result["exit_code"] == 1
    assert "every reviewer returned empty or failed" in result["error"]


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
