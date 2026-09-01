"""Orchestrates one FeneFX-AI analysis: fetch data -> build chart + market
brief -> call the active LLM provider (streaming, with progress/ETA
tracking) -> validate the returned signal -> post to Telegram -> track."""

import asyncio
import logging
import time

import market_data
import state as st
import telegram_post
from chart import render_chart
from providers import get_provider, PROVIDER_DEFS

log = logging.getLogger("fenefx-engine")

with open("prompts/fenefx_prompt.md", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()


def _build_market_brief(symbol, bars, swings, bias, trendline, levels, range_box):
    lines = [
        f"Symbol: {symbol}",
        f"Timeframe: M15, last {len(bars)} candles",
        f"Structure bias (HH/HL vs LH/LL over recent swings): {bias}",
        f"Current price (last close): {bars[-1]['close']}",
    ]
    if trendline:
        lines.append(f"Homogeneous trend line detected: {len(trendline['points'])} touches, "
                      f"slope={trendline['slope']:.6f} (already drawn on the chart image).")
    else:
        lines.append("No valid homogeneous trend line found (fewer than 3 aligned touches).")
    if levels.get("resistance"):
        lines.append(f"Nearby static resistance levels: {[round(p, 5) for p in levels['resistance']]}")
    if levels.get("support"):
        lines.append(f"Nearby static support levels: {[round(p, 5) for p in levels['support']]}")
    if range_box:
        lines.append(f"Recent range detected: top={range_box['top']}, bottom={range_box['bottom']}")
    recent_swings = swings[-10:]
    lines.append("Recent swing points (index, type, price): " +
                  ", ".join(f"({s['index']},{s['type']},{round(s['price'], 5)})" for s in recent_swings))
    lines.append("\nDecide whether a valid FeneFX setup exists right now for this symbol, "
                  "using the rules in your system prompt. Use the attached chart image for visual judgment.")
    return "\n".join(lines)


def _validate_signal(signal: dict, current_price: float) -> tuple[bool, str]:
    if not signal or not signal.get("setup_found"):
        return False, "no setup"
    required = ["direction", "entry", "stop_loss", "tp1", "tp2", "tp3"]
    for key in required:
        if key not in signal or not isinstance(signal[key], (int, float)) and key != "direction":
            return False, f"missing/invalid field: {key}"
    direction = signal.get("direction")
    if direction not in ("bullish", "bearish"):
        return False, "invalid direction"

    entry, sl = signal["entry"], signal["stop_loss"]
    tp1, tp2, tp3 = signal["tp1"], signal["tp2"], signal["tp3"]

    if direction == "bullish":
        if not (sl < entry < tp1 <= tp2 <= tp3 or sl < entry < tp1 < tp2 < tp3):
            return False, "levels not correctly ordered for a bullish setup (need sl < entry < tp1 <= tp2 <= tp3)"
    else:
        if not (sl > entry > tp1 >= tp2 >= tp3 or sl > entry > tp1 > tp2 > tp3):
            return False, "levels not correctly ordered for a bearish setup (need sl > entry > tp1 >= tp2 >= tp3)"

    # Sanity bound: entry shouldn't be wildly far from the live price (the
    # model may have picked a stale price from the data window).
    if current_price and abs(entry - current_price) / current_price > 0.05:
        return False, "entry is implausibly far from current price"

    signal["confidence"] = max(0.0, min(1.0, float(signal.get("confidence", 0) or 0)))
    return True, "ok"


async def run_analysis(symbol: str) -> dict:
    """Runs one full analysis for a symbol. Raises if the busy lock is held."""
    if st.progress["busy"]:
        raise RuntimeError("another analysis is already running")

    st.progress.update(busy=True, symbol=symbol, phase="prompt", percent=2, started_at=time.time(), eta_seconds=None)
    provider_key = st.state["active_provider"]
    provider_label = PROVIDER_DEFS.get(provider_key, {}).get("label", provider_key)

    try:
        provider = get_provider(provider_key)
        if not provider.is_configured():
            raise RuntimeError(f"provider '{provider_key}' is not configured (missing API key/model/base URL)")

        bars = await market_data.fetch_candles(symbol, tf="15m", count=150)
        if len(bars) < 30:
            raise RuntimeError("not enough candle data returned by windows-bridge")

        swings = market_data.find_swings(bars)
        bias = market_data.structure_bias(swings)
        trendline = market_data.find_homogeneous_trendline(bars, swings, bias)
        levels = market_data.find_static_levels(swings)
        range_box = market_data.detect_range(bars, swings, bias)

        chart_bytes = render_chart(symbol, bars, swings, bias, trendline, levels, range_box)
        market_brief = _build_market_brief(symbol, bars, swings, bias, trendline, levels, range_box)

        est_total = st.estimate_total_seconds(provider_key)
        call_start = time.time()
        first_token_at = None

        def on_progress(phase, chars_so_far):
            nonlocal first_token_at
            now = time.time()
            if phase == "generating" and first_token_at is None:
                first_token_at = now
            st.progress["phase"] = phase
            expected_chars = 2500
            pct = 40 + min(58, int(chars_so_far / expected_chars * 58))
            st.progress["percent"] = pct
            if est_total:
                remaining = max(0, est_total - (now - call_start))
                st.progress["eta_seconds"] = round(remaining)

        # Providers only report progress once tokens start streaming, but for
        # local CPU inference the wait *before* the first token can be the
        # longest part of the whole call - without this, the bar would sit
        # frozen at its starting value for minutes. Tick it forward on a
        # timer instead, using the provider's historical prompt-processing
        # time as a reference when we have one.
        est_prompt_seconds = None
        stats = st.timing_stats.get(provider_key)
        if stats and stats["samples"] > 0:
            est_prompt_seconds = stats["prompt_seconds"]

        async def _prompt_ticker():
            while first_token_at is None:
                await asyncio.sleep(1)
                if first_token_at is not None:
                    break
                elapsed = time.time() - call_start
                reference = est_prompt_seconds or 90  # generic guess until we have real stats
                st.progress["phase"] = "prompt"
                st.progress["percent"] = 2 + min(38, int(elapsed / reference * 38))
                st.progress["eta_seconds"] = round(max(0, reference - elapsed)) if est_prompt_seconds else None

        ticker_task = asyncio.create_task(_prompt_ticker())
        try:
            image_bytes = chart_bytes if provider.supports_vision else None
            result = await provider.analyze(SYSTEM_PROMPT, market_brief, image_bytes, on_progress=on_progress)
        finally:
            ticker_task.cancel()

        call_end = time.time()
        prompt_seconds = (first_token_at or call_end) - call_start
        generate_seconds = max(0.001, call_end - (first_token_at or call_end))
        st.update_timing(provider_key, prompt_seconds, generate_seconds, len(result.raw_text))

        if result.error:
            st.log_action(f"FeneFX-AI {symbol}: provider error — {result.error}")
            return {"symbol": symbol, "ok": False, "error": result.error}

        ok, reason = _validate_signal(result.signal, bars[-1]["close"])
        if not ok:
            st.log_action(f"FeneFX-AI {symbol}: no valid signal ({reason})")
            return {"symbol": symbol, "ok": True, "signal_found": False, "reason": reason, "raw_text": result.raw_text}

        message_id = await telegram_post.emit_signal(symbol, result.signal, chart_bytes, provider_label)
        st.log_action(f"FeneFX-AI {symbol}: {result.signal['direction'].upper()} signal posted "
                       f"({result.signal.get('setup_type', '')}, confidence {result.signal['confidence']*100:.0f}%)")

        return {
            "symbol": symbol,
            "ok": True,
            "signal_found": True,
            "signal": result.signal,
            "telegram_message_id": message_id,
            "raw_text": result.raw_text,
        }
    except Exception as exc:
        log.exception("analysis failed for %s", symbol)
        st.log_action(f"FeneFX-AI {symbol}: failed — {exc}")
        return {"symbol": symbol, "ok": False, "error": str(exc)}
    finally:
        st.progress.update(busy=False, symbol=None, phase=None, percent=100, eta_seconds=0)


async def automatic_loop():
    while True:
        await asyncio.sleep(5)
        if st.state["mode"] != "automatic":
            continue
        if st.progress["busy"]:
            continue
        now = time.time()
        last = st.state["last_cycle_started_at"]
        if last and (now - last) < st.state["interval_seconds"]:
            continue

        st.state["last_cycle_started_at"] = now
        st.log_action("Automatic cycle started")
        for symbol in st.SYMBOLS:
            if st.state["mode"] != "automatic":
                break
            try:
                await run_analysis(symbol)
            except RuntimeError:
                break  # busy lock held by a manual request; resume next cycle
            await asyncio.sleep(1)
        st.state["last_cycle_finished_at"] = time.time()
        st.log_action("Automatic cycle finished")


async def tracking_loop():
    """Independent of mode - a signal opened manually still needs its
    SL/TP outcome tracked, so this runs continuously on its own cadence."""
    while True:
        await asyncio.sleep(20)
        try:
            await telegram_post.track_open_signals()
        except Exception:
            log.exception("signal tracking failed")
