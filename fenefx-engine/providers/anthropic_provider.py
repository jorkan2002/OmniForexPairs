"""Anthropic Messages API (Claude), with prompt caching on the system
prompt since it's identical across every call in a scan cycle."""

import base64
import json
from typing import Optional

import httpx

from .base import AnalysisResult, LLMProvider, ProgressCallback, extract_json_block

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 1200


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_vision = True

    async def analyze(
        self,
        system_prompt: str,
        user_text: str,
        image_bytes: Optional[bytes],
        on_progress: Optional[ProgressCallback] = None,
    ) -> AnalysisResult:
        content = [{"type": "text", "text": user_text}]
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content.insert(0, {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })

        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "stream": True,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

        text_parts = []
        usage = {}
        first_token_seen = False

        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("POST", API_URL, headers=headers, json=payload) as resp:
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
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta", {})
                            piece = delta.get("text")
                            if piece:
                                if not first_token_seen:
                                    first_token_seen = True
                                    if on_progress:
                                        on_progress("generating", 0)
                                text_parts.append(piece)
                                if on_progress:
                                    on_progress("generating", len("".join(text_parts)))
                        elif etype == "message_start":
                            usage.update(event.get("message", {}).get("usage", {}))
                        elif etype == "message_delta":
                            usage.update(event.get("usage", {}))
        except httpx.HTTPError as exc:
            return AnalysisResult(raw_text="", signal=None, error=str(exc))

        full_text = "".join(text_parts)
        signal = extract_json_block(full_text)
        return AnalysisResult(
            raw_text=full_text,
            signal=signal,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
