"""In-memory state for the FeneFX engine: current provider, mode
(manual/automatic), the single-flight busy lock, and live progress for
whichever analysis is currently running."""

import time

# Fallback only, used if windows-bridge can't be reached yet. The live list
# is fetched dynamically from windows-bridge's /api/symbols (see
# market_data.get_symbols) so newly-added pairs show up without a restart.
FALLBACK_SYMBOLS = [s.strip() for s in __import__("os").environ.get(
    "MT5_SYMBOLS",
    "XAUUSD.,GBPCAD.,USDCAD.,EURCAD.,GBPUSD.,USDJPY.,EURJPY.,USDCHF.,EURGBP.,EURUSD.,CADJPY.,GBPJPY.",
).split(",") if s.strip()]

state = {
    "active_provider": __import__("os").environ.get("LLM_ACTIVE_PROVIDER", "local"),
    "mode": "manual",              # "manual" | "automatic"
    "interval_seconds": 900,       # minimum gap between the START of two automatic cycles
    "engine_enabled": True,        # master switch: when off, no automatic scanning and manual requests are refused
    "symbol_enabled": {},          # symbol -> bool; a symbol missing here defaults to enabled
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "actions": [],                 # recent human-readable log lines, most recent first
    "last_result": None,
}

progress = {
    "busy": False,
    "symbol": None,
    "phase": None,       # "prompt" | "generating" | None
    "percent": 0,
    "eta_seconds": None,
    "started_at": None,
}

# Rolling per-provider timing stats, used to estimate ETA for future calls.
# {provider_key: {"prompt_seconds": avg, "chars_per_second": avg, "samples": n}}
timing_stats: dict = {}


def log_action(msg: str):
    state["actions"].insert(0, f"{time.strftime('%H:%M:%S')} — {msg}")
    state["actions"] = state["actions"][:40]


def update_timing(provider_key: str, prompt_seconds: float, generate_seconds: float, output_chars: int):
    stats = timing_stats.setdefault(provider_key, {"prompt_seconds": prompt_seconds, "chars_per_second": 1.0, "samples": 0})
    cps = (output_chars / generate_seconds) if generate_seconds > 0 else stats["chars_per_second"]
    n = stats["samples"]
    # exponential moving average, weighted toward recent calls
    alpha = 0.4 if n > 0 else 1.0
    stats["prompt_seconds"] = stats["prompt_seconds"] * (1 - alpha) + prompt_seconds * alpha
    stats["chars_per_second"] = stats["chars_per_second"] * (1 - alpha) + cps * alpha
    stats["samples"] = n + 1


def estimate_total_seconds(provider_key: str, expected_output_chars: int = 2500):
    stats = timing_stats.get(provider_key)
    if not stats or stats["samples"] == 0:
        return None  # unknown until we have at least one completed call
    cps = max(stats["chars_per_second"], 0.5)
    return stats["prompt_seconds"] + expected_output_chars / cps
