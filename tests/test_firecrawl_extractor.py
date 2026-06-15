"""Firecrawl content extractor.

Firecrawl turns a URL into clean LLM-ready markdown (and renders JS), so it pairs
with Exa: Exa finds sources, Firecrawl reads them. It is wired into
`fetch_webpage_content` with a `content_extractor` dial:
  - "firecrawl": try Firecrawl first for every page (built-in fallback on failure)
  - "auto":      built-in first, Firecrawl only rescues JS-shell / thin pages
  - "builtin":   never call Firecrawl
Internal/private URLs are never sent to Firecrawl (same SSRF guard as the
built-in fetch). These tests cover the scrape mapping + the orchestration.
"""

from services.search import content


class _FcResp:
    """Stand-in for the httpx response from api.firecrawl.dev."""

    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _Resp:
    """Stand-in for the built-in httpx fetch (_get_public_url)."""

    def __init__(self, text, status=200, content_type="text/html; charset=utf-8"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


def _fc_payload(markdown="# Clean\n\nReal content here.", title="Clean Page"):
    return {
        "success": True,
        "data": {
            "markdown": markdown,
            "metadata": {"title": title, "sourceURL": "https://ex.com/a", "statusCode": 200},
        },
    }


def _fc_result(text):
    """A Firecrawl-shaped result dict (what _firecrawl_scrape returns)."""
    return {
        "success": True, "content": text, "title": "T", "url": "https://ex.com/x",
        "lists": [], "tables": [], "code_blocks": [], "meta_description": "",
        "meta_keywords": "", "js_rendered": False, "js_message": "", "error": "",
        "extractor": "firecrawl",
    }


# ── _firecrawl_scrape ──────────────────────────────────────────────────────

def test_firecrawl_scrape_builds_request_and_maps_markdown(monkeypatch):
    cap = {}
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)

    def _post(url, **kw):
        cap["url"] = url
        cap.update(kw)
        return _FcResp(_fc_payload())

    monkeypatch.setattr(content.httpx, "post", _post)

    r = content._firecrawl_scrape("https://ex.com/a", timeout=20)

    assert cap["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert cap["headers"]["Authorization"] == "Bearer fc-key"
    assert cap["json"]["url"] == "https://ex.com/a"
    assert cap["json"]["formats"] == ["markdown"]
    assert cap["json"]["onlyMainContent"] is True
    assert r["success"] is True
    assert r["content"].startswith("# Clean")
    assert r["title"] == "Clean Page"
    assert r["extractor"] == "firecrawl"


def test_firecrawl_scrape_returns_none_without_key(monkeypatch):
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "")

    def _post(*a, **k):
        raise AssertionError("must not POST to Firecrawl without a key")

    monkeypatch.setattr(content.httpx, "post", _post)
    assert content._firecrawl_scrape("https://ex.com/a") is None


def test_firecrawl_scrape_skips_internal_url(monkeypatch):
    # Real _public_http_url returns False for localhost -> never leaves the box.
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")

    def _post(*a, **k):
        raise AssertionError("must not send an internal URL to Firecrawl")

    monkeypatch.setattr(content.httpx, "post", _post)
    assert content._firecrawl_scrape("http://localhost:8080/admin") is None


def test_firecrawl_scrape_handles_unsuccessful_response(monkeypatch):
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)
    monkeypatch.setattr(content.httpx, "post", lambda url, **k: _FcResp({"success": False}))
    assert content._firecrawl_scrape("https://ex.com/a") is None


# ── _content_extractor_mode ────────────────────────────────────────────────

def test_content_extractor_mode_normalizes(monkeypatch):
    monkeypatch.setattr(content, "_load_settings", lambda: {"content_extractor": "firecrawl"})
    assert content._content_extractor_mode() == "firecrawl"
    monkeypatch.setattr(content, "_load_settings", lambda: {})
    assert content._content_extractor_mode() == "auto"
    monkeypatch.setattr(content, "_load_settings", lambda: {"content_extractor": "bogus"})
    assert content._content_extractor_mode() == "auto"


# ── fetch_webpage_content orchestration ────────────────────────────────────

def test_fetch_firecrawl_mode_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content, "_content_extractor_mode", lambda: "firecrawl")
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)
    monkeypatch.setattr(content, "_firecrawl_scrape", lambda u, t: _fc_result("FC markdown body"))

    def _no_fetch(*a, **k):
        raise AssertionError("built-in fetch must not run when Firecrawl succeeds")

    monkeypatch.setattr(content, "_get_public_url", _no_fetch)

    r = content.fetch_webpage_content("https://ex.com/page")
    assert r["extractor"] == "firecrawl"
    assert r["content"] == "FC markdown body"


def test_fetch_auto_rescues_thin_builtin(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content, "_content_extractor_mode", lambda: "auto")
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)

    thin = "<html><head><title>T</title></head><body><p>tiny</p></body></html>"
    monkeypatch.setattr(content, "_get_public_url", lambda url, **k: _Resp(thin))
    rich = "FIRECRAWL " * 200
    monkeypatch.setattr(content, "_firecrawl_scrape", lambda u, t: _fc_result(rich))

    r = content.fetch_webpage_content("https://ex.com/spa")
    assert r.get("extractor") == "firecrawl"
    assert r["content"] == rich


def test_fetch_auto_keeps_good_builtin(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content, "_content_extractor_mode", lambda: "auto")
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)

    rich_html = ("<html><head><title>T</title></head><body>"
                 "<div class='content'>" + ("word " * 400) + "</div></body></html>")
    monkeypatch.setattr(content, "_get_public_url", lambda url, **k: _Resp(rich_html))

    def _no_fc(*a, **k):
        raise AssertionError("auto mode must not call Firecrawl when built-in content is sufficient")

    monkeypatch.setattr(content, "_firecrawl_scrape", _no_fc)

    r = content.fetch_webpage_content("https://ex.com/article")
    assert r.get("extractor") != "firecrawl"
    assert "word" in r["content"]


def test_fetch_builtin_mode_never_calls_firecrawl(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content, "_content_extractor_mode", lambda: "builtin")
    monkeypatch.setattr(content, "_firecrawl_key", lambda: "fc-key")
    monkeypatch.setattr(content, "_public_http_url", lambda u: True)

    thin = "<html><head><title>T</title></head><body><p>tiny</p></body></html>"
    monkeypatch.setattr(content, "_get_public_url", lambda url, **k: _Resp(thin))

    def _no_fc(*a, **k):
        raise AssertionError("builtin mode must never call Firecrawl")

    monkeypatch.setattr(content, "_firecrawl_scrape", _no_fc)

    r = content.fetch_webpage_content("https://ex.com/thin")
    assert r.get("extractor") != "firecrawl"
