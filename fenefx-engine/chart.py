"""Renders the FeneFX chart image: candles + homogeneous trend line +
static support/resistance levels + range box (when applicable)."""

import io


def render_chart(symbol: str, bars: list, swings: list, bias: str, trendline: dict | None,
                  levels: dict, range_box: dict | None):
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

    hlines_prices, hlines_colors = [], []
    for lvl in levels.get("resistance", []):
        hlines_prices.append(lvl)
        hlines_colors.append("#ff5c5c")
    for lvl in levels.get("support", []):
        hlines_prices.append(lvl)
        hlines_colors.append("#33d17a")

    hlines = None
    if hlines_prices:
        hlines = dict(hlines=hlines_prices, colors=hlines_colors, linestyle="dotted", linewidths=0.9, alpha=0.7)

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        hlines=hlines,
        volume=False,
        figsize=(11, 6.5),
        title=f"{symbol.rstrip('.')}  (FeneFX AI)",
        returnfig=True,
    )
    ax = axes[0]
    n = len(df)

    # Homogeneous trend line (bar-index space -> plot x uses 0..n-1)
    if trendline:
        x0, x1 = 0, n - 1
        y0 = trendline["slope"] * x0 + trendline["intercept"]
        y1 = trendline["slope"] * x1 + trendline["intercept"]
        color = "#33d17a" if bias == "bullish" else "#ff5c5c"
        ax.plot([x0, x1], [y0, y1], color=color, linewidth=1.6, alpha=0.9, zorder=5)
        for px, py in trendline["points"]:
            ax.scatter([px], [py], color=color, s=18, zorder=6)

    # Range box
    if range_box:
        ax.axhspan(range_box["bottom"], range_box["top"], color="#4da3ff", alpha=0.08, zorder=1)
        ax.axhline(range_box["top"], color="#4da3ff", linestyle="--", linewidth=1, alpha=0.6)
        ax.axhline(range_box["bottom"], color="#4da3ff", linestyle="--", linewidth=1, alpha=0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#0b0e11")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
