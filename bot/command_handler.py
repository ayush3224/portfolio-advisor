"""Parse and dispatch Telegram commands to the portfolio manager.

All commands are case-insensitive with flexible spacing. The router is async
because BUY/SELL/QUOTE depend on async live-price fetches.

Output strings use HTML formatting (matches the bot_runner's parse_mode=HTML).
"""

from __future__ import annotations

import html
import logging
import math
import re
from datetime import datetime
from typing import Any

import pytz

import config
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


def _sym(market: str | None) -> str:
    """Currency symbol for a market — US prices are USD, everything else INR."""
    return "$" if (market or "IND").upper() == "US" else "₹"


def _fmt_ccy(amount: float | None, market: str | None, *, with_sign: bool = False) -> str:
    """Money in the market's own currency. US holdings are priced in dollars;
    rendering them with ₹ is what made an INR-priced US BUY look plausible."""
    if amount is None:
        return "—"
    sign = "+" if with_sign and amount > 0 else ("-" if with_sign and amount < 0 else "")
    return f"{sign}{_sym(market)}{abs(amount):,.2f}"


def _pnl_marker(pnl: float | None) -> str:
    if pnl is None:
        return ""
    if pnl > 0:
        return "🟢"
    if pnl < 0:
        return "🔴"
    return "⚪"


# Symbols that look like tickers but aren't tradable: fund-family brand names
# (SPDR), index names (NIFTY, SENSEX), and asset classes (GOLD, BITCOIN). Each
# maps to what the user most likely meant. A BUY on one of these is refused
# before any database write — SPDR in particular quotes as NaN forever, so
# accepting it books a position whose CMP never updates.
REJECTED_TICKERS = {
    "SPDR": "GLD (SPDR Gold Trust) or SPY (S&P 500)",
    "NIFTY": "NIFTYBEES or ICICINIFTY",
    "SENSEX": "SETFNIF50 or NIFTYBEES",
    "BITCOIN": "Not supported — equity only",
    "GOLD": "GLD (US) or GOLDBEES (India)",
}


def _format_rejected_ticker(ticker: str) -> str:
    suggestion = REJECTED_TICKERS[ticker]
    return (
        f"❌ {ticker} is not a valid ticker symbol.\n"
        f"Did you mean: {html.escape(suggestion)}?\n\n"
        f"Resend with the correct ticker."
    )


def _format_buy(reply: dict[str, Any]) -> str:
    ticker = reply["ticker"]
    qty = reply["quantity_added"]
    total_qty = reply["total_quantity"]
    avg = reply["average_price"]
    cmp_ = reply["current_price"]
    upnl = reply["unrealised_pnl"]
    upct = reply["unrealised_pnl_pct"]
    market = reply.get("market") or "IND"
    sym = _sym(market)
    body = (
        f"✅ <b>BUY Confirmed — {ticker}</b>\n"
        f"{_RULE}\n"
        f"Bought:    {qty} shares @ {sym}{reply.get('buy_price', reply['average_price']):,.2f}\n"
        f"Total:     {_fmt_ccy(reply['buy_total'], market)}\n\n"
        f"Your position:\n"
        f"Shares:    {total_qty} | Avg: {sym}{avg:,.2f}\n"
        f"CMP:       {sym}{cmp_:,.2f}\n"
        f"P&amp;L:       {_fmt_ccy(upnl, market, with_sign=True)} ({upct:+.2f}%) {_pnl_marker(upnl)}"
    )
    match_message = reply.get("match_message")
    if match_message:
        body += f"\n\n{match_message}"
    if market.upper() == "US":
        body += f"\n\n{portfolio_manager.CURRENCY_HINT}"
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
    market = reply.get("market") or "IND"
    sym = _sym(market)

    body = (
        f"✅ <b>SELL Confirmed — {ticker}</b>\n"
        f"{_RULE}\n"
        f"Sold:      {qty} shares @ {sym}{sell_price:,.2f}\n"
        f"Realised:  {_fmt_ccy(realised, market, with_sign=True)} ({realised_pct:+.2f}%) {_pnl_marker(realised)}\n\n"
    )
    if rem > 0:
        unreal = round((cmp_ - avg) * rem, 2)
        unreal_pct = round((cmp_ - avg) / avg * 100, 2) if avg else 0.0
        body += (
            f"Remaining: {rem} shares @ {sym}{avg:,.2f} avg\n"
            f"CMP:       {sym}{cmp_:,.2f}\n"
            f"Unrealised: {_fmt_ccy(unreal, market, with_sign=True)} ({unreal_pct:+.2f}%) {_pnl_marker(unreal)}"
        )
    else:
        body += f"Position fully closed."
    match_message = reply.get("match_message")
    if match_message:
        body += f"\n\n{match_message}"
    return body


