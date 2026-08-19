"""
Multi-layer technical analysis engine (structure bias, daily bias, kill-zone
timing, liquidity-sweep / MSS / order-block / FVG entry detection) driven
entirely off MT5 price data.

Layer 1 (weekly fundamental bias) and COT positioning are NOT implemented
here - they need an external news/calendar/COT data feed that isn't wired
up. The frontend exposes a manual override for layer 1 instead of faking it.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

NY_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- Layer 4 --
# Kill-zone session filter (New York time)

def _ny_hour_frac(epoch_seconds: float) -> float:
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone(NY_TZ)
    return dt.hour + dt.minute / 60


def current_kill_zone():
    now_ny = datetime.now(NY_TZ)
    frac = now_ny.hour + now_ny.minute / 60
    active = []
    if frac >= 19:
        active.append("Asia")
    if 2 <= frac < 5:
        active.append("London")
    if 7 <= frac < 10:
        active.append("New York")
    if 10 <= frac < 12:
        active.append("London Close")
    return {
        "ny_time": now_ny.strftime("%H:%M"),
        "active": active,
        "label": " + ".join(active) if active else "Out of Kill Zone",
    }


# --------------------------------------------------------------- Raw data --

def get_rates(symbol: str, timeframe: int, count: int):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        return []
    return [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rates
    ]


# ---------------------------------------------------------------- Layer 2 --
# H4 / Daily swing structure -> trend bias

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


# ---------------------------------------------------------------- Layer 3 --
# Daily bias: previous-day sweep, Asia range, daily open, position vs prior range

def daily_bias(symbol: str):
    d1 = get_rates(symbol, mt5.TIMEFRAME_D1, 3)
    if len(d1) < 2:
        return {}
    prev_day = d1[-2]
    today = d1[-1]
    tick = mt5.symbol_info_tick(symbol)
    price = tick.bid if tick else None

    h1 = get_rates(symbol, mt5.TIMEFRAME_H1, 48)
    asia_bars = [b for b in h1 if _ny_hour_frac(b["time"]) >= 19]
    asia_high = max((b["high"] for b in asia_bars), default=None)
    asia_low = min((b["low"] for b in asia_bars), default=None)

    return {
        "prev_day_high": prev_day["high"],
        "prev_day_low": prev_day["low"],
        "daily_open": today["open"],
        "current_price": price,
        "swept_prev_high": price is not None and price > prev_day["high"],
        "swept_prev_low": price is not None and price < prev_day["low"],
        "asia_high": asia_high,
        "asia_low": asia_low,
    }


# ---------------------------------------------------------------- Layer 5 --
# Liquidity sweep -> Market Structure Shift -> Order Block / FVG -> OTE entry

def _avg_body(bars, upto_index, n=20):
    start = max(0, upto_index - n)
    seg = bars[start:upto_index]
    if not seg:
        return 1e-9
    return sum(abs(b["close"] - b["open"]) for b in seg) / len(seg)


def _avg_range(bars, n=14):
    seg = bars[-n:]
    if not seg:
        return 1e-9
    return sum(b["high"] - b["low"] for b in seg) / len(seg)


def _find_recent_sweep(bars, swings, lookback=40):
    cutoff = max(0, len(bars) - lookback)
    recent_swings = [s for s in swings if s["index"] >= cutoff]
    best = None
    for s in recent_swings:
        for j in range(s["index"] + 1, len(bars)):
            bar = bars[j]
            if s["type"] == "high" and bar["high"] > s["price"] and bar["close"] < s["price"]:
                cand = {"swing_index": s["index"], "type": "high_sweep", "direction": "bearish",
                        "index": j, "price": s["price"], "time": bar["time"]}
                if best is None or cand["index"] > best["index"]:
                    best = cand
            if s["type"] == "low" and bar["low"] < s["price"] and bar["close"] > s["price"]:
                cand = {"swing_index": s["index"], "type": "low_sweep", "direction": "bullish",
                        "index": j, "price": s["price"], "time": bar["time"]}
                if best is None or cand["index"] > best["index"]:
                    best = cand
    return best


def _find_mss(bars, swings, sweep):
    direction = sweep["direction"]
    after = [s for s in swings if s["index"] > sweep["index"]]
    target_swings = [s for s in after if s["type"] == ("high" if direction == "bullish" else "low")]
    for s in target_swings:
        avg_body = _avg_body(bars, s["index"])
        for j in range(s["index"] + 1, len(bars)):
            bar = bars[j]
            body = abs(bar["close"] - bar["open"])
            if direction == "bullish" and bar["close"] > s["price"] and body > avg_body * 1.3:
                return {"swing_index": s["index"], "index": j, "time": bar["time"],
                        "price": bar["close"], "direction": "bullish"}
            if direction == "bearish" and bar["close"] < s["price"] and body > avg_body * 1.3:
                return {"swing_index": s["index"], "index": j, "time": bar["time"],
                        "price": bar["close"], "direction": "bearish"}
    return None


def _find_order_block(bars, mss):
    idx = mss["index"]
    direction = mss["direction"]
    for j in range(idx - 1, max(idx - 10, -1), -1):
        bar = bars[j]
        bullish_candle = bar["close"] > bar["open"]
        if direction == "bullish" and not bullish_candle:
            return {"index": j, "time": bar["time"], "high": bar["high"], "low": bar["low"]}
        if direction == "bearish" and bullish_candle:
            return {"index": j, "time": bar["time"], "high": bar["high"], "low": bar["low"]}
    return None


def _find_fvg(bars, mss):
    idx = mss["index"]
    if idx < 1 or idx + 1 >= len(bars):
        return None
    c1, c3 = bars[idx - 1], bars[idx + 1]
    if mss["direction"] == "bullish" and c3["low"] > c1["high"]:
        return {"top": c3["low"], "bottom": c1["high"], "index": idx}
    if mss["direction"] == "bearish" and c3["high"] < c1["low"]:
        return {"top": c1["low"], "bottom": c3["high"], "index": idx}
    return None


def _ranges_overlap(ob, fvg):
    return ob["low"] <= fvg["top"] and fvg["bottom"] <= ob["high"]


def _ote_zone(sweep, mss):
    start, end = sweep["price"], mss["price"]
    diff = end - start
    lvl_618 = end - diff * 0.618
    lvl_79 = end - diff * 0.79
    lo, hi = sorted([lvl_618, lvl_79])
    return {"top": hi, "bottom": lo, "entry": (lvl_618 + lvl_79) / 2}


def _opposite_liquidity(swings, direction, entry):
    if direction == "bullish":
        cands = [s for s in swings if s["type"] == "high" and s["price"] > entry]
        return min(cands, key=lambda s: s["price"]) if cands else None
    cands = [s for s in swings if s["type"] == "low" and s["price"] < entry]
    return max(cands, key=lambda s: s["price"]) if cands else None


def detect_setup(symbol: str, timeframe=mt5.TIMEFRAME_M5, count=150):
    bars = get_rates(symbol, timeframe, count)
    if len(bars) < 30:
        return {"state": "insufficient_data"}

    swings = find_swings(bars)
    if len(swings) < 4:
        return {"state": "no_structure"}

    sweep = _find_recent_sweep(bars, swings)
    if not sweep:
        return {"state": "waiting_for_sweep"}

    mss = _find_mss(bars, swings, sweep)
    if not mss:
        return {"state": "sweep_detected", "sweep": sweep}

    ob = _find_order_block(bars, mss)
    fvg = _find_fvg(bars, mss)
    unicorn = bool(ob and fvg and _ranges_overlap(ob, fvg))
    ote = _ote_zone(sweep, mss)

    direction = mss["direction"]
    entry = ote["entry"]
    buffer = _avg_range(bars) * 0.15
    sl = sweep["price"] - buffer if direction == "bullish" else sweep["price"] + buffer
    risk = abs(entry - sl)

    pool = _opposite_liquidity(swings, direction, entry)
    if pool:
        tp = pool["price"]
    else:
        tp = entry + risk * 2 if direction == "bullish" else entry - risk * 2

    rr = round(abs(tp - entry) / risk, 2) if risk else None

    return {
        "state": "setup_ready",
        "direction": direction,
        "sweep": sweep,
        "mss": mss,
        "order_block": ob,
        "fvg": fvg,
        "unicorn": unicorn,
        "ote": ote,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "risk_reward": rr,
    }


# ---------------------------------------------------------------- Combined --

def full_analysis(symbol: str):
    h4 = get_rates(symbol, mt5.TIMEFRAME_H4, 120)
    d1 = get_rates(symbol, mt5.TIMEFRAME_D1, 60)
    m5 = get_rates(symbol, mt5.TIMEFRAME_M5, 150)

    h4_swings = find_swings(h4)
    d1_swings = find_swings(d1)

    return {
        "symbol": symbol,
        "h4_bias": structure_bias(h4_swings),
        "daily_structure_bias": structure_bias(d1_swings),
        "daily_bias": daily_bias(symbol),
        "kill_zone": current_kill_zone(),
        "setup": detect_setup(symbol),
        "m5_candles": m5,
        "h4_swings": h4_swings[-10:],
    }
