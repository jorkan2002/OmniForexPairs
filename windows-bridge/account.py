"""Account/positions/history reporting for the dashboard tab. Read-only."""

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5


def get_account_summary():
    info = mt5.account_info()
    if info is None:
        return None
    return {
        "login": info.login,
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "margin_free": info.margin_free,
        "margin_level": info.margin_level,
        "currency": info.currency,
        "floating_profit": info.profit,
    }


def _risk_money(pos):
    if not pos.sl:
        return None
    si = mt5.symbol_info(pos.symbol)
    if not si or not si.trade_tick_size:
        return None
    distance = abs(pos.price_open - pos.sl)
    ticks = distance / si.trade_tick_size
    return ticks * si.trade_tick_value * pos.volume


def get_open_positions():
    positions = mt5.positions_get()
    if positions is None:
        return [], 0.0
    out = []
    total_risk = 0.0
    for p in positions:
        risk = _risk_money(p)
        if risk:
            total_risk += risk
        out.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume": p.volume,
            "price_open": p.price_open,
            "price_current": p.price_current,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit,
            "swap": p.swap,
            "time": p.time,
            "risk_money": risk,
        })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out, total_risk


def get_closed_deals(days=31):
    date_from = datetime.now(timezone.utc) - timedelta(days=days)
    date_to = datetime.now(timezone.utc) + timedelta(days=1)
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        return []
    out = []
    for d in deals:
        if d.entry != mt5.DEAL_ENTRY_OUT:
            continue
        out.append({
            "ticket": d.ticket,
            "position_id": d.position_id,
            "symbol": d.symbol,
            "type": "sell" if d.type == mt5.DEAL_TYPE_SELL else "buy",
            "volume": d.volume,
            "price": d.price,
            "profit": d.profit,
            "commission": d.commission,
            "swap": d.swap,
            "net": d.profit + d.commission + d.swap,
            "time": d.time,
        })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


def _period_sum(deals, cutoff_ts):
    return sum(d["net"] for d in deals if d["time"] >= cutoff_ts)


def dashboard_data():
    acct = get_account_summary()
    positions, total_risk = get_open_positions()
    floating_pnl = sum(p["profit"] for p in positions)
    closed = get_closed_deals(days=31)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = (now - timedelta(days=7)).timestamp()
    month_start = (now - timedelta(days=30)).timestamp()

    daily = _period_sum(closed, today_start)
    weekly = _period_sum(closed, week_start)
    monthly = _period_sum(closed, month_start)

    balance = acct["balance"] if acct else None

    def pct(period_net):
        # % is against an estimated start-of-period balance (current balance
        # minus this period's realized P&L), not current balance - otherwise
        # a large realized loss makes the % look absurd once balance has
        # already shrunk. Deposits/withdrawals during the period are not
        # accounted for, so this is still an approximation.
        if balance is None:
            return None
        start_balance = balance - period_net
        if not start_balance:
            return None
        return period_net / start_balance * 100

    def risk_pct():
        if not balance:
            return None
        return total_risk / balance * 100

    return {
        "account": acct,
        "positions": positions,
        "floating_pnl": floating_pnl,
        "total_risk_money": total_risk,
        "total_risk_pct": risk_pct(),
        "closed_deals": closed[:50],
        "closed_pnl_total": sum(d["net"] for d in closed),
        "daily_pnl": daily,
        "daily_pnl_pct": pct(daily),
        "weekly_pnl": weekly,
        "weekly_pnl_pct": pct(weekly),
        "monthly_pnl": monthly,
        "monthly_pnl_pct": pct(monthly),
    }