def _signed_rs0(amount: float) -> str:
    """Signed rupees, no paise — sign outside the symbol (+₹1,234), matching
    _fmt_rs and the rest of the bot's money formatting."""
    return f"{'+' if amount >= 0 else '-'}₹{abs(amount):,.0f}"


def _signed_usd(amount: float) -> str:
    return f"{'+' if amount >= 0 else '-'}${abs(amount):,.2f}"


# Telegram hard-caps a message at 4096 chars; stay clear of the edge.
_MAX_MSG_CHARS = 4000


def _format_section(rows: list[dict[str, Any]], *, curr_sym: str) -> list[str]:
    """One line per holding: TICKER  Xsh  avg→cmp  P&L% emoji, priced in the
    market's own currency."""
    out: list[str] = []
    for h in rows:
        marker = _pnl_marker(h["unrealised_pnl"])
        qty_str = f"{h['quantity']:g}"
        out.append(
            f"<b>{h['ticker']:<10}</b> {qty_str}sh  "
            f"{curr_sym}{h['average_price']:,.2f}→{curr_sym}{h['current_price']:,.2f}  "
            f"{h['unrealised_pnl_pct']:+.2f}% {marker}"
        )
    return out


def _format_portfolio(p: dict[str, Any]) -> list[str]:
    """PORTFOLIO reply, as one message or two.

    IND and US are reported separately in their own currency; the combined
    total is INR only, with the US leg converted at the live USD/INR rate.
    The old reply added a dollar subtotal onto a rupee one and labelled the
    result "₹", understating the portfolio by roughly the FX rate.
    """
    holdings = p.get("holdings") or []
    if not holdings:
        return [
            "📊 <b>Your Portfolio</b>\n"
            f"{_RULE}\n"
            "<i>No active holdings. Send BUY &lt;TICKER&gt; &lt;QTY&gt; &lt;PRICE&gt; to add one.</i>"
        ]

    ind = [h for h in holdings if (h.get("market") or "IND").upper() == "IND"]
    us = [h for h in holdings if (h.get("market") or "").upper() == "US"]
    rate = float(p.get("usd_inr_rate") or config.USD_INR_RATE)

    ind_block: list[str] = []
    if ind:
        ind_block.append(f"🇮🇳 <b>INDIAN</b> ({len(ind)} stocks)")
        ind_block.extend(_format_section(ind, curr_sym="₹"))
        ind_block.append(
            f"Subtotal: ₹{p['ind_value_inr']:,.0f} | "
            f"P&amp;L: {_signed_rs0(p['ind_pnl_inr'])} ({p['ind_pnl_pct']:+.1f}%) "
            f"{_pnl_marker(p['ind_pnl_inr'])}"
        )

    us_block: list[str] = []
    if us:
        us_block.append(f"🌐 <b>US</b> ({len(us)} stocks)")
        us_block.extend(_format_section(us, curr_sym="$"))
        us_block.append(
            f"Subtotal: ${p['us_value_usd']:,.2f} (₹{p['us_value_inr']:,.0f})"
        )
        us_block.append(
            f"P&amp;L: {_signed_usd(p['us_pnl_usd'])} ({p['us_pnl_pct']:+.1f}%) "
            f"{_pnl_marker(p['us_pnl_usd'])}"
        )

    total_block = [
        _RULE,
        "💼 <b>TOTAL PORTFOLIO</b>",
        f"Value: ₹{p['total_value_inr']:,.0f}",
    ]
    if ind:
        total_block.append(f"  IND: ₹{p['ind_value_inr']:,.0f}")
    if us:
        total_block.append(f"  US:  ${p['us_value_usd']:,.2f} @ ₹{rate:,.2f}")
    total_block.append(
        f"P&amp;L:  {_signed_rs0(p['total_pnl_inr'])} ({p['total_pnl_pct']:+.1f}%) "
        f"{_pnl_marker(p['total_pnl_inr'])}"
    )
    total_block.append(_RULE)
    total_block.append(
        f"Holdings: {len(ind)} IND + {len(us)} US = {len(ind) + len(us)}"
    )

    header = ["📊 <b>Your Portfolio</b>", _RULE]
    single = header + ind_block + ([""] + us_block if (ind and us) else us_block) + total_block
    joined = "\n".join(single)
    if len(joined) <= _MAX_MSG_CHARS:
        return [joined]

    # Too long for one Telegram message — break on the IND/US seam so each
    # part is still a self-contained, readable statement.
    first = "\n".join(header + ind_block)
    second = "\n".join(
        ["📊 <b>Your Portfolio</b> (continued)", _RULE] + us_block + total_block
    )
    return [first, second]


