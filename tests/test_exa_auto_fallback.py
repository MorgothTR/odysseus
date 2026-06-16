"""Exa is the preferred default fallback once a key is configured.

Before this, a dead/empty primary (e.g. the no-Docker SearXNG default) fell back
to DuckDuckGo. With an Exa key set, research-grade Exa is used automatically —
the user gets it without selecting Exa as the provider. An explicit
``search_fallback_chain`` still wins, and Exa is never duplicated.
"""

from services.search import core, providers


def _settings(monkeypatch, settings):
    # _build_provider_chain reads core._get_search_settings; _get_provider_key
    # (defined in providers) reads providers._get_search_settings — patch both.
    monkeypatch.setattr(core, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.delenv("EXA_API_KEY", raising=False)


def test_exa_inserted_into_default_fallback_when_keyed(monkeypatch):
    _settings(monkeypatch, {"search_provider": "searxng", "exa_api_key": "k"})
    assert core._build_provider_chain("searxng") == ["searxng", "exa", "duckduckgo"]


def test_default_fallback_unchanged_without_key(monkeypatch):
    _settings(monkeypatch, {"search_provider": "searxng"})
    assert core._build_provider_chain("searxng") == ["searxng", "duckduckgo"]


def test_explicit_fallback_chain_still_wins(monkeypatch):
    _settings(monkeypatch, {"exa_api_key": "k", "search_fallback_chain": ["brave"]})
    assert core._build_provider_chain("searxng") == ["searxng", "brave"]


def test_exa_not_duplicated_when_it_is_the_primary(monkeypatch):
    _settings(monkeypatch, {"exa_api_key": "k"})
    assert core._build_provider_chain("exa") == ["exa", "duckduckgo"]
