import os

from .anthropic_provider import AnthropicProvider
from .base import AnalysisResult, LLMProvider
from .gemini_provider import GeminiProvider
from .openai_compat import OpenAICompatProvider

# Registry of provider configs read from env. Each entry: how to build the
# adapter, and whether the currently-configured model is known to support
# vision (shown as a warning in the panel when false/unknown).
PROVIDER_DEFS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "build": lambda: AnthropicProvider(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        ),
        "vision": True,
    },
    "openai": {
        "label": "OpenAI",
        "build": lambda: OpenAICompatProvider(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            name="openai",
            vision=True,
        ),
        "vision": True,
    },
    "gemini": {
        "label": "Google Gemini",
        "build": lambda: GeminiProvider(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", "gemini-1.5-pro"),
        ),
        "vision": True,
    },
    "arvancloud": {
        "label": "ArvanCloud AI",
        "build": lambda: OpenAICompatProvider(
            api_key=os.environ.get("ARVANCLOUD_API_KEY", ""),
            model=os.environ.get("ARVANCLOUD_MODEL", ""),
            base_url=os.environ.get("ARVANCLOUD_BASE_URL", ""),
            name="arvancloud",
            vision=False,  # unverified - most hosted open-source models here are text-only
        ),
        "vision": False,
    },
    "local": {
        "label": "Local (Ollama)",
        "build": lambda: OpenAICompatProvider(
            api_key=os.environ.get("LOCAL_LLM_API_KEY", "ollama"),
            model=os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:7b-instruct"),
            base_url=os.environ.get("LOCAL_LLM_BASE_URL", "http://ollama:11434/v1"),
            name="local",
            vision=False,  # default text-only model; set vision=True in .env comment if you pull a VL model
        ),
        "vision": False,
    },
    "custom": {
        "label": os.environ.get("CUSTOM_LLM_NAME", "Custom"),
        "build": lambda: OpenAICompatProvider(
            api_key=os.environ.get("CUSTOM_LLM_API_KEY", ""),
            model=os.environ.get("CUSTOM_LLM_MODEL", ""),
            base_url=os.environ.get("CUSTOM_LLM_BASE_URL", ""),
            name="custom",
            vision=True,
        ),
        "vision": True,
    },
}


def get_provider(key: str) -> LLMProvider:
    defn = PROVIDER_DEFS.get(key)
    if not defn:
        raise ValueError(f"unknown provider: {key}")
    return defn["build"]()


def list_providers():
    out = []
    for key, defn in PROVIDER_DEFS.items():
        provider = defn["build"]()
        out.append({
            "key": key,
            "label": defn["label"],
            "model": provider.model,
            "configured": provider.is_configured(),
            "vision": defn["vision"],
        })
    return out