def _format_quote(ticker: str, q: dict[str, Any] | None) -> str:
    if not q or q.get("ltp") is None:
        return f"❌ Quote unavailable for {ticker}"
    ltp = float(q["ltp"])
    if math.isnan(ltp):
        return f"❌ Quote unavailable for {ticker}"
    prev = float(q.get("prev_close")) if q.get("prev_close") is not None else None
    open_ = q.get("open")
    high = q.get("high")
    low = q.get("low")
    volume = q.get("volume")
    change = (ltp - prev) if prev is not None else None
    change_pct = ((ltp - prev) / prev * 100) if prev else None

    market = (q.get("market") or "IND").upper()
    sym = _sym(market)
    venue = "US" if market == "US" else "NSE"

    lines = [
        f"📈 <b>{ticker}</b> ({venue})",
        _RULE,
        f"LTP:    {sym}{ltp:,.2f}",
    ]
    if open_ is not None:
        lines.append(f"Open:   {sym}{float(open_):,.2f}")
    if high is not None:
        lines.append(f"High:   {sym}{float(high):,.2f}")
    if low is not None:
        lines.append(f"Low:    {sym}{float(low):,.2f}")
    if volume:
        v = int(volume)
        lines.append(f"Volume: {v/1_000_000:.2f}M" if v >= 1_000_000 else f"Volume: {v:,}")
    if change is not None and change_pct is not None:
        lines.append(
            f"Change: {_fmt_ccy(change, market, with_sign=True)} ({change_pct:+.2f}%) {_pnl_marker(change)}"
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
    "<b>EXECUTED</b> &lt;TICKER&gt; &lt;BUY|SELL|EXIT|HOLD&gt; — mark today's call as followed\n"
    "<b>PORTFOLIO</b> (or PORT / P) — show holdings\n"
    "<b>QUOTE</b> &lt;TICKER&gt; (or Q) — live price\n"
    "<b>STATUS</b> (or S) — system health\n"
    "<b>HISTORY</b> &lt;TICKER&gt; — last 5 trades\n"
    "<b>HELP</b> (or H) — this message\n"
    f"{_RULE}\n"
    f"{portfolio_manager.CURRENCY_HINT}"
)


