"""Tests for the Ollama `think` toggle and reasoning-only response policy.

Thinking models (kimi-k2.6 etc.) on native Ollama /api/chat put their hidden
reasoning in message["thinking"]. Two production failure modes are covered:

  1. Non-streaming calls where the model spent the whole num_predict budget
     on reasoning returned "" silently — now they raise a specific 502 so
     fallback chains retry the next candidate and callers see why.
  2. Utility callers (code review swarm reviewers) can now pass think=False
     so the model skips reasoning entirely and the budget goes to the answer.
"""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from src import llm_core


# ---------------------------------------------------------------------------
# _build_ollama_payload: think emission
# ---------------------------------------------------------------------------

def test_build_ollama_payload_omits_think_by_default():
    payload = llm_core._build_ollama_payload(
        "kimi-k2.6", [{"role": "user", "content": "x"}],
        temperature=0.2, max_tokens=100,
    )
    assert "think" not in payload


@pytest.mark.parametrize("flag", [False, True])
def test_build_ollama_payload_emits_explicit_think(flag):
    payload = llm_core._build_ollama_payload(
        "kimi-k2.6", [{"role": "user", "content": "x"}],
        temperature=0.2, max_tokens=100, think=flag,
    )
    assert payload["think"] is flag


# ---------------------------------------------------------------------------
# _parse_ollama_response: reasoning-only policy
# ---------------------------------------------------------------------------

def test_parse_ollama_reasoning_only_raises_specific_error():
    data = {"message": {"content": "", "thinking": "step 1... step 2..."}, "done": True}
    with pytest.raises(HTTPException) as exc:
        llm_core._parse_ollama_response(data)
    assert exc.value.status_code == 502
    assert "think" in exc.value.detail.lower()


def test_parse_ollama_content_wins_when_thinking_also_present():
    data = {"message": {"content": "The answer", "thinking": "hidden"}, "done": True}
    assert llm_core._parse_ollama_response(data) == "The answer"


def test_parse_ollama_empty_without_thinking_still_returns_empty():
    data = {"message": {"content": ""}, "done": True}
    assert llm_core._parse_ollama_response(data) == ""


def test_parse_ollama_whitespace_thinking_is_not_reasoning_only():
    data = {"message": {"content": "", "thinking": "  \n"}, "done": True}
    assert llm_core._parse_ollama_response(data) == ""


# ---------------------------------------------------------------------------
# llm_call (sync): think threads into the outgoing native Ollama request
# ---------------------------------------------------------------------------

def test_llm_call_threads_think_false_into_payload(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200, request=request,
            json={"message": {"content": "OK"}, "done": True},
        )

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 0)

    result = llm_core.llm_call(
        "https://ollama.com/api",
        "kimi-k2.6:cloud",
        [{"role": "user", "content": "sync think=False request"}],
        temperature=0.2,
        max_tokens=7,
        think=False,
    )

    assert result == "OK"
    assert seen["json"]["think"] is False


# ---------------------------------------------------------------------------
# llm_call_async_with_fallback: the swarm path — think passes through kwargs,
# and a reasoning-only first candidate falls through to the next one.
# ---------------------------------------------------------------------------

class _FakeAsyncClient:
    """Routes by host: ollama.com returns a reasoning-only native response,
    anything else returns a normal OpenAI-compatible answer."""

    def __init__(self, seen):
        self.seen = seen

    async def post(self, url, headers=None, json=None, timeout=None):
        self.seen.setdefault("payloads", []).append((url, json))
        request = httpx.Request("POST", url)
        if "ollama.com" in url:
            return httpx.Response(
                200, request=request,
                json={"message": {"content": "", "thinking": "budget burned"}, "done": True},
            )
        return httpx.Response(
            200, request=request,
            json={"choices": [{"message": {"content": "fallback answer"}}]},
        )


def test_async_fallback_threads_think_and_survives_reasoning_only(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeAsyncClient(seen))
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 0)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)

    result = asyncio.run(llm_core.llm_call_async_with_fallback(
        [
            ("https://ollama.com/api", "kimi-k2.6:cloud", None),
            ("http://fallback-host/v1/chat/completions", "small-model", None),
        ],
        [{"role": "user", "content": "async fallback think request"}],
        max_tokens=50,
        think=False,
    ))

    # Reasoning-only primary did not silently return "" — the chain moved on.
    assert result == "fallback answer"
    ollama_url, ollama_payload = seen["payloads"][0]
    assert "ollama.com" in ollama_url
    assert ollama_payload["think"] is False


def test_llm_call_async_reasoning_only_raises_not_schema_error(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeAsyncClient(seen))
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 0)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(llm_core.llm_call_async(
            "https://ollama.com/api",
            "kimi-k2.6:cloud",
            [{"role": "user", "content": "async reasoning-only request"}],
            max_tokens=50,
        ))

    assert exc.value.status_code == 502
    assert "think" in exc.value.detail.lower()
    # The specific message must not be masked by the generic schema guard.
    assert "Unexpected schema" not in exc.value.detail


# ---------------------------------------------------------------------------
# Cache key: think participates, so think=False and default responses
# can never serve each other from the response cache.
# ---------------------------------------------------------------------------

def test_cache_key_separates_think_modes():
    msgs = [{"role": "user", "content": "x"}]
    keys = {
        llm_core._get_cache_key("u", "m", msgs, 0.2, 100),
        llm_core._get_cache_key("u", "m", msgs, 0.2, 100, think=False),
        llm_core._get_cache_key("u", "m", msgs, 0.2, 100, think=True),
    }
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# Code review swarm: reviewers are utility calls and must request think=False
# ---------------------------------------------------------------------------

def test_swarm_call_llm_passes_think_false(monkeypatch):
    from src import code_review_swarm

    seen = {}

    async def fake_fallback(candidates, messages, **kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_fallback)

    result = asyncio.run(code_review_swarm._call_llm(
        [("https://ollama.com/api", "kimi-k2.6:cloud", None)],
        [{"role": "user", "content": "review"}],
        max_tokens=9000,
    ))

    assert result == "ok"
    assert seen["kwargs"]["think"] is False
