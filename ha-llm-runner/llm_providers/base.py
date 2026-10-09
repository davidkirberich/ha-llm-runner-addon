import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMRequest:
    """Provider-neutral description of a single LLM call."""
    prompt: str
    model: str
    temperature: float = 0.0
    schema: dict | None = None
    # (base64_data, mime_type) tuples, e.g. camera snapshots or PDFs
    attachments: list[tuple[str, str]] = field(default_factory=list)


class LLMProvider(ABC):
    """Base class for LLM backends. Subclasses only handle transport and response extraction."""

    name: str = ""
    # Add-on options follow the convention `<name>_api_key` and `<name>_model`.
    # Override only for extra key names; options are checked first, then env vars.
    api_key_options: tuple[str, ...] = ()
    model_option: str | None = None
    default_model: str = ""

    @classmethod
    def api_key_option_names(cls) -> tuple[str, ...]:
        return cls.api_key_options or (f"{cls.name}_api_key",)

    @classmethod
    def model_option_name(cls) -> str:
        return cls.model_option or f"{cls.name}_model"

    def __init__(self, api_key: str, timeout: int = 120):
        if not api_key:
            raise ValueError(f"No API key configured for provider '{self.name}'.")
        self.api_key = api_key
        self.timeout = timeout

    def resolve_model(self, task_model: str | None, options: dict) -> str:
        model = task_model or options.get(self.model_option_name()) or self.default_model
        if not model:
            raise ValueError(f"No model configured for provider '{self.name}'. Set `model:` in the task.")
        return model

    @abstractmethod
    def generate_text(self, request: LLMRequest) -> str:
        """Send the request and return the raw text answer of the model."""

    def generate(self, request: LLMRequest) -> dict:
        """Send the request and return the answer normalized to a dict payload."""
        return parse_json_response(self.generate_text(request))


def clean_latex(text: str) -> str:
    text = re.sub(r"\$([^\$]+)\$", lambda m: (
        m.group(1).replace(r"\text{", "").replace(r"}", "")
        .replace(r"^\circ", "°").replace(r"\circ", "°")
        .replace(r"\,", " ").replace(r"~", " ").strip()
    ), text)
    return re.sub(r"\\text\{([^}]+)\}", r"\1", text)


def strip_markdown_fences(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return clean_latex(text)


def parse_json_response(raw_text: str) -> dict:
    """Parse a model answer as JSON; fall back to a text payload for free-form answers."""
    text = strip_markdown_fences(raw_text)
    if not text:
        raise ValueError("Empty LLM response")
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"text": text, "summary": text}
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed, "text": text}
