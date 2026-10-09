import os

from .base import LLMProvider, LLMRequest, parse_json_response, strip_markdown_fences
from .gemini import GeminiProvider

# Register new providers here, keyed by the value used in a task's `provider:` field
PROVIDERS: dict[str, type[LLMProvider]] = {
    GeminiProvider.name: GeminiProvider,
}

DEFAULT_PROVIDER = GeminiProvider.name


def resolve_api_key(provider_cls: type[LLMProvider], options: dict) -> str:
    for key in provider_cls.api_key_option_names():
        value = options.get(key) or os.environ.get(key.upper()) or os.environ.get(key)
        if value:
            return value
    return ""


def get_provider(name: str | None, options: dict) -> LLMProvider:
    provider_name = (name or DEFAULT_PROVIDER).lower()
    provider_cls = PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown LLM provider '{provider_name}'. Available: {', '.join(sorted(PROVIDERS))}")
    return provider_cls(api_key=resolve_api_key(provider_cls, options))


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "GeminiProvider",
    "LLMProvider",
    "LLMRequest",
    "get_provider",
    "parse_json_response",
    "resolve_api_key",
    "strip_markdown_fences",
]
