"""Common interface every LLM provider adapter implements, plus shared
helpers (JSON-block extraction, streaming progress callback shape)."""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class AnalysisResult:
    raw_text: str
    signal: Optional[dict]
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None


# Called as on_progress(phase, chars_or_tokens_so_far) while streaming.
# phase is "prompt" (waiting for first token) or "generating".
ProgressCallback = Callable[[str, int], None]


def extract_json_block(text: str) -> Optional[dict]:
    """Pull the last ```json ... ``` fenced block out of a response and
    parse it. Falls back to the last {...} span if no fence is found."""
    fences = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = fences[:] if fences else []
    if not candidates:
        braces = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
        candidates = braces[:]
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


class LLMProvider(ABC):
    name: str = "base"
    supports_vision: bool = False

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def is_configured(self) -> bool:
        return bool(self.model) and (bool(self.api_key) or self.name == "local")

    @abstractmethod
    async def analyze(
        self,
        system_prompt: str,
        user_text: str,
        image_bytes: Optional[bytes],
        on_progress: Optional[ProgressCallback] = None,
    ) -> AnalysisResult:
        """Stream a completion from the provider and return the parsed result."""
        raise NotImplementedError
