"""Fetches candles from the windows-bridge and computes the extra FeneFX
chart geometry (swings, homogeneous trend lines, static levels, range
box) that the sweep-engine's analysis.py doesn't need."""

import os
import time

import httpx

import state as st

WINDOWS_BRIDGE_URL = os.environ.get("WINDOWS_BRIDGE_URL", "http://host.docker.internal:8000")

_symbols_cache = {"list": None, "at": 0}
SYMBOLS_CACHE_SECONDS = 20


async def fetch_candles(symbol: str, tf: str = "15m", count: int = 150):
    url = f"{WINDOWS_BRIDGE_URL}/api/analysis/{symbol}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params={"tf": tf})
        resp.raise_for_status()
        data = resp.json()
    return data.get("candles", [])[-count:]


async def get_symbols(force_refresh: bool = False) -> list[str]:
    """The live symbol list, fetched from windows-bridge (so pairs added at
    runtime show up here too) and cached briefly to avoid hammering it."""
    now = time.time()
    if not force_refresh and _symbols_cache["list"] and (now - _symbols_cache["at"]) < SYMBOLS_CACHE_SECONDS:
        return _symbols_cache["list"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{WINDOWS_BRIDGE_URL}/api/symbols")
            resp.raise_for_status()
            symbols = resp.json().get("symbols", [])
        if symbols:
            _symbols_cache["list"] = symbols
            _symbols_cache["at"] = now
            return symbols
    except Exception:
        pass
    return _symbols_cache["list"] or st.FALLBACK_SYMBOLS


# ---------------------------------------------------------------- Swings --

def find_swings(bars, left=2, right=2):
    swings = []
    n = len(bars)
    for i in range(left, n - right):
        window_highs = [bars[j]["high"] for j in range(i - left, i + right + 1)]
        window_lows = [bars[j]["low"] for j in range(i - left, i + right + 1)]
        if bars[i]["high"] == max(window_highs) and window_highs.count(bars[i]["high"]) == 1:
            swings.append({"index": i, "time": bars[i]["time"], "price": bars[i]["high"], "type": "high"})
        if bars[i]["low"] == min(window_lows) and window_lows.count(bars[i]["low"]) == 1:
            swings.append({"index": i, "time": bars[i]["time"], "price": bars[i]["low"], "type": "low"})
    swings.sort(key=lambda s: s["index"])
    return swings


def structure_bias(swings):
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return "neutral"


# ------------------------------------------------------------ Trend lines --

def _fit_line(points):
    """Least-squares line through (index, price) points -> (slope, intercept)."""
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def find_homogeneous_trendline(bars, swings, bias, touch_tolerance_frac=0.0015):
    """Uptrend -> fit through lows; downtrend -> fit through highs.
    Requires >=3 points within tolerance of the fitted line. Returns
    {slope, intercept, points} in (bar-index, price) space, or None."""
    if bias == "bullish":
        pts_all = [(s["index"], s["price"]) for s in swings if s["type"] == "low"]
    elif bias == "bearish":
        pts_all = [(s["index"], s["price"]) for s in swings if s["type"] == "high"]
    else:
        return None
    if len(pts_all) < 3:
        return None

    avg_price = sum(p[1] for p in pts_all) / len(pts_all)
    tolerance = avg_price * touch_tolerance_frac * 50  # loose band, swing points aren't perfectly linear

    # Try progressively larger subsets from the most recent points backwards,
    # keep the best-fitting line (most points within tolerance).
    best = None
    for start in range(0, max(1, len(pts_all) - 2)):
        subset = pts_all[start:]
        if len(subset) < 3:
            continue
        slope, intercept = _fit_line(subset)
        touches = [p for p in subset if abs((slope * p[0] + intercept) - p[1]) <= tolerance]
        if len(touches) >= 3 and (best is None or len(touches) > len(best["points"])):
            best = {"slope": slope, "intercept": intercept, "points": touches}

    if not best:
        return None
    return best


# --------------------------------------------------------------- Levels ---

def find_static_levels(swings, max_levels=4):
    """Unmitigated-ish static levels: most extreme recent swing highs/lows,
    deduped, most recent first. Returned as simple price levels for the
    chart overlay."""
    highs = sorted((s["price"] for s in swings if s["type"] == "high"), reverse=True)
    lows = sorted((s["price"] for s in swings if s["type"] == "low"))
    return {
        "resistance": highs[:max_levels],
        "support": lows[:max_levels],
    }


# ----------------------------------------------------------------- Range --

def detect_range(bars, swings, bias, lookback=40):
    """If structure is neutral (no clear trend) over the recent window,
    return a (top, bottom) range box; else None."""
    if bias != "neutral":
        return None
    recent = bars[-lookback:]
    if len(recent) < 10:
        return None
    top = max(b["high"] for b in recent)
    bottom = min(b["low"] for b in recent)
    if top <= bottom:
        return None
    return {"top": top, "bottom": bottom}
