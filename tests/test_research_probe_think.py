"""The deep-research pre-flight probe must pass think=False.

`ResearchHandler._probe_endpoint` is a one-call liveness check that runs before
any search. Without think=False, a thinking model (e.g. kimi-k2.6) spends its
whole tiny token budget on hidden reasoning and returns no visible content,
which llm_core raises as a 502 — aborting the entire research run before it
starts ("probe failed: ... only hidden reasoning"). A liveness probe needs one
visible token, not reasoning.
"""

import asyncio

import src.llm_core as llm_core
from src.research_handler import ResearchHandler


def test_probe_disables_thinking_and_uses_a_real_budget(monkeypatch):
    captured = {}

    async def _fake_call(**kwargs):
        captured.update(kwargs)
        return "hi"

    # _probe_endpoint does `from src.llm_core import llm_call_async` at call time,
    # so patching the module attribute is enough.
    monkeypatch.setattr(llm_core, "llm_call_async", _fake_call)

    asyncio.run(ResearchHandler._probe_endpoint("http://x/api", "kimi-k2.6", {}))

    assert captured.get("think") is False, "probe must disable hidden reasoning"
    assert captured.get("model") == "kimi-k2.6"
    # No longer the 5-token trap that guaranteed a thinking model produced nothing.
    assert captured.get("max_tokens", 0) >= 16


def test_probe_propagates_failure(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(llm_core, "llm_call_async", _boom)

    try:
        asyncio.run(ResearchHandler._probe_endpoint("http://x/api", "m", {}))
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "a failed probe must raise so research aborts with a clear message"
