import pytest

import llm_providers
from llm_providers import GeminiProvider, LLMProvider, LLMRequest, get_provider, parse_json_response


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def gemini_answer(*parts, finish_reason="STOP"):
    return {"candidates": [{"content": {"parts": list(parts)}, "finishReason": finish_reason}]}


def test_gemini_payload_contains_attachments_temperature_and_schema():
    schema = {"type": "object", "properties": {"state": {"type": "string"}}}
    request = LLMRequest(
        prompt="Describe the image.",
        model="gemini-test",
        temperature=0.4,
        schema=schema,
        attachments=[("abc", "image/png"), ("pdf", "application/pdf")],
    )

    payload = GeminiProvider(api_key="key").build_payload(request)

    parts = payload["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/png", "data": "abc"}}
    assert parts[1] == {"inlineData": {"mimeType": "application/pdf", "data": "pdf"}}
    assert parts[-1] == {"text": "Describe the image."}
    config = payload["generationConfig"]
    assert config["temperature"] == 0.4
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == schema


def test_gemini_payload_without_schema_requests_plain_text():
    payload = GeminiProvider(api_key="key").build_payload(LLMRequest(prompt="Hi", model="m"))

    assert payload["generationConfig"] == {"temperature": 0.0}


def test_gemini_sends_api_key_as_header_not_in_url(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers)
        return FakeResponse(gemini_answer({"text": '{"state": "ok"}'}))

    monkeypatch.setattr(llm_providers.gemini.requests, "post", fake_post)

    result = GeminiProvider(api_key="secret-key").generate(LLMRequest(prompt="Hi", model="gemini-test"))

    assert result == {"state": "ok"}
    assert captured["url"].endswith("/models/gemini-test:generateContent")
    assert "secret-key" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "secret-key"


def test_gemini_extract_text_skips_thought_parts_and_joins_chunks():
    data = gemini_answer({"text": "internal", "thought": True}, {"text": "Hello "}, {"text": "world"})

    assert GeminiProvider.extract_text(data) == "Hello world"


@pytest.mark.parametrize("data", [{"candidates": []}, gemini_answer(finish_reason="SAFETY")])
def test_gemini_extract_text_raises_on_missing_answer(data):
    with pytest.raises(RuntimeError):
        GeminiProvider.extract_text(data)


def test_parse_json_response_handles_fences_text_and_lists():
    assert parse_json_response('```json\n{"summary": "ok"}\n```') == {"summary": "ok"}
    assert parse_json_response("Plain answer") == {"text": "Plain answer", "summary": "Plain answer"}
    assert parse_json_response("[1, 2]")["data"] == [1, 2]


def test_get_provider_defaults_to_gemini_and_reads_key_from_options(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    provider = get_provider(None, {"gemini_api_key": "from-options"})

    assert isinstance(provider, GeminiProvider)
    assert provider.api_key == "from-options"


def test_get_provider_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    assert get_provider("gemini", {}).api_key == "from-env"


def test_get_provider_rejects_unknown_provider_and_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("gemini_api_key", raising=False)

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_provider("does-not-exist", {"gemini_api_key": "k"})
    with pytest.raises(ValueError, match="No API key"):
        get_provider("gemini", {})


def test_registered_custom_provider_is_selectable(monkeypatch):
    class EchoProvider(LLMProvider):
        name = "echo"

        def generate_text(self, request):
            return request.prompt

    monkeypatch.setitem(llm_providers.PROVIDERS, "echo", EchoProvider)

    provider = get_provider("echo", {"echo_api_key": "k"})

    assert provider.generate(LLMRequest(prompt="hello", model="m")) == {"text": "hello", "summary": "hello"}


def test_option_names_follow_provider_name_convention(monkeypatch):
    class EchoProvider(LLMProvider):
        name = "echo"

        def generate_text(self, request):
            return ""

    monkeypatch.setitem(llm_providers.PROVIDERS, "echo", EchoProvider)
    monkeypatch.setenv("ECHO_API_KEY", "env-key")

    assert EchoProvider.api_key_option_names() == ("echo_api_key",)
    assert EchoProvider.model_option_name() == "echo_model"
    assert GeminiProvider.api_key_option_names() == ("gemini_api_key",)
    assert GeminiProvider.model_option_name() == "gemini_model"

    provider = get_provider("echo", {"gemini_api_key": "other"})
    assert provider.api_key == "env-key"
    assert provider.resolve_model(None, {"echo_model": "echo-1", "gemini_model": "g"}) == "echo-1"


def test_resolve_model_prefers_task_then_option_then_default():
    provider = GeminiProvider(api_key="k")

    assert provider.resolve_model("task-model", {"gemini_model": "opt-model"}) == "task-model"
    assert provider.resolve_model(None, {"gemini_model": "opt-model"}) == "opt-model"
    assert provider.resolve_model(None, {}) == GeminiProvider.default_model


def test_resolve_model_does_not_leak_gemini_option_into_other_providers():
    class NoDefaultProvider(LLMProvider):
        name = "nodefault"

        def generate_text(self, request):
            return ""

    with pytest.raises(ValueError, match="No model configured"):
        NoDefaultProvider(api_key="k").resolve_model(None, {"gemini_model": "gemini-x"})
