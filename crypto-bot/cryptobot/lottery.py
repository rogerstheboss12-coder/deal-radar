"""The "lottery" test: all-in, leveraged strategies started on many dates, to see how often $100
really turns into $1,000, $10,000 or $100,000, and how often it goes to zero.

Each run trades one coin with the whole account as margin (perpetual-futures style): the position is
equity x leverage. A candle whose low (for a long) or high (for a short) wipes out the margin is a
liquidation, and that run is over at $0. Fees and funding are charged on the full leveraged size.
A run counts as reaching a target if equity *ever* touched it, as if you cashed out at the perfect moment.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Settings
from .strategy import target_weights

START = 100.0
TARGETS = {"10x ($1k)": 1_000.0, "100x ($10k)": 10_000.0, "1000x ($100k)": 100_000.0}
LEVERAGES = (1, 3, 10, 25, 100)
HORIZONS = {"1 month": 30, "1 year": 365}
FEE = 0.0005          # per side, on the leveraged size (typical perpetual-futures taker fee)
FUNDING = 0.0003      # per day, on the leveraged size (0.01% every 8 hours)
MAINTENANCE = 0.005   # liquidated when equity falls to 0.5% of position size


def signals(df: pd.DataFrame, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Position after each candle closes: +1 long, -1 short, 0 flat."""
    close = df["close"]
    hi20, lo20 = close.shift(1).rolling(20).max(), close.shift(1).rolling(20).min()
    hi10, lo10 = close.shift(1).rolling(10).max(), close.shift(1).rolling(10).min()
    breakout = np.zeros(len(df))
    pos = 0
    for i, (c, h20, l20, h10, l10) in enumerate(zip(close, hi20, lo20, hi10, lo10)):
        if np.isnan(h20):
            continue
        if pos == 1 and c < l10 or pos == -1 and c > h10:
            pos = 0
        if pos == 0:
            pos = 1 if c > h20 else -1 if c < l20 else 0
        breakout[i] = pos
    return {
        "always long": np.ones(len(df)),
        "breakout long/short": breakout,
        "trend (steady bot's signal)": (target_weights(df, Settings()).to_numpy() > 0).astype(float),
        "coin flip": rng.choice([-1.0, 1.0], size=len(df)),
    }


def simulate(o, h, l, c, sig, start: int, end: int, lev: float) -> tuple[float, float]:
    """One run. Returns (final equity, highest equity seen)."""
    eq, peak = START, START
    pos, entry, size = 0.0, 0.0, 0.0
    for t in range(start, end):
        want = sig[t - 1]
        if want != pos:
            if pos:
                eq += size * pos * (o[t] / entry - 1) - size * FEE
                if eq <= 0:  # gapped through the liquidation price overnight
                    return 0.0, peak
            pos = want
            if pos:
                entry, size = o[t], eq * lev
                eq -= size * FEE
        if pos:
            worst = l[t] if pos > 0 else h[t]
            if eq + size * pos * (worst / entry - 1) <= MAINTENANCE * size:
                return 0.0, peak
            eq -= size * FUNDING
            best = h[t] if pos > 0 else l[t]
            peak = max(peak, eq + size * pos * (best / entry - 1))
            if eq <= 0:
                return 0.0, peak
        else:
            peak = max(peak, eq)
    if pos:
        eq += size * pos * (c[end - 1] / entry - 1) - size * FEE
    return max(eq, 0.0), peak


def run(history: dict[str, pd.DataFrame], step_days: int = 7, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    rows = []
    for coin, df in history.items():
        o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
        warm = Settings().warmup
        for strategy, sig in signals(df, rng).items():
            for horizon, days in HORIZONS.items():
                for start in range(warm, len(df) - days, step_days):
                    for lev in LEVERAGES:
                        final, peak = simulate(o, h, l, c, sig, start, start + days, lev)
                        rows.append((strategy, horizon, lev, coin, final, peak))
    runs = pd.DataFrame(rows, columns=["strategy", "horizon", "leverage", "coin", "final", "peak"])

    summary = []
    for (strategy, horizon, lev), g in runs.groupby(["strategy", "horizon", "leverage"], sort=False):
        row = {"strategy": strategy, "horizon": horizon, "leverage": f"{lev}x", "runs": len(g),
               "wiped_out_pct": pct(g["final"] < 1), "lost_money_pct": pct(g["final"] < START),
               "median_final": round(g["final"].median(), 2), "best_peak": round(g["peak"].max(), 2)}
        for name, target in TARGETS.items():
            row[f"touched_{name}_pct"] = pct(g["peak"] >= target)
        summary.append(row)
    return {"total_runs": len(runs), "coins": list(history), "summary": summary,
            "one_day_10x": one_day_10x(history)}


def one_day_10x(history: dict[str, pd.DataFrame]) -> dict:
    """Days on which even a perfect guess of direction turned $100 into $1,000 at 100x in one day:
    the coin had to move 9%+ the right way without ever going 1% the wrong way first."""
    out = {}
    for coin, df in history.items():
        up = (df["close"] / df["open"] - 1 >= 0.09) & (df["low"] / df["open"] - 1 > -0.01)
        down = (df["close"] / df["open"] - 1 <= -0.09) & (df["high"] / df["open"] - 1 < 0.01)
        out[coin] = {"days": len(df), "winning_days": int((up | down).sum()), "pct": pct(up | down)}
    return out


def pct(mask) -> float:
    return round(float(np.mean(mask)) * 100, 2)


def table(result: dict) -> str:
    """Plain-text table for the logs."""
    cols = ["strategy", "horizon", "leverage", "runs", "wiped_out_pct", "lost_money_pct", "median_final"] + [
        f"touched_{n}_pct" for n in TARGETS] + ["best_peak"]
    df = pd.DataFrame(result["summary"])[cols]
    df.columns = ["strategy", "horizon", "lev", "runs", "wiped %", "lost %", "median $"] + [
        f"hit {n.split()[0]} %" for n in TARGETS] + ["best $"]
    return df.to_string(index=False)
