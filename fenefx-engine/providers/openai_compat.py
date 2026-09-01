"""Works with any OpenAI-compatible chat/completions endpoint: OpenAI
itself, a local Ollama server, ArvanCloud (if it exposes an OpenAI-style
API), OpenRouter, or any other "Custom" endpoint the user points at."""

import base64
import json
from typing import Optional

import httpx

from .base import AnalysisResult, LLMProvider, ProgressCallback, extract_json_block


class OpenAICompatProvider(LLMProvider):
    supports_vision = True  # depends on the actual model; caller should warn if unsure

    def __init__(self, api_key: str, model: str, base_url: str, name: str = "openai", vision: bool = True):
        super().__init__(api_key, model, base_url)
        self.name = name
        self.supports_vision = vision

    def is_configured(self) -> bool:
        return bool(self.model) and bool(self.base_url) and (bool(self.api_key) or self.name == "local")

    async def analyze(
        self,
        system_prompt: str,
        user_text: str,
        image_bytes: Optional[bytes],
        on_progress: Optional[ProgressCallback] = None,
    ) -> AnalysisResult:
        content = [{"type": "text", "text": user_text}]
        if image_bytes and self.supports_vision:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "stream": True,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = self.base_url.rstrip("/") + "/chat/completions"
        text_parts = []
        usage = {}
        first_token_seen = False

        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        return AnalysisResult(raw_text="", signal=None, error=f"{resp.status_code}: {body[:300]!r}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            if not first_token_seen:
                                first_token_seen = True
                                if on_progress:
                                    on_progress("generating", 0)
                            text_parts.append(piece)
                            if on_progress:
                                on_progress("generating", len("".join(text_parts)))
                        if chunk.get("usage"):
                            usage = chunk["usage"]
        except httpx.HTTPError as exc:
            return AnalysisResult(raw_text="", signal=None, error=str(exc))

        full_text = "".join(text_parts)
        signal = extract_json_block(full_text)
        return AnalysisResult(
            raw_text=full_text,
            signal=signal,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
