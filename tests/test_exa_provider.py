"""Exa neural-search provider: request shape + result parsing.

Exa is the research-grade backend (https://exa.ai/docs/reference/search). The
provider POSTs to api.exa.ai/search with an x-api-key header and maps Exa's
`highlights`/`text` into our standard {title,url,snippet,age} result dict, so
the rest of the search/deep-research stack treats it like any other provider.
"""

from services.search import providers


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch(monkeypatch, *, settings=None, capture=None, payload=None, status_code=200):
    settings = settings if settings is not None else {"exa_api_key": "exa-test-key"}
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    def _fake_post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return _FakeResponse(payload if payload is not None else {"results": []}, status_code)

    monkeypatch.setattr(providers.httpx, "post", _fake_post)


def test_exa_search_builds_request_and_parses_results(monkeypatch):
    capture = {}
    payload = {
        "results": [
            {
                "title": "Neural search explained",
                "url": "https://example.com/neural",
                "publishedDate": "2026-01-02T00:00:00.000Z",
                "highlights": ["Exa uses embeddings to find relevant pages."],
                "text": "full body text ...",
            },
            {
                "title": "No highlight page",
                "url": "https://example.com/plain",
                "text": "fallback body used as snippet",
            },
            {"title": "skip — no url", "url": ""},
        ]
    }
    _patch(monkeypatch, capture=capture, payload=payload)

    results = providers.exa_search("how does neural search work", count=5)

    # Request: right endpoint, x-api-key header, query + numResults in the body.
    assert capture["url"] == "https://api.exa.ai/search"
    assert capture["headers"]["x-api-key"] == "exa-test-key"
    body = capture["json"]
    assert body["query"] == "how does neural search work"
    assert body["numResults"] == 5

    # Parsing: highlight -> snippet; text fallback when no highlight; url-less dropped.
    assert [r["url"] for r in results] == [
        "https://example.com/neural",
        "https://example.com/plain",
    ]
    assert results[0]["snippet"] == "Exa uses embeddings to find relevant pages."
    assert results[0]["age"] == "2026-01-02T00:00:00.000Z"
    assert results[1]["snippet"] == "fallback body used as snippet"


def test_exa_search_returns_empty_without_key(monkeypatch):
    called = {"post": False}

    def _no_post(*a, **k):
        called["post"] = True
        raise AssertionError("httpx.post must not be called without a key")

    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(providers.httpx, "post", _no_post)

    assert providers.exa_search("anything") == []
    assert called["post"] is False


def test_exa_search_time_filter_sets_start_date(monkeypatch):
    capture = {}
    _patch(monkeypatch, capture=capture, payload={"results": []})
    providers.exa_search("recent ai news", count=3, time_filter="week")
    assert "startPublishedDate" in capture["json"]


def test_exa_registered_as_keyed_provider():
    label, needs_key, needs_url = providers.PROVIDER_INFO["exa"]
    assert label == "Exa"
    assert needs_key is True
    assert needs_url is False


def test_exa_provider_key_maps_to_exa_api_key(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {"exa_api_key": "k-123"})
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert providers._get_provider_key("exa") == "k-123"
