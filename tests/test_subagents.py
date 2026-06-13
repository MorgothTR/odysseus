import pytest

from src.subagents import READONLY_TOOLSET, run_subagent


@pytest.mark.asyncio
async def test_run_subagent_collects_text_and_confines_tools(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield 'data: {"delta": "Hello "}\n\n'
        yield 'data: {"type": "tool_output", "tool": "grep", "output": "match in app.py"}\n\n'
        yield 'data: {"delta": "world"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream)

    result = await run_subagent(
        goal="investigate",
        system_prompt="you are a reviewer",
        candidate=("http://local/v1/chat/completions", "m", {}),
        root="C:/proj",
        toolset={"read_file", "grep"},
        max_rounds=6,
    )

    assert result == "Hello world"
    # Confinement: workspace + restricted toolset passed through.
    assert captured["workspace"] == "C:/proj"
    assert captured["relevant_tools"] == {"read_file", "grep"}
    assert captured["max_rounds"] == 6
    # Allowlist inverted to denylist: granted tools NOT disabled, dangerous ones ARE.
    assert "read_file" not in captured["disabled_tools"]
    assert "grep" not in captured["disabled_tools"]
    for blocked in ("bash", "python", "write_file", "edit_file", "run_code_review_swarm"):
        assert blocked in captured["disabled_tools"]


@pytest.mark.asyncio
async def test_run_subagent_excludes_thinking_from_answer(monkeypatch):
    # Thinking-model reasoning must not leak into the returned summary — only
    # the real (non-thinking) answer deltas count.
    async def fake_stream(**kwargs):
        yield 'data: {"delta": "The user wants me to audit...", "thinking": true}\n\n'
        yield 'data: {"delta": "let me read the file...", "thinking": true}\n\n'
        yield 'data: {"delta": "## Findings\\n- bug on line 42"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream)
    result = await run_subagent(goal="g", system_prompt="s", candidate=("u", "m", {}))
    assert result == "## Findings\n- bug on line 42"
    assert "The user wants me to" not in result


@pytest.mark.asyncio
async def test_run_subagent_grace_summarizes_when_no_final_text(monkeypatch):
    async def fake_stream(**kwargs):
        # Only tool output, model never wrote a final answer (ran out of rounds).
        yield 'data: {"type": "tool_output", "tool": "ls", "output": "app.py\\nutil.py"}\n\n'
        yield "data: [DONE]\n\n"

    captured_grace = {}

    async def fake_grace(candidates, messages, **kwargs):
        captured_grace["messages"] = messages
        captured_grace["think"] = kwargs.get("think")
        return "graceful summary of findings"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream)
    monkeypatch.setattr("src.llm_core.llm_call_async_with_fallback", fake_grace)

    result = await run_subagent(
        goal="investigate",
        system_prompt="you are a reviewer",
        candidate=("http://local/v1/chat/completions", "m", {}),
    )

    assert result == "graceful summary of findings"
    # Grace call disables thinking and includes the captured tool output.
    assert captured_grace["think"] is False
    assert "app.py" in captured_grace["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_run_subagent_returns_empty_when_grace_fails(monkeypatch):
    async def fake_stream(**kwargs):
        yield "data: [DONE]\n\n"

    async def boom(*a, **k):
        raise RuntimeError("no endpoint")

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream)
    monkeypatch.setattr("src.llm_core.llm_call_async_with_fallback", boom)

    result = await run_subagent(
        goal="x",
        system_prompt="y",
        candidate=("u", "m", {}),
    )
    assert result == ""


@pytest.mark.asyncio
async def test_run_subagent_honors_wall_clock_timeout(monkeypatch):
    import asyncio

    async def hanging_stream(**kwargs):
        yield 'data: {"delta": "partial findings before the hang"}\n\n'
        await asyncio.sleep(30)  # wedged call the round cap can't catch
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", hanging_stream)

    result = await run_subagent(
        goal="g", system_prompt="s", candidate=("u", "m", {}), timeout=0.2
    )
    # Timed out, but returns what the child had written before the hang.
    assert result == "partial findings before the hang"


def test_subagent_model_setting_overrides_utility(monkeypatch):
    from src.subagents import resolve_subagent_candidates

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: "kimi-k2.7-code" if key == "subagent_model" else default,
    )
    monkeypatch.setattr(
        "src.ai_interaction._resolve_model",
        lambda spec, owner=None: ("http://local/v1/chat/completions", spec, {}),
    )
    # No per-call model -> falls back to the subagent_model setting (not Utility).
    candidates, model = resolve_subagent_candidates("", owner=None)
    assert model == "kimi-k2.7-code"
    # An explicit per-call model still wins over the setting.
    candidates2, model2 = resolve_subagent_candidates("some-other-model", owner=None)
    assert model2 == "some-other-model"


def test_readonly_toolset_has_no_mutators():
    for mutator in ("bash", "python", "write_file", "edit_file", "run_code_review_swarm"):
        assert mutator not in READONLY_TOOLSET
