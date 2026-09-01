"""Persists symbols added at runtime (beyond the MT5_SYMBOLS base list in
.env) to a small JSON file, so they survive a bridge restart."""

import json
import logging
import os

log = logging.getLogger("mt5-symbols")

STORE_PATH = os.path.join(os.path.dirname(__file__), "symbols_extra.json")


def load_extra() -> list[str]:
    if not os.path.exists(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data if isinstance(s, str)]
    except Exception:
        log.exception("failed to read %s", STORE_PATH)
        return []


def add_extra(symbol: str):
    extra = load_extra()
    if symbol not in extra:
        extra.append(symbol)
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(extra, f, ensure_ascii=False, indent=2)
