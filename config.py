"""Central config — loads env, defines capital model.

Trading product: CNC (delivery / swing). No MIS, no intraday leverage. Every
trade is sized 1x against capital_deployed only — `leverage_multiplier` is
retained as a column for schema continuity but is always 1."""

import logging
import os

from dotenv import load_dotenv

load_dotenv("/root/portfolio-advisor/.env")

log = logging.getLogger(__name__)


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        log.warning("Missing required env var: %s — dependent features will be disabled", name)
    return value


# --- Credentials ---
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY", required=True)
UPSTOX_ANALYTICS_TOKEN = _get("UPSTOX_ANALYTICS_TOKEN", required=True)
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", required=True)
TAVILY_API_KEY = _get("TAVILY_API_KEY", required=True)
TELETHON_API_ID = _get("TELETHON_API_ID")
TELETHON_API_HASH = _get("TELETHON_API_HASH")
TELETHON_PHONE = _get("TELETHON_PHONE")
SUPABASE_URL = _get("SUPABASE_URL", required=True)
SUPABASE_KEY = _get("SUPABASE_KEY", required=True)


# --- Capital model ---
DAILY_CAPITAL_BUDGET = int(_get("DAILY_CAPITAL_BUDGET", "10000"))
DAILY_LOSS_LIMIT = int(_get("DAILY_LOSS_LIMIT", "2000"))
MAX_POSITIONS_PER_DAY = 3


# --- Models ---
SONNET_MODEL = "claude-sonnet-4-5"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


# --- Capital map: confidence → capital_fraction (1x CNC, no leverage) ---
# None => skip (do not recommend). Notional == capital_deployed everywhere.
CAPITAL_FRACTION_MAP: dict[int, float | None] = {
    10: 0.40,
    9:  0.40,
    8:  0.30,
    7:  0.30,
    6:  0.20,
    5:  None,
    4:  None,
    3:  None,
    2:  None,
    1:  None,
    0:  None,
}


def sizing_for_confidence(confidence: int) -> tuple[int, float] | None:
    """Return (leverage, capital_fraction) for a confidence score, or None to skip.

    Tuple shape preserved for existing callers; leverage is always 1 (CNC)."""
    frac = CAPITAL_FRACTION_MAP.get(int(confidence))
    return (1, frac) if frac is not None else None


# --- Risk guardrails (CNC / swing — no leverage rules) ---
MAX_SINGLE_POSITION_PCT = 0.20
MAX_SECTOR_CONCENTRATION_PCT = 0.30
MARKET_OPEN_IST = "09:15"
MARKET_CLOSE_IST = "15:30"


# --- Cache TTLs (seconds) ---
NEWS_CACHE_TTL = 3600       # 1 hour
PORTFOLIO_CACHE_TTL = 300   # 5 minutes


# --- FX ---
# USD_INR_RATE is fetched once per process from yfinance INR=X, falling back
# to 95.31 on failure. USD_TO_INR is kept as a backwards-compat alias for
# callers that still import the old name.
try:
    from ingestion.market_context import fetch_usd_inr_rate as _fetch_usd_inr
    USD_INR_RATE: float = _fetch_usd_inr()
except Exception as _exc:  # pragma: no cover — import-time failures
    log.warning("USD/INR fetch failed at config import: %s", _exc)
    USD_INR_RATE = 95.31

log.info("USD/INR rate: %s", USD_INR_RATE)
USD_TO_INR = USD_INR_RATE  # legacy alias


# --- Project identity (used to scope run_log queries away from sibling apps) ---
PROJECT_NAME = "portfolio-advisor"
PROJECT_RUN_TYPES = ("premarket", "midday", "eod", "weekly_scorecard")


# --- Telegram channels (handle → weight) ---
TELEGRAM_CHANNELS: dict[str, int] = {
    "religarebrokingofficial": 3,
    "ICICIdirectMARKETSstocks": 3,
    "Equity99Official_Equity_999": 3,
    "equity99": 3,
    "nooreshtech": 3,
    "STOCKGAINERSS": 2,
    "aakankshatrading": 1,
    "rawattraderss": 1,
    "STOCK_MARKET_SEBI_R": 1,
}


# --- Telethon session ---
# Optional persistent string-session (preferred for headless VPS cron).
# Generate once via: python scripts/export_telethon_session.py
TELETHON_SESSION_STRING = _get("TELETHON_SESSION_STRING")
# File-based fallback — portfolio-advisor session file.
TELETHON_SESSION_FILE = _get("TELETHON_SESSION_FILE", "/root/portfolio-advisor/portfolio_advisor_session")


