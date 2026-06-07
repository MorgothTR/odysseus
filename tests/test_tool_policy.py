import asyncio
import json
import sys
from types import SimpleNamespace

import src.agent_loop as al
from src.agent_tools import ToolBlock
from src.tool_execution import execute_tool_block
from src.tool_policy import (
    build_effective_tool_policy,
    detect_guide_only_turn,
    detect_smart_file_routing_turn,
)


def _collect(gen):
    async def _run():
        return [c async for c in gen]

    return asyncio.run(_run())


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return out


def _delta_chunk(text):
    return "data: " + json.dumps({"delta": text}) + "\n\n"


def _patch_loop_basics(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)


def test_detects_strong_guide_only_turns():
    assert detect_guide_only_turn("GUIDE-ONLY MODE. DO NOT USE TOOLS.")
    assert detect_guide_only_turn("NO-TOOLS MODE.")
    assert detect_guide_only_turn("Ask me before using tools.")
    assert detect_guide_only_turn("You are not allowed to:\n- use tools\n- execute commands")


def test_does_not_treat_ordinary_guidance_as_no_tools():
    assert detect_guide_only_turn("Can you guide me through fixing this bug?") is None
    assert detect_guide_only_turn("I have no tools installed in this project.") is None
    assert detect_guide_only_turn("Write the script in the repo; I'll run it locally.") is None
    assert detect_guide_only_turn("Do not run commands that write files; inspect the repo first.") is None
    assert detect_guide_only_turn("Don't execute shell commands unless I approve them.") is None


def test_guide_only_policy_blocks_and_hides_tools():
    policy = build_effective_tool_policy(
        disabled_tools={"web_search"},
        last_user_message="GUIDE-ONLY MODE. DO NOT USE TOOLS.",
    )
    assert policy.mode == "guide_only"
    assert policy.disable_mcp is True
    assert policy.block_all_tool_calls is True
    for tool in ("bash", "python", "web_search", "read_file"):
        assert tool in policy.disabled_tools
        assert tool in policy.hidden_tools
        assert policy.blocks(tool)


def test_normal_policy_preserves_existing_disabled_tools():
    policy = build_effective_tool_policy(
        disabled_tools={"web_search"},
        last_user_message="Please check this normally.",
    )
    assert policy.mode == "normal"
    assert policy.blocks("web_search")
    assert not policy.blocks("bash")


def test_smart_file_routing_detects_plain_local_file_requests():
    assert detect_smart_file_routing_turn(
        r"Can you list the files in C:\Projects\framescan_backup\framescan?"
    )
    assert detect_smart_file_routing_turn(
        "Use the dedicated Odysseus ls tool to list C:/Projects/example. Do not use bash or PowerShell."
    )
    assert detect_smart_file_routing_turn(
        "Search C:/Projects/example for TODO with grep."
    )
    assert detect_smart_file_routing_turn(
        "Make a list of files in C:/Projects/example."
    )


def test_smart_file_routing_keeps_explicit_power_tool_requests_available():
    assert detect_smart_file_routing_turn(
        r"as a bash use this: dir C:\Projects\framescan_backup\framescan /b"
    ) is None
    assert detect_smart_file_routing_turn(
        r"run this command: dir C:\Projects\framescan_backup\framescan /b"
    ) is None
    assert detect_smart_file_routing_turn(
        "Write a Python script that lists C:/Projects/example."
    ) is None
    assert detect_smart_file_routing_turn(
        "List C:/Projects/example and save the output to a text file."
    ) is None
    assert detect_smart_file_routing_turn(
        "Make a text file containing the files in C:/Projects/example."
    ) is None


def test_smart_file_routing_policy_hides_power_and_write_tools_only_for_turn():
    policy = build_effective_tool_policy(
        last_user_message=r"Can you list the files in C:\Projects\framescan_backup\framescan?"
    )

    assert policy.mode == "file_routing"
    for tool in ("bash", "python", "write_file", "edit_file", "create_document"):
        assert policy.blocks(tool)
        assert tool in policy.hidden_tools
        assert "dedicated file tools" in policy.reason_for(tool)
    for tool in ("ls", "read_file", "grep", "glob"):
        assert not policy.blocks(tool)


def test_executor_policy_backstop_blocks_tools():
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    desc, result = asyncio.run(
        execute_tool_block(ToolBlock("bash", "echo should-not-run"), tool_policy=policy)
    )
    assert desc == "bash: BLOCKED"
    assert result["exit_code"] == 1
    assert "forbade" in result["error"]


def test_executor_policy_backstop_blocks_smart_file_routing_power_tools():
    policy = build_effective_tool_policy(
        last_user_message=r"Can you list C:\Projects\framescan_backup\framescan?"
    )
    desc, result = asyncio.run(
        execute_tool_block(ToolBlock("python", "print('should not run')"), tool_policy=policy)
    )
    assert desc == "python: BLOCKED"
    assert result["exit_code"] == 1
    assert "dedicated file tools" in result["error"]


def test_agent_loop_injects_smart_file_routing_directive(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_messages = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_messages.extend(messages)
        yield _delta_chunk("Done")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    prompt = r"Can you list the files in C:\Projects\framescan_backup\framescan?"
    policy = build_effective_tool_policy(last_user_message=prompt)
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": prompt}],
            max_rounds=1,
            relevant_tools={"ls", "read_file", "grep", "glob"},
            tool_policy=policy,
        )
    )

    assert any("LOCAL FILE TOOL ROUTING" in msg.get("content", "") for msg in sent_messages)
    assert any('"pattern": "TODO"' in msg.get("content", "") for msg in sent_messages)
    assert any("Done" in event.get("delta", "") for event in _events(chunks))