# What the user types → the recommendation family it resolves to. EXIT is an
# alias for SELL: you exit a position, the advisor calls it EXIT-PARTIAL, and
# either word should find it. The families themselves live in
# supabase_client (_SELL_ACTIONS already spans EXIT-FULL, EXIT-PARTIAL,
# TIGHTEN-SL, BOOK-PROFIT, SELL and the legacy PARTIAL-EXIT / FULL-EXIT).
_EXECUTED_ALIASES = {
    "BUY": "BUY",
    "SELL": "SELL",
    "EXIT": "SELL",
    "HOLD": "HOLD",
}


def handle_executed(ticker: str, action_word: str) -> str:
    """EXECUTED <TICKER> <BUY|SELL|EXIT|HOLD> — mark today's call as followed.

    The bot already auto-matches trades entered through BUY/SELL; this covers
    trades placed directly in the broker app, which is the common case.
    """
    ticker = ticker.upper().strip()
    action_word = action_word.upper().strip()
    family = _EXECUTED_ALIASES.get(action_word, action_word)
    rec = supabase_client.match_and_mark_execution(ticker, family)
    if not rec:
        return (
            f"❌ No {action_word} recommendation found for <b>{ticker}</b> today"
        )
    conf = rec.get("confidence_score")
    conf_str = f" (conf {conf:g}/10)" if conf is not None else ""
    return (
        f"✅ Marked as executed\n"
        f"{ticker} {rec.get('action')}{conf_str} noted as followed."
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
    r"^EXECUTED\s+([A-Z0-9&\-\.]+)\s+(BUY|SELL|EXIT|HOLD)\s*$", re.IGNORECASE,
)


async def process(text: str) -> list[str]:
    """Route a single Telegram message body to one or more reply messages.

    Almost every command yields a single message; PORTFOLIO can split into two
    when the holdings list would exceed Telegram's per-message limit."""
    if not text:
        return ["❌ Empty message. Send HELP for commands."]
    text = text.strip()
    upper = text.upper()

    if upper in ("HELP", "H"):
        return [_HELP]
    if upper in ("PORTFOLIO", "PORT", "P"):
        return _format_portfolio(portfolio_manager.get_portfolio())
    if upper in ("STATUS", "S"):
        return [_format_status()]

    m = _TRADE_RE.match(text)
    if m:
        action, market, ticker, qty_s, price_s = m.groups()
        if not price_s:
            return [f"❌ Price required: {action.upper()} {ticker.upper()} {qty_s} &lt;PRICE&gt;"]
        try:
            qty = float(qty_s)
            price = float(price_s)
        except ValueError:
            return ["❌ Invalid number in command. Send HELP for examples."]
        market_u = market.upper() if market else None
        if action.upper() == "BUY":
            # Refused before add_position runs — no holdings or transactions
            # row is created. SELL is deliberately exempt so an already-booked
            # bad position can still be closed out.
            if ticker.upper() in REJECTED_TICKERS:
                return [_format_rejected_ticker(ticker.upper())]
            res = await portfolio_manager.add_position(ticker, qty, price, market=market_u)
            if not res.get("success"):
                # Validation failures (e.g. an INR price on a US stock) carry a
                # ready-made explanation; anything else is a bare error string.
                if res.get("reply"):
                    return [res["reply"]]
                return [f"❌ {res.get('error', 'BUY failed')}"]
            return [_format_buy(res)]
        else:
            res = await portfolio_manager.close_position(ticker, qty, price)
            if not res.get("success"):
                if res.get("reply"):
                    return [res["reply"]]
                return [f"❌ {res.get('error', 'SELL failed')}"]
            return [_format_sell(res)]

    m = _QUOTE_RE.match(text)
    if m:
        ticker = m.group(2).upper()
        q = await portfolio_manager.get_quote(ticker)
        return [_format_quote(ticker, q)]

    m = _HISTORY_RE.match(text)
    if m:
        ticker = m.group(2).upper()
        return [_format_history(ticker, portfolio_manager.get_transactions(ticker, limit=5))]

    m = _EXECUTED_RE.match(text)
    if m:
        ticker = m.group(1).upper()
        action = m.group(2).upper()
        return [handle_executed(ticker, action)]

    return ["❌ Invalid command. Send HELP for commands."]