# --- NSE instrument key map (NSE_EQ|<ISIN>) ---
# Source: Upstox instrument master + NSE ISIN registry. Verify against the
# Upstox instrument file (https://api.upstox.com/v2/option/contract) before
# treating as authoritative — corporate actions can re-issue ISINs.
INSTRUMENT_KEYS: dict[str, str] = {
    "RELIANCE":   "NSE_EQ|INE002A01018",
    "TCS":        "NSE_EQ|INE467B01029",
    "HDFCBANK":   "NSE_EQ|INE040A01034",
    "ICICIBANK":  "NSE_EQ|INE090A01021",
    "INFY":       "NSE_EQ|INE009A01021",
    "SBIN":       "NSE_EQ|INE062A01020",
    "AXISBANK":   "NSE_EQ|INE238A01034",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "WIPRO":      "NSE_EQ|INE075A01022",
    "MARUTI":     "NSE_EQ|INE585B01010",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "ADANIENT":   "NSE_EQ|INE423A01024",
    "SUNPHARMA":  "NSE_EQ|INE044A01036",
    "LTIM":       "NSE_EQ|INE214T01019",
    "TITAN":      "NSE_EQ|INE280A01028",
    "ONGC":       "NSE_EQ|INE213A01029",
    "NTPC":       "NSE_EQ|INE733E01010",
    "COALINDIA":  "NSE_EQ|INE522F01014",
    "TATASTEEL":  "NSE_EQ|INE081A01020",
    "BPCL":       "NSE_EQ|INE029A01011",
    "HINDALCO":   "NSE_EQ|INE038A01020",
    "TECHM":      "NSE_EQ|INE669C01036",
    "DIVISLAB":   "NSE_EQ|INE361B01024",
    "PIIND":      "NSE_EQ|INE603J01030",
    "TRENT":      "NSE_EQ|INE849A01020",
    "ETERNAL":    "NSE_EQ|INE758T01015",   # formerly Zomato
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "KOTAKBANK":  "NSE_EQ|INE237A01028",
    "INDUSINDBK": "NSE_EQ|INE095A01012",
    "HDFCLIFE":   "NSE_EQ|INE795G01014",
    "BAJAJFINSV": "NSE_EQ|INE918I01026",
    "M&M":        "NSE_EQ|INE101A01026",
    "GRASIM":     "NSE_EQ|INE047A01021",
    "TATAPOWER":  "NSE_EQ|INE245A01021",
    "NESTLEIND":  "NSE_EQ|INE239A01024",
    "ULTRACEMCO": "NSE_EQ|INE481G01011",
    "ADANIPORTS": "NSE_EQ|INE742F01042",
}


def instrument_key_for(ticker: str | None) -> str | None:
    """Return Upstox instrument_key for a ticker, or None if unmapped."""
    if not ticker:
        return None
    return INSTRUMENT_KEYS.get(ticker.upper())


# --- yfinance symbol overrides for NSE tickers ---
# yfinance does not always carry the same ticker symbol that NSE / our internal
# code uses. When the default `<TICKER>.NS` returns no data we route through
# these overrides. Tested 2026-05-13 — any tickers not in this map use
# `<TICKER>.NS` directly (which works for the vast majority of NSE stocks).
#
# ICICINIFTY → NIFTYIETF.NS: same issuer (ICICI Prudential Nifty 50 ETF). NAV
# tracks the same underlying so it is a faithful proxy.
YFINANCE_IND_SYMBOL_MAP: dict[str, str] = {
    "ICICIGOLD":     "GOLDIETF.NS",   # ICICI Pru Gold ETF — NSE symbol is GOLDIETF
    "ICICINIFTY":    "NIFTYIETF.NS",  # ICICI Pru Nifty 50 ETF — NSE symbol is NIFTYIETF
    "MANKINDPHARMA": "MANKIND.NS",    # Mankind Pharma — NSE symbol is MANKIND
}


def yf_ind_symbol(ticker: str | None) -> str | None:
    """yfinance symbol for an NSE-listed ticker. Returns the override if any,
    otherwise `<TICKER>.NS`. Returns None for empty input."""
    if not ticker:
        return None
    t = ticker.upper().strip()
    return YFINANCE_IND_SYMBOL_MAP.get(t, f"{t}.NS")


# --- Known macro / event calendar ---
# severity drives risk_guardrails.check_event_tomorrow:
#   high   → halve all position sizes  (capital_deployed × 0.5)
#   medium → (no-op since CNC is 1x; surfaced as informational warning only)
# Add events as they're announced; entries with date < today are harmless.
KNOWN_EVENTS: list[dict[str, str]] = [
    {"date": "2026-05-07", "event": "RBI Monetary Policy",   "severity": "high"},
    {"date": "2026-06-06", "event": "RBI Monetary Policy",   "severity": "high"},
    {"date": "2026-08-06", "event": "RBI Monetary Policy",   "severity": "high"},
    {"date": "2026-02-01", "event": "Union Budget",          "severity": "high"},
    {"date": "2026-12-31", "event": "F&O expiry (year-end)", "severity": "medium"},
]


# --- Operational flags ---
DRY_RUN = _get("DRY_RUN", "false").lower() == "true"
USE_MOCK_PORTFOLIO = _get("USE_MOCK_PORTFOLIO", "false").lower() == "true"
# PAPER_TRADING=true loads a mock portfolio, banners every Telegram message,
# and tags every persisted recommendation as paper. Use this for Monday rehearsals
# and any pre-Upstox-token validation.
PAPER_TRADING = _get("PAPER_TRADING", "false").lower() == "true"
