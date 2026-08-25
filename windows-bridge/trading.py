"""
Automated entry engine: M15 liquidity sweep confirmed by a same-direction
M5 sweep triggers a market order at a user-configurable fixed lot size,
with SL beyond the swept extreme and TP at the nearest opposite liquidity
pool (or a 1:2 fallback). Open positions placed by this engine (tagged
with MAGIC) are then trailed as price moves in their favor.

Trades only fire in the direction of the H4 trend (layer 2 structure bias)
- a bullish sweep is only taken if H4 structure is bullish, and likewise
for bearish. If H4 structure is neutral/ambiguous, no trade is taken,
matching the framework's own rule: unclear structure = no entry.

Every confirmed signal is posted to Telegram (see signals.py) regardless
of whether real order placement is enabled or even possible - the two are
independent. Real execution is disabled by default - toggled at runtime
via /api/trading/toggle. Only manages positions carrying MAGIC, so it
never touches manually-placed or pre-existing trades on the account.
"""

import logging
import time as _time

import MetaTrader5 as mt5

import signals
from analysis import (
    _avg_range,
    _find_recent_sweep,
    find_swings,
    get_rates,
    structure_bias,
)

log = logging.getLogger("mt5-trading")

MAGIC = 990211
RISK_REWARD_FALLBACK = 2.0
SWEEP_FRESHNESS_BARS = 3
TRAIL_TRIGGER_R = 1.0
TRAIL_ATR_MULT = 1.5

trading_state = {
    "enabled": False,
    "lot_size": 0.01,
    "last_error": None,
    "last_scan": None,
    "actions": [],  # recent log of open/trail actions, most recent first
}

last_signaled = {}  # symbol -> sweep15 index already posted to Telegram, for de-dup


def _log_action(msg: str):
    log.info(msg)
    trading_state["actions"].insert(0, msg)
    trading_state["actions"] = trading_state["actions"][:30]


def _filling_mode(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    modes = info.filling_mode
    if modes & 1:
        return mt5.ORDER_FILLING_FOK
    if modes & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _lot_size(symbol, requested_lots):
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    step = info.volume_step or 0.01
    lots = round(requested_lots / step) * step
    lots = max(info.volume_min, min(info.volume_max, lots))
    return round(lots, 2)


def _clamp_stops(symbol, direction, price, sl, tp):
    """Push sl/tp out to the broker's minimum stop distance from price if
    they're closer than that (cause of 'Invalid stops' rejections)."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return sl, tp
    min_dist = max(info.trade_stops_level, info.trade_freeze_level) * info.point
    if min_dist <= 0:
        return sl, tp
    if direction == "bullish":
        sl = min(sl, price - min_dist)
        tp = max(tp, price + min_dist)
    else:
        sl = max(sl, price + min_dist)
        tp = min(tp, price - min_dist)
    return sl, tp


def _pip_size(symbol):
    """Classic industry-standard pip (not the broker's 5th/3rd fractional
    digit): 0.01 for JPY pairs, 0.1 for gold, 0.0001 for everything else."""
    if symbol.upper().startswith("XAU"):
        return 0.1
    if "JPY" in symbol.upper():
        return 0.01
    return 0.0001


def _sl_extra_buffer(symbol):
    """Extra distance pushed onto the SL beyond the swept level, to survive
    a wick before the real reversal instead of getting stopped out early:
    10 pips for gold, 5 pips for everything else."""
    pips = 10 if symbol.upper().startswith("XAU") else 5
    return pips * _pip_size(symbol)


def _h4_trend_bias(symbol):
    h4 = get_rates(symbol, mt5.TIMEFRAME_H4, 120)
    if len(h4) < 30:
        return "neutral"
    return structure_bias(find_swings(h4))


def _liquidity_pools(swings, direction, reference_price, n=3):
    """Up to n opposite-side swing levels beyond reference_price, nearest first."""
    if direction == "bullish":
        cands = sorted(s["price"] for s in swings if s["type"] == "high" and s["price"] > reference_price)
    else:
        cands = sorted(
            (s["price"] for s in swings if s["type"] == "low" and s["price"] < reference_price),
            reverse=True,
        )
    return cands[:n]


def _tp_levels(swings, direction, entry, risk):
    """3 take-profit targets: nearest liquidity pools where available,
    filled out with 1R/2R/3R multiples where structure doesn't give enough."""
    pools = _liquidity_pools(swings, direction, entry, n=3)
    tps = []
    for i in range(3):
        if i < len(pools):
            tps.append(pools[i])
        else:
            mult = i + 1
            tps.append(entry + risk * mult if direction == "bullish" else entry - risk * mult)
    tps.sort(reverse=(direction == "bearish"))
    return tps


def detect_signal(symbol):
    """M15 sweep confirmed by a matching-direction M5 sweep, aligned with
    the H4 trend. Returns entry/SL/3 TPs plus chart bars for the Telegram
    post, or None. Used for both the Telegram signal feed and (optionally)
    real order placement - independent of MT5 trading permission."""
    m15 = get_rates(symbol, mt5.TIMEFRAME_M15, 150)
    m5 = get_rates(symbol, mt5.TIMEFRAME_M5, 150)
    if len(m15) < 30 or len(m5) < 30:
        return None

    m15_swings = find_swings(m15)
    m5_swings = find_swings(m5)
    sweep15 = _find_recent_sweep(m15, m15_swings)
    sweep5 = _find_recent_sweep(m5, m5_swings)
    if not sweep15 or not sweep5:
        return None
    if sweep15["direction"] != sweep5["direction"]:
        return None
    if (len(m15) - 1 - sweep15["index"]) > SWEEP_FRESHNESS_BARS:
        return None
    if (len(m5) - 1 - sweep5["index"]) > SWEEP_FRESHNESS_BARS:
        return None

    direction = sweep15["direction"]
    if _h4_trend_bias(symbol) != direction:
        return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    entry = tick.ask if direction == "bullish" else tick.bid

    buffer = _avg_range(m15) * 0.15 + _sl_extra_buffer(symbol)
    sl = sweep15["price"] - buffer if direction == "bullish" else sweep15["price"] + buffer

    # sl is derived from the swept M15 swing, which can be a few candles
    # stale - if price has since moved past it again, sl can end up on the
    # wrong side of (or too close to) the live entry price. abs() would
    # silently hide that instead of catching it, so check the sign
    # explicitly and reject rather than emit a broken signal.
    risk = entry - sl if direction == "bullish" else sl - entry
    if risk <= 0:
        return None

    tps = _tp_levels(m15_swings, direction, entry, risk)
    sl, tps[0] = _clamp_stops(symbol, direction, entry, sl, tps[0])

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tps": tps,
        "sweep_index": sweep15["index"],
        "chart_bars": m5[-100:],
    }


