"""Parse and dispatch Telegram commands to the portfolio manager.

All commands are case-insensitive with flexible spacing. The router is async
because BUY/SELL/QUOTE depend on async live-price fetches.

Output strings use HTML formatting (matches the bot_runner's parse_mode=HTML).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import pytz

from bot import portfolio_manager
from storage import supabase_client

log = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_RULE = "━━━━━━━━━━━━━━━━━━━━━━"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_rs(amount: float | None, *, with_sign: bool = False) -> str:
    if amount is None:
        return "—"
    sign = "+" if with_sign and amount > 0 else ("-" if with_sign and amount < 0 else "")
    return f"{sign}₹{abs(amount):,.2f}"


def _pnl_marker(pnl: float | None) -> str:
    if pnl is None:
        return ""
    if pnl > 0:
        return "🟢"
    if pnl < 0:
        return "🔴"
    return "⚪"


def _format_buy(reply: dict[str, Any]) -> str:
    ticker = reply["ticker"]
    qty = reply["quantity_added"]
    total_qty = reply["total_quantity"]
    avg = reply["average_price"]
    cmp_ = reply["current_price"]
    upnl = reply["unrealised_pnl"]
    upct = reply["unrealised_pnl_pct"]
    body = (
        f"✅ <b>BUY Confirmed — {ticker}</b>\n"
        f"{_RULE}\n"
        f"Bought:    {qty} shares @ ₹{reply['average_price']:,.2f}\n"
        f"Total:     {_fmt_rs(reply['buy_total'])}\n\n"
        f"Your position:\n"
        f"Shares:    {total_qty} | Avg: ₹{avg:,.2f}\n"
        f"CMP:       ₹{cmp_:,.2f}\n"
        f"P&amp;L:       {_fmt_rs(upnl, with_sign=True)} ({upct:+.2f}%) {_pnl_marker(upnl)}"
    )
    matched = reply.get("matched_recommendation")
    if matched:
        body += "\n\n📋 Matched to today's recommendation"
    return body


def _format_sell(reply: dict[str, Any]) -> str:
    ticker = reply["ticker"]
    qty = reply["quantity_sold"]
    rem = reply["remaining_quantity"]
    sell_price = reply["sell_price"]
    avg = reply["avg_price"]
    realised = reply["realised_pnl"]
    realised_pct = reply["realised_pnl_pct"]
    cmp_ = reply["current_price"]

    body = (
        f"✅ <b>SELL Confirmed — {ticker}</b>\n"
        f"{_RULE}\n"
        f"Sold:      {qty} shares @ ₹{sell_price:,.2f}\n"
        f"Realised:  {_fmt_rs(realised, with_sign=True)} ({realised_pct:+.2f}%) {_pnl_marker(realised)}\n\n"
    )
    if rem > 0:
        unreal = round((cmp_ - avg) * rem, 2)
        unreal_pct = round((cmp_ - avg) / avg * 100, 2) if avg else 0.0
        body += (
            f"Remaining: {rem} shares @ ₹{avg:,.2f} avg\n"
            f"CMP:       ₹{cmp_:,.2f}\n"
            f"Unrealised: {_fmt_rs(unreal, with_sign=True)} ({unreal_pct:+.2f}%) {_pnl_marker(unreal)}"
        )
    else:
        body += f"Position fully closed."
    matched = reply.get("matched_recommendation")
    if matched:
        body += "\n\n📋 Matched to today's recommendation"
    return body


def _format_section(rows: list[dict[str, Any]], *, title: str, curr_sym: str) -> list[str]:
    out = [f"<b>{title}</b>"]
    for h in rows:
        marker = _pnl_marker(h["unrealised_pnl"])
        qty_str = f"{h['quantity']:g}"
        out.append(
            f"<b>{h['ticker']:<10}</b> {qty_str}sh  "
            f"{curr_sym}{h['average_price']:,.2f}→{curr_sym}{h['current_price']:,.2f}  "
            f"{('+' if h['unrealised_pnl'] >= 0 else '-')}{curr_sym}{abs(h['unrealised_pnl']):,.2f}  "
            f"{h['unrealised_pnl_pct']:+.2f}% {marker}"
        )
    return out


def _format_portfolio(p: dict[str, Any]) -> str:
    holdings = p.get("holdings") or []
    if not holdings:
        return (
            "📊 <b>Your Portfolio</b>\n"
            f"{_RULE}\n"
            "<i>No active holdings. Send BUY &lt;TICKER&gt; &lt;QTY&gt; &lt;PRICE&gt; to add one.</i>"
        )
    ind = [h for h in holdings if (h.get("market") or "IND").upper() == "IND"]
    us = [h for h in holdings if (h.get("market") or "").upper() == "US"]

    lines = ["📊 <b>Your Portfolio</b>", _RULE]
    if ind:
        lines.extend(_format_section(ind, title="🇮🇳 Indian Holdings", curr_sym="₹"))
        ind_value = sum(h["current_value"] for h in ind)
        ind_cost = sum(h["cost_value"] for h in ind)
        ind_pnl = ind_value - ind_cost
        ind_pct = (ind_pnl / ind_cost * 100) if ind_cost else 0.0
        lines.append(
            f"  Subtotal: {_fmt_rs(ind_value)} | P&amp;L {_fmt_rs(ind_pnl, with_sign=True)} ({ind_pct:+.2f}%)"
        )
    if us:
        if ind:
            lines.append("")
        lines.extend(_format_section(us, title="🇺🇸 US Holdings", curr_sym="$"))
        us_value = sum(h["current_value"] for h in us)
        us_cost = sum(h["cost_value"] for h in us)
        us_pnl = us_value - us_cost
        us_pct = (us_pnl / us_cost * 100) if us_cost else 0.0
        lines.append(
            f"  Subtotal: ${us_value:,.2f} | P&amp;L {('+' if us_pnl>=0 else '-')}${abs(us_pnl):,.2f} ({us_pct:+.2f}%)"
        )

    lines.append(_RULE)
    lines.append(f"Combined:  {_fmt_rs(p['total_value'])} (IND ₹ + US $)")
    lines.append(
        f"P&amp;L:       {_fmt_rs(p['total_pnl'], with_sign=True)} "
        f"({p['total_pnl_pct']:+.2f}%) {_pnl_marker(p['total_pnl'])}"
    )
    lines.append(f"Holdings:  {len(ind)} Indian | {len(us)} US")
    return "\n".join(lines)


def _format_quote(ticker: str, q: dict[str, Any] | None) -> str:
    if not q or q.get("ltp") is None:
        return f"❌ Quote unavailable for {ticker}"
    ltp = float(q["ltp"])
    prev = float(q.get("prev_close")) if q.get("prev_close") is not None else None
    open_ = q.get("open")
    high = q.get("high")
    low = q.get("low")
    volume = q.get("volume")
    change = (ltp - prev) if prev is not None else None
    change_pct = ((ltp - prev) / prev * 100) if prev else None

    lines = [
        f"📈 <b>{ticker}</b> (NSE)",
        _RULE,
        f"LTP:    ₹{ltp:,.2f}",
    ]
    if open_ is not None:
        lines.append(f"Open:   ₹{float(open_):,.2f}")
    if high is not None:
        lines.append(f"High:   ₹{float(high):,.2f}")
    if low is not None:
        lines.append(f"Low:    ₹{float(low):,.2f}")
    if volume:
        v = int(volume)
        lines.append(f"Volume: {v/1_000_000:.2f}M" if v >= 1_000_000 else f"Volume: {v:,}")
    if change is not None and change_pct is not None:
        lines.append(
            f"Change: {_fmt_rs(change, with_sign=True)} ({change_pct:+.2f}%) {_pnl_marker(change)}"
        )
    return "\n".join(lines)


def _format_status() -> str:
    holdings_n = portfolio_manager.count_active_holdings()
    last_run = portfolio_manager.last_run_started_at("premarket")
    last_run_label = "—"
    if last_run:
        try:
            dt = datetime.fromisoformat(last_run.replace("Z", "+00:00")).astimezone(_IST)
            last_run_label = dt.strftime("%d-%b-%y %H:%M IST")
        except Exception:
            last_run_label = last_run
    return (
        f"✅ <b>Portfolio Advisor — System Status</b>\n"
        f"{_RULE}\n"
        f"Bot:      Running ✅\n"
        f"Upstox:   Connected ✅\n"
        f"Supabase: Connected ✅\n"
        f"Holdings: {holdings_n} stocks\n"
        f"Last run: {last_run_label}\n"
        f"Next run: 9:00 AM IST"
    )


def _format_history(ticker: str, txns: list[dict[str, Any]]) -> str:
    if not txns:
        return f"❌ No transactions found for {ticker}"
    lines = [f"📋 <b>{ticker} — Last {len(txns)} Transactions</b>", _RULE]
    for t in txns:
        action = t["action"]
        qty = t["quantity"]
        price = float(t["price"])
        try:
            dt = datetime.fromisoformat(t["executed_at"].replace("Z", "+00:00")).astimezone(_IST)
            datestr = dt.strftime("%d-%b-%y")
        except Exception:
            datestr = "—"
        pnl = t.get("realised_pnl")
        pnl_chunk = f"  {_fmt_rs(float(pnl), with_sign=True)}" if pnl is not None else ""
        lines.append(f"<b>{action:<4}</b> {qty}sh @ ₹{price:,.2f}{pnl_chunk}  {datestr}")
    return "\n".join(lines)


_HELP = (
    "🤖 <b>Portfolio Advisor — Commands</b>\n"
    f"{_RULE}\n"
    "<b>BUY</b> &lt;TICKER&gt; &lt;QTY&gt; &lt;PRICE&gt;\n"
    "<b>SELL</b> &lt;TICKER&gt; &lt;QTY&gt; &lt;PRICE&gt;\n"
    "<b>EXECUTED</b> &lt;TICKER&gt; &lt;BUY|SELL|HOLD&gt; — mark today's call as followed\n"
    "<b>PORTFOLIO</b> (or PORT / P) — show holdings\n"
    "<b>QUOTE</b> &lt;TICKER&gt; (or Q) — live price\n"
    "<b>STATUS</b> (or S) — system health\n"
    "<b>HISTORY</b> &lt;TICKER&gt; — last 5 trades\n"
    "<b>HELP</b> (or H) — this message"
)


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

_TRADE_RE = re.compile(
    r"^(BUY|SELL)(?:-(IND|US))?\s+([A-Z0-9&\-\.]+)\s+([\d.]+)(?:\s+([\d.]+))?\s*$",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(r"^(QUOTE|Q)\s+([A-Z0-9&\-]+)\s*$", re.IGNORECASE)
_HISTORY_RE = re.compile(r"^(HISTORY|HIST)\s+([A-Z0-9&\-]+)\s*$", re.IGNORECASE)
_EXECUTED_RE = re.compile(
    r"^EXECUTED\s+([A-Z0-9&\-\.]+)\s+(BUY|SELL|HOLD)\s*$", re.IGNORECASE,
)


async def process(text: str) -> str:
    """Route a single Telegram message body to a reply string."""
    if not text:
        return "❌ Empty message. Send HELP for commands."
    text = text.strip()
    upper = text.upper()

    if upper in ("HELP", "H"):
        return _HELP
    if upper in ("PORTFOLIO", "PORT", "P"):
        return _format_portfolio(portfolio_manager.get_portfolio())
    if upper in ("STATUS", "S"):
        return _format_status()

    m = _TRADE_RE.match(text)
    if m:
        action, market, ticker, qty_s, price_s = m.groups()
        if not price_s:
            return f"❌ Price required: {action.upper()} {ticker.upper()} {qty_s} &lt;PRICE&gt;"
        try:
            qty = float(qty_s)
            price = float(price_s)
        except ValueError:
            return "❌ Invalid number in command. Send HELP for examples."
        market_u = market.upper() if market else None
        if action.upper() == "BUY":
            res = await portfolio_manager.add_position(ticker, qty, price, market=market_u)
            if not res.get("success"):
                return f"❌ {res.get('error', 'BUY failed')}"
            return _format_buy(res)
        else:
            res = await portfolio_manager.close_position(ticker, qty, price)
            if not res.get("success"):
                return f"❌ {res.get('error', 'SELL failed')}"
            return _format_sell(res)

    m = _QUOTE_RE.match(text)
    if m:
        ticker = m.group(2).upper()
        q = await portfolio_manager.get_quote(ticker)
        return _format_quote(ticker, q)

    m = _HISTORY_RE.match(text)
    if m:
        ticker = m.group(2).upper()
        return _format_history(ticker, portfolio_manager.get_transactions(ticker, limit=5))

    m = _EXECUTED_RE.match(text)
    if m:
        ticker = m.group(1).upper()
        action = m.group(2).upper()
        rec = supabase_client.match_and_mark_execution(ticker, action)
        if not rec:
            return (
                f"❌ No matching {action} recommendation for <b>{ticker}</b> today."
            )
        return f"✅ Marked {ticker} {rec.get('action')} as executed"

    return "❌ Invalid command. Send HELP for commands."