def test_local_file_success_followup_only_for_successful_file_routing():
    policy = build_effective_tool_policy(
        last_user_message="Search C:/Projects/example for TODO."
    )

    note = al._local_file_success_followup(policy, "grep", {"output": "No matches", "exit_code": 0})

    assert "Treat that result as authoritative" in note
    assert "Do not retry the same listing/search/read" in note


def test_local_file_success_followup_skips_errors_and_normal_mode():
    policy = build_effective_tool_policy(
        last_user_message="Search C:/Projects/example for TODO."
    )
    normal = build_effective_tool_policy(last_user_message="Review this repo.")

    assert al._local_file_success_followup(policy, "grep", {"error": "bad path", "exit_code": 1}) == ""
    assert al._local_file_success_followup(policy, "bash", {"output": "ok", "exit_code": 0}) == ""
    assert al._local_file_success_followup(normal, "grep", {"output": "ok", "exit_code": 0}) == ""


def test_agent_loop_blocks_guide_only_fenced_tool_before_start(monkeypatch):
    _patch_loop_basics(monkeypatch)
    called = False

    async def _fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return ("bash", {"output": "ran", "exit_code": 0})

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("```bash\necho should-not-run\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    policy = build_effective_tool_policy(last_user_message="GUIDE-ONLY MODE. DO NOT USE TOOLS.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "GUIDE-ONLY MODE. DO NOT USE TOOLS."}],
            max_rounds=1,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert called is False
    assert not any(event.get("type") == "tool_start" for event in events)
    blocked = [event for event in events if event.get("type") == "tool_output"]
    assert blocked
    assert blocked[0]["tool"] == "bash"
    assert blocked[0]["exit_code"] == 1


def test_guide_only_hides_api_function_schemas(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"bash", "web_search"},
            tool_policy=policy,
        )
    )

    assert sent_tools == [None]


def test_guide_only_skips_tool_retrieval(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    def _fail_tool_index():
        raise AssertionError("guide-only mode must not retrieve tool candidates")

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "src.tool_index",
        SimpleNamespace(get_tool_index=_fail_tool_index, ALWAYS_AVAILABLE=set()),
    )
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools=None,
            tool_policy=policy,
        )
    )

    assert sent_tools == [None]


def test_guide_only_blocks_document_prestream(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("```create_document\nTitle\nmd\nBody\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"create_document"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert not any(event.get("type") == "doc_stream_open" for event in events)
    assert not any(event.get("type") == "tool_start" for event in events)
    assert any(event.get("type") == "tool_output" and event.get("tool") == "create_document" for event in events)


def test_guide_only_blocks_later_round_document_streaming(monkeypatch):
    _patch_loop_basics(monkeypatch)
    calls = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _delta_chunk("```bash\necho blocked\n```")
        else:
            yield _delta_chunk("```create_document\nTitle\nmd\nBody\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=2,
            relevant_tools={"bash", "create_document"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert calls == 2
    assert not any(event.get("type") == "doc_stream_open" for event in events)
    assert not any(event.get("type") == "doc_stream_delta" for event in events)


def test_guide_only_directive_dominates_workspace_prompt(monkeypatch):
    _patch_loop_basics(monkeypatch)
    system_prompts = []

    async def _fake_stream(_candidates, messages, **kwargs):
        system_prompts.append(messages[0]["content"])
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"bash"},
            tool_policy=policy,
            workspace="/tmp/project",
        )
    )

    assert system_prompts
    assert system_prompts[0].startswith("## GUIDE-ONLY MODE")
    assert "ACTIVE WORKSPACE" not in system_prompts[0]
    assert "ALWAYS start by exploring" not in system_prompts[0]


def test_guide_only_skips_intent_without_action_nudge(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("I will check the logs.")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=2,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert not any(event.get("type") == "agent_step" for event in events)


def test_guide_only_suppresses_active_document_context(monkeypatch):
    _patch_loop_basics(monkeypatch)
    prompt_payloads = []

    async def _fake_stream(_candidates, messages, **kwargs):
        prompt_payloads.append("\n\n".join(str(msg.get("content", "")) for msg in messages))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    active_doc = SimpleNamespace(
        id="doc-1",
        current_content="SECRET ACTIVE DOCUMENT CONTENT",
        title="Secret Doc",
        language="markdown",
    )

    _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"edit_document"},
            tool_policy=policy,
            active_document=active_doc,
        )
    )

    assert prompt_payloads
    assert "SECRET ACTIVE DOCUMENT CONTENT" not in prompt_payloads[0]
    assert "ACTIVE DOCUMENT" not in prompt_payloads[0]
    assert "Relevant skills" not in prompt_payloads[0]


def test_guide_only_skips_teacher_escalation(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("Could you tell me what output you see?")
        yield "data: [DONE]\n\n"

    async def _fail_teacher(*_args, **_kwargs):
        raise AssertionError("teacher escalation must not run in guide-only mode")
        yield ""

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "src.teacher_escalation",
        SimpleNamespace(run_teacher_inline=_fail_teacher),
    )
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )

    assert any("Could you tell me" in chunk for chunk in chunks)
