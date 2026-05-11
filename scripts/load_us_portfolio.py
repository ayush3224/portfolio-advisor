"""One-shot seeder for the 24 US holdings the user already owns via IndMoney.

Inserts a row per ticker into `holdings` with market='US' / currency='USD'.
If a row for the ticker already exists this script SKIPS — re-running is
safe and will never double-count. Use SELL or `delete from holdings` to
correct mistakes.

NOTE on avg_price values: the constants below are USD-denominated estimates
derived from IndMoney invested_inr / qty / USD_INR (which was ₹83.5 at seed
time). For any *future* reload that wants to recompute from invested INR,
import config.USD_INR_RATE (live yfinance INR=X) and divide invested_inr by
qty × config.USD_INR_RATE — not by the historical 83.5.
"""

from __future__ import annotations

import sys
from typing import Iterable

sys.path.insert(0, "/root/portfolio-advisor")

from storage import supabase_client  # noqa: E402

# (ticker, quantity, avg_price_usd)
US_HOLDINGS: list[tuple[str, float, float]] = [
    ("RTX",   0.97, 194.50),
    ("SLV",   2.96,  28.62),
    ("GLD",   1.35, 174.44),
    ("AAAU", 10.00,  18.69),
    ("GOOGL", 1.77, 103.73),
    ("BKR",   7.80,  18.10),
    ("DUK",   1.94,  48.65),
    ("PLTR",  1.28,  73.50),
    ("AMZN",  0.35, 121.71),
    ("PANW",  0.64,  73.57),
    ("GOOG",  3.64,  65.18),
    ("IBKR", 10.50,  22.45),
    ("META",  0.77, 272.55),
    ("SOXX",  1.03, 104.44),
    ("EQIX",  0.24, 397.47),
    ("BRK.B", 0.39, 215.63),
    ("NVDA", 12.07,  68.49),
    ("NFLX",  4.44, 531.25),
    ("TSLA",  0.55, 144.85),
    ("XOM",   6.04,  53.13),
    ("AAPL",  2.61,  82.22),
    ("IAU",   5.28,  35.64),
    ("TSM",   0.91, 103.53),
    ("PSI",   1.55,  27.81),
]


def _existing_us_tickers(client) -> set[str]:
    res = client.table("holdings").select("ticker,market").eq("market", "US").execute()
    return {row["ticker"] for row in (res.data or [])}


def main() -> int:
    client = supabase_client.get_client()
    if client is None:
        print("Supabase client unavailable.")
        return 1
    skip = _existing_us_tickers(client)
    inserted = 0
    skipped = 0
    failed: list[str] = []
    for ticker, qty, avg in US_HOLDINGS:
        if ticker in skip:
            skipped += 1
            continue
        payload = {
            "ticker": ticker,
            "quantity": qty,
            "average_price": round(avg, 4),
            "market": "US",
            "currency": "USD",
            "exchange": "US",
            "is_active": True,
            "notes": "seeded from IndMoney 2026-05-11",
        }
        try:
            client.table("holdings").insert(payload).execute()
            inserted += 1
        except Exception as exc:
            failed.append(f"{ticker}: {exc}")
    print(f"Inserted: {inserted}")
    print(f"Skipped (already present): {skipped}")
    if failed:
        print(f"Failed ({len(failed)}):")
        for f in failed:
            print(" -", f)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
