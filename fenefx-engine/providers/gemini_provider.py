"""Google Gemini (Generative Language API). Implemented but untested end-
to-end (no Gemini key was available while building this) - verify against
a live key before relying on it."""

import base64
import json
from typing import Optional

import httpx

from .base import AnalysisResult, LLMProvider, ProgressCallback, extract_json_block

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(LLMProvider):
    name = "gemini"
    supports_vision = True

    async def analyze(
        self,
        system_prompt: str,
        user_text: str,
        image_bytes: Optional[bytes],
        on_progress: Optional[ProgressCallback] = None,
    ) -> AnalysisResult:
        parts = [{"text": user_text}]
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            parts.append({"inline_data": {"mime_type": "image/png", "data": b64}})

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
        }
        url = f"{BASE}/{self.model}:streamGenerateContent?alt=sse&key={self.api_key}"

        text_parts = []
        usage = {}
        first_token_seen = False

        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        return AnalysisResult(raw_text="", signal=None, error=f"{resp.status_code}: {body[:300]!r}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        candidates = chunk.get("candidates") or []
                        if not candidates:
                            continue
                        for part in candidates[0].get("content", {}).get("parts", []):
                            piece = part.get("text")
                            if piece:
                                if not first_token_seen:
                                    first_token_seen = True
                                    if on_progress:
                                        on_progress("generating", 0)
                                text_parts.append(piece)
                                if on_progress:
                                    on_progress("generating", len("".join(text_parts)))
                        if chunk.get("usageMetadata"):
                            usage = chunk["usageMetadata"]
        except httpx.HTTPError as exc:
            return AnalysisResult(raw_text="", signal=None, error=str(exc))

        full_text = "".join(text_parts)
        signal = extract_json_block(full_text)
        return AnalysisResult(
            raw_text=full_text,
            signal=signal,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
