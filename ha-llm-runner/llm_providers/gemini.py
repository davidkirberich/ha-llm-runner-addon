import requests

from .base import LLMProvider, LLMRequest, strip_markdown_fences


class GeminiProvider(LLMProvider):
    """Google Gemini via the REST generateContent endpoint (text, images/PDFs, temperature, JSON schema)."""

    name = "gemini"
    default_model = "gemini-3.5-flash-lite"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def build_payload(self, request: LLMRequest) -> dict:
        parts = [{"inlineData": {"mimeType": mime, "data": data}} for data, mime in request.attachments]
        parts.append({"text": request.prompt})

        generation_config = {"temperature": float(request.temperature)}
        if request.schema:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseJsonSchema"] = request.schema

        return {"contents": [{"parts": parts}], "generationConfig": generation_config}

    def generate_text(self, request: LLMRequest) -> str:
        url = f"{self.base_url}/models/{request.model}:generateContent"
        # Key goes in a header so it never shows up in logged request URLs
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        res = requests.post(url, headers=headers, json=self.build_payload(request), timeout=self.timeout)
        res.raise_for_status()
        return self.extract_text(res.json())

    @staticmethod
    def extract_text(res_data: dict) -> str:
        candidates = res_data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"No answer from Gemini: {res_data.get('promptFeedback', res_data)}")

        parts = candidates[0].get("content", {}).get("parts", [])
        # Thinking models may return thought parts before the actual answer
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        if not text:
            raise RuntimeError(f"Empty Gemini response (finishReason={candidates[0].get('finishReason')})")
        return strip_markdown_fences(text)
