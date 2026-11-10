import ai_router


def test_router_initializes_without_api_keys(monkeypatch):
    monkeypatch.setattr(ai_router, "GEMINI_API_KEY", None)
    monkeypatch.setattr(ai_router, "GROQ_API_KEY", None)

    router = ai_router.AIRouter()

    assert router.gemini is None
    assert router.groq is None


def test_router_returns_fallback_when_no_provider_available(monkeypatch):
    monkeypatch.setattr(ai_router, "GEMINI_API_KEY", None)
    monkeypatch.setattr(ai_router, "GROQ_API_KEY", None)

    router = ai_router.AIRouter()

    assert router.ask("Hello!") == "Sorry, both AI providers are unavailable."