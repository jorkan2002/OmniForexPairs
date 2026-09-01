"""Posts FeneFX-AI signals to the same Telegram channel as the sweep
engine, but visually tagged so the two are distinguishable, and tracks
each signal's SL/TP outcome against live price the same way."""

import logging
import os
import time

import httpx

log = logging.getLogger("fenefx-telegram")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WINDOWS_BRIDGE_URL = os.environ.get("WINDOWS_BRIDGE_URL", "http://host.docker.internal:8000")

DIRECTION_FA = {"bullish": "خرید (BUY)", "bearish": "فروش (SELL)"}

open_signals = []


def telegram_enabled():
    return bool(BOT_TOKEN and CHAT_ID)


def _decimals(symbol):
    u = symbol.upper()
    if "JPY" in u:
        return 3
    if u.startswith("XAU"):
        return 2
    return 5


async def _api_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=data, files=files)
    if not r.ok:
        raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text[:300]}")
    return r.json()["result"]


async def _send_photo(photo_bytes, caption):
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    return (await _api_call("sendPhoto", data=data, files=files))["message_id"]


async def _send_message(text, reply_to=None):
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return (await _api_call("sendMessage", data=data))["message_id"]


def _caption(symbol, signal, provider_label):
    dec = _decimals(symbol)
    direction = signal["direction"]
    emoji = "🟢" if direction == "bullish" else "🔴"
    name = symbol.rstrip(".")
    entry, sl = signal["entry"], signal["stop_loss"]
    tp1, tp2, tp3 = signal["tp1"], signal["tp2"], signal["tp3"]
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk else 0.0
    return (
        f"🧠 <b>FeneFX AI — {emoji} {name} ({DIRECTION_FA.get(direction, direction)})</b>\n"
        f"<i>{signal.get('setup_type', '')} · {provider_label}</i>\n\n"
        f"ورود (Entry): <code>{entry:.{dec}f}</code>\n"
        f"حد ضرر (SL): <code>{sl:.{dec}f}</code>\n"
        f"هدف ۱ (TP1): <code>{tp1:.{dec}f}</code>\n"
        f"هدف ۲ (TP2): <code>{tp2:.{dec}f}</code>\n"
        f"هدف ۳ (TP3): <code>{tp3:.{dec}f}</code>\n\n"
        f"ریسک:ریوارد تا TP1 &#8776; 1:{rr1:.1f}\n"
        f"اطمینان مدل: {signal.get('confidence', 0) * 100:.0f}٪\n"
        f"مبنا: {signal.get('rule_basis', '-')}"
    )


async def emit_signal(symbol, signal, chart_bytes, provider_label):
    if not telegram_enabled():
        return None
    try:
        caption = _caption(symbol, signal, provider_label)
        message_id = await _send_photo(chart_bytes, caption)
        open_signals.append({
            "symbol": symbol,
            "direction": signal["direction"],
            "entry": signal["entry"],
            "sl": signal["stop_loss"],
            "tps": [signal["tp1"], signal["tp2"], signal["tp3"]],
            "message_id": message_id,
            "tp_hit": [False, False, False],
            "closed": False,
            "outcome": None,
            "opened_at": time.time(),
        })
        del open_signals[:-200]
        return message_id
    except Exception:
        log.exception("failed to send FeneFX-AI signal for %s", symbol)
        return None


async def track_open_signals():
    if not telegram_enabled():
        return
    async with httpx.AsyncClient(timeout=15) as client:
        for sig in open_signals:
            if sig["closed"]:
                continue
            try:
                resp = await client.get(f"{WINDOWS_BRIDGE_URL}/api/prices")
                resp.raise_for_status()
                entry = resp.json().get("symbols", {}).get(sig["symbol"])
            except Exception:
                continue
            if not entry or entry.get("bid") is None:
                continue
            is_buy = sig["direction"] == "bullish"
            price = entry["bid"] if is_buy else entry["ask"]

            hit_sl = price <= sig["sl"] if is_buy else price >= sig["sl"]
            if hit_sl:
                try:
                    await _send_message("❌ <b>Stop Loss خورد</b>", reply_to=sig["message_id"])
                except Exception:
                    log.exception("failed to send SL update for %s", sig["symbol"])
                sig["closed"] = True
                sig["outcome"] = "sl"
                continue

            for i, tp in enumerate(sig["tps"]):
                if sig["tp_hit"][i]:
                    continue
                hit = price >= tp if is_buy else price <= tp
                if not hit:
                    break
                sig["tp_hit"][i] = True
                try:
                    final = " — سیگنال بسته شد 🎯" if i == 2 else ""
                    await _send_message(f"✅ <b>TP{i + 1} خورد</b>{final}", reply_to=sig["message_id"])
                except Exception:
                    log.exception("failed to send TP update for %s", sig["symbol"])
                if i == 2:
                    sig["closed"] = True
                    sig["outcome"] = "tp"
