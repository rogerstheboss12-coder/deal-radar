"""The fee wall for second-by-second trading, measured on real Kraken trades.

For each coin: rebuild a 1-second price series from every trade, then for holding times of 1s, 10s,
1 min and 5 min, measure how big price moves usually are and how often they exceed the round-trip cost.
"Perfect foresight" is the upper bound for ANY strategy at that speed: trade only the moves that beat the
cost, always in the right direction, never overlapping. Bid/ask spread is not charged, so real results
would be worse still.
"""

from __future__ import annotations

import logging
import time

import ccxt
import numpy as np
import pandas as pd

log = logging.getLogger("cryptobot")

HOLDS = {"1s": 1, "10s": 10, "1min": 60, "5min": 300}
ROUND_TRIP = {"maker 0.25%x2": 0.005, "taker 0.40%x2": 0.008}


def pick_pairs(ex: ccxt.Exchange, n_small: int) -> list[str]:
    """BTC for reference plus small coins: USD pairs ranked by 24h dollar volume, from the quiet end up,
    skipping ones too dead to trade (under $50k a day)."""
    tickers = ex.fetch_tickers()
    vols = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if not m or m["quote"] != "USD" or not m.get("spot", True) or m["base"] in {"USDT", "USDC", "DAI", "EUR", "GBP"}:
            continue
        usd = t.get("quoteVolume") or (t.get("baseVolume") or 0) * (t.get("last") or 0)
        if usd and usd >= 50_000:
            vols.append((usd, sym))
    vols.sort()
    small = [s for _, s in vols[: n_small * 3 : 3]]  # spread the picks across the low-volume end
    return ["BTC/USD"] + small


def fetch_trades(ex: ccxt.Exchange, sym: str, hours: float) -> pd.Series:
    since = ex.milliseconds() - int(hours * 3600 * 1000)
    rows, cursor = [], since
    while True:
        batch = ex.fetch_trades(sym, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend((t["timestamp"], t["price"]) for t in batch)
        if batch[-1]["timestamp"] <= cursor or len(batch) < 2:
            break
        cursor = batch[-1]["timestamp"] + 1
        if cursor >= ex.milliseconds() - 1000:
            break
        time.sleep(ex.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=["ts", "price"]).drop_duplicates()
    s = pd.Series(df["price"].to_numpy(), index=pd.to_datetime(df["ts"], unit="ms", utc=True)).sort_index()
    return s.resample("1s").last().ffill()  # price at every second = last trade so far


def measure(prices: pd.Series) -> dict:
    out = {"seconds": len(prices), "hours": round(len(prices) / 3600, 1)}
    logp = np.log(prices.to_numpy())
    for name, k in HOLDS.items():
        moves = np.abs(logp[k::k] - logp[:-k:k])  # non-overlapping windows
        row = {"median_move_pct": round(float(np.median(moves)) * 100, 4),
               "p99_move_pct": round(float(np.percentile(moves, 99)) * 100, 3)}
        for cname, cost in ROUND_TRIP.items():
            wins = moves[moves > cost] - cost
            row[f"beats_{cname}_pct"] = round(float((moves > cost).mean()) * 100, 3)
            # Perfect foresight, compounded, scaled to one day.
            per_day = np.exp(np.log1p(wins).sum() * 86400 / max(len(prices), 1))
            row[f"perfect_$100_per_day_{cname}"] = round(100 * float(per_day), 2)
        out[name] = row
    return out


def run(n_small: int = 10, hours_small: float = 72, hours_btc: float = 6) -> dict:
    ex = ccxt.kraken({"enableRateLimit": True})
    ex.load_markets()
    result = {}
    for sym in pick_pairs(ex, n_small):
        try:
            prices = fetch_trades(ex, sym, hours_btc if sym == "BTC/USD" else hours_small)
        except Exception as exc:
            log.warning("skip %s: %s", sym, exc)
            continue
        if len(prices) > 600:
            result[sym] = measure(prices)
            log.info("%s: %s seconds of prices", sym, f"{len(prices):,}")
    return result


def table(result: dict) -> str:
    lines = []
    for sym, r in result.items():
        for hold in HOLDS:
            h = r[hold]
            lines.append({"coin": sym.split("/")[0], "hold": hold, "typical move %": h["median_move_pct"],
                          "1-in-100 move %": h["p99_move_pct"],
                          "beats 0.5% cost (% of windows)": h["beats_maker 0.25%x2_pct"],
                          "beats 0.8% cost": h["beats_taker 0.40%x2_pct"],
                          "PERFECT foresight $100/day, maker": h["perfect_$100_per_day_maker 0.25%x2"],
                          "taker": h["perfect_$100_per_day_taker 0.40%x2"]})
    return pd.DataFrame(lines).to_string(index=False)