def _open_position(setup, lot_size):
    symbol = setup["symbol"]
    info = mt5.symbol_info(symbol)
    if info is None:
        return

    direction = setup["direction"]
    price = setup["entry"]
    sl = setup["sl"]
    tp = setup["tps"][0]  # a single MT5 position can only carry one TP; TP1 is used live

    volume = _lot_size(symbol, lot_size)
    if not volume or volume <= 0:
        return

    order_type = mt5.ORDER_TYPE_BUY if direction == "bullish" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "sweep-m15-m5",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(symbol),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else None
        comment = result.comment if result else "no response"
        trading_state["last_error"] = f"{symbol}: order_send failed ({code} {comment})"
        _log_action(f"FAILED open {direction} {symbol}: {code} {comment}")
        return

    _log_action(f"Opened {direction.upper()} {symbol} (H4 trend confirmed) vol={volume} sl={sl:.5f} tp={tp:.5f}")


def _trail_position(pos):
    symbol = pos.symbol
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    m5 = get_rates(symbol, mt5.TIMEFRAME_M5, 60)
    if len(m5) < 20:
        return

    trail_dist = _avg_range(m5, 14) * TRAIL_ATR_MULT
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    risk = abs(pos.price_open - pos.sl) if pos.sl else None

    new_sl = None
    if is_buy:
        candidate = price - trail_dist
        if risk and (price - pos.price_open) >= risk * TRAIL_TRIGGER_R:
            candidate = max(candidate, pos.price_open)
        if candidate > (pos.sl or -1e18) and candidate < price:
            new_sl = candidate
    else:
        candidate = price + trail_dist
        if risk and (pos.price_open - price) >= risk * TRAIL_TRIGGER_R:
            candidate = min(candidate, pos.price_open)
        if (pos.sl == 0 or candidate < pos.sl) and candidate > price:
            new_sl = candidate

    if new_sl is None:
        return

    info = mt5.symbol_info(symbol)
    if info:
        min_dist = max(info.trade_stops_level, info.trade_freeze_level) * info.point
        if min_dist > 0:
            if is_buy and price - new_sl < min_dist:
                new_sl = price - min_dist
            elif not is_buy and new_sl - price < min_dist:
                new_sl = price + min_dist

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": pos.ticket,
        "sl": new_sl,
        "tp": pos.tp,
        "magic": MAGIC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        _log_action(f"Trailed SL {symbol} #{pos.ticket} -> {new_sl:.5f}")
    else:
        code = result.retcode if result else None
        trading_state["last_error"] = f"{symbol}: trail failed ({code})"


def run_cycle(symbols):
    """One pass: trail our open positions, scan every symbol for a fresh
    confirmed signal (posting to Telegram whenever one appears - this does
    NOT depend on trading being enabled or MT5 permitting real orders),
    optionally place a real order when auto-trading is enabled, then check
    tracked Telegram signals against live price for SL/TP outcomes."""
    trading_state["last_scan"] = _time.time()

    positions = mt5.positions_get() or []
    our_positions = [p for p in positions if p.magic == MAGIC]
    held_symbols = {p.symbol for p in our_positions}

    for pos in our_positions:
        try:
            _trail_position(pos)
        except Exception as exc:
            log.exception("trail failed for %s", pos.symbol)
            trading_state["last_error"] = f"{pos.symbol}: trail exception {exc}"

    ti = mt5.terminal_info()
    can_trade = (
        trading_state["enabled"]
        and ti is not None
        and ti.trade_allowed
    )
    if trading_state["enabled"] and ti is not None and not ti.trade_allowed:
        trading_state["last_error"] = "AutoTrading is disabled in the MT5 terminal (click the AutoTrading button)"

    for symbol in symbols:
        try:
            setup = detect_signal(symbol)
        except Exception as exc:
            log.exception("signal detection failed for %s", symbol)
            continue
        if not setup:
            continue

        if last_signaled.get(symbol) != setup["sweep_index"]:
            last_signaled[symbol] = setup["sweep_index"]
            signals.emit_signal(setup)

        if can_trade and symbol not in held_symbols:
            try:
                _open_position(setup, trading_state["lot_size"])
            except Exception as exc:
                log.exception("open failed for %s", symbol)
                trading_state["last_error"] = f"{symbol}: open exception {exc}"

    try:
        signals.track_open_signals()
    except Exception:
        log.exception("signal tracking failed")

    try:
        signals.check_session_ends()
    except Exception:
        log.exception("session summary check failed")
