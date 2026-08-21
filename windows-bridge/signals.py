"""
Telegram signal posting + tracking.

Fires whenever the strategy considers a position worth taking (a confirmed
M15+M5 sweep aligned with the H4 trend) - independent of whether an actual
MT5 order can be placed (broker/terminal permissions don't matter here,
only price data does). Each signal is posted as a chart image with entry/
SL/3 TPs marked, then tracked against live price until SL or a TP is hit,
at which point a follow-up reply is posted to the same message.

In-memory only: open_signals / last_signaled reset on bridge restart.
"""

import io
import logging
import os
import time

import MetaTrader5 as mt5
import requests

log = logging.getLogger("mt5-signals")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DIRECTION_FA = {"bullish": "خرید (BUY)", "bearish": "فروش (SELL)"}

open_signals = []   # list of dicts, most recent last
last_signaled = {}  # symbol -> sweep15 index already posted, to avoid duplicates


def telegram_enabled():
    return bool(BOT_TOKEN and CHAT_ID)


def _decimals(symbol):
    u = symbol.upper()
    if "JPY" in u:
        return 3
    if u.startswith("XAU"):
        return 2
    return 5


def _api_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=data, files=files, timeout=20)
    if not r.ok:
        # Don't let r.raise_for_status() leak the bot token via the URL in logs.
        raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text[:300]}")
    return r.json()["result"]


def _send_photo(photo_bytes, caption):
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    return _api_call("sendPhoto", data=data, files=files)["message_id"]


def _send_message(text, reply_to=None):
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return _api_call("sendMessage", data=data)["message_id"]


def render_chart(symbol, bars, entry, sl, tps, direction):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    import pandas as pd

    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})

    mc = mpf.make_marketcolors(up="#33d17a", down="#ff5c5c", edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(
        marketcolors=mc,
        facecolor="#0b0e11",
        edgecolor="#2a313b",
        gridcolor="#1c2128",
        figcolor="#0b0e11",
        rc={"axes.labelcolor": "#9aa4b0", "xtick.color": "#9aa4b0", "ytick.color": "#9aa4b0"},
    )

    hlines = dict(
        hlines=[entry, sl, tps[0], tps[1], tps[2]],
        colors=["#ffb020", "#ff5c5c", "#33d17a", "#33d17a", "#33d17a"],
        linestyle="--",
        linewidths=1.2,
    )

    dec = _decimals(symbol)
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        hlines=hlines,
        volume=False,
        figsize=(10, 6),
        title=f"{symbol.rstrip('.')}  {DIRECTION_FA.get(direction, direction)}",
        returnfig=True,
    )
    ax = axes[0]
    xlim = ax.get_xlim()
    labels = [
        ("Entry", entry, "#ffb020"),
        ("SL", sl, "#ff5c5c"),
        ("TP1", tps[0], "#33d17a"),
        ("TP2", tps[1], "#33d17a"),
        ("TP3", tps[2], "#33d17a"),
    ]
    for label, price, color in labels:
        ax.text(xlim[1], price, f" {label} {price:.{dec}f}", color=color, fontsize=8, va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#0b0e11")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _caption(symbol, direction, entry, sl, tps, rr1):
    dec = _decimals(symbol)
    emoji = "🟢" if direction == "bullish" else "🔴"
    name = symbol.rstrip(".")
    return (
        f"{emoji} <b>سیگنال جدید — {name} ({DIRECTION_FA[direction]})</b>\n\n"
        f"ورود (Entry): <code>{entry:.{dec}f}</code>\n"
        f"حد ضرر (SL): <code>{sl:.{dec}f}</code>\n"
        f"هدف ۱ (TP1): <code>{tps[0]:.{dec}f}</code>\n"
        f"هدف ۲ (TP2): <code>{tps[1]:.{dec}f}</code>\n"
        f"هدف ۳ (TP3): <code>{tps[2]:.{dec}f}</code>\n\n"
        f"ریسک:ریوارد تا TP1 &#8776; 1:{rr1:.1f}\n"
        f"مبنا: Liquidity Sweep M15 + تایید M5، هم‌جهت با روند H4"
    )


def emit_signal(setup):
    if not telegram_enabled():
        return
    symbol = setup["symbol"]
    try:
        direction = setup["direction"]
        entry, sl, tps = setup["entry"], setup["sl"], setup["tps"]
        img = render_chart(symbol, setup["chart_bars"], entry, sl, tps, direction)
        risk = abs(entry - sl)
        rr1 = abs(tps[0] - entry) / risk if risk else 0.0
        caption = _caption(symbol, direction, entry, sl, tps, rr1)
        message_id = _send_photo(img, caption)
        open_signals.append({
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tps": list(tps),
            "message_id": message_id,
            "tp_hit": [False, False, False],
            "closed": False,
            "opened_at": time.time(),
        })
        del open_signals[:-200]  # keep unbounded growth in check
        log.info("Telegram signal sent for %s (%s)", symbol, direction)
    except Exception:
        log.exception("failed to send telegram signal for %s", symbol)


def track_open_signals():
    if not telegram_enabled():
        return
    for sig in open_signals:
        if sig["closed"]:
            continue
        tick = mt5.symbol_info_tick(sig["symbol"])
        if tick is None:
            continue
        is_buy = sig["direction"] == "bullish"
        price = tick.bid if is_buy else tick.ask

        hit_sl = price <= sig["sl"] if is_buy else price >= sig["sl"]
        if hit_sl:
            try:
                _send_message("❌ <b>Stop Loss خورد</b>", reply_to=sig["message_id"])
            except Exception:
                log.exception("failed to send SL update for %s", sig["symbol"])
            sig["closed"] = True
            continue

        for i, tp in enumerate(sig["tps"]):
            if sig["tp_hit"][i]:
                continue
            hit = price >= tp if is_buy else price <= tp
            if not hit:
                break  # targets are ordered by distance; nearer ones must hit first
            sig["tp_hit"][i] = True
            try:
                final = " — سیگنال بسته شد 🎯" if i == 2 else ""
                _send_message(f"✅ <b>TP{i + 1} خورد</b>{final}", reply_to=sig["message_id"])
            except Exception:
                log.exception("failed to send TP update for %s", sig["symbol"])
            if i == 2:
                sig["closed"] = True
