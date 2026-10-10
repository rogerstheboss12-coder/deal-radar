"""Long-only trend following.

The signal for a candle uses only that candle and earlier ones, and is meant to be traded at the
next candle's open. Live trading and the backtest call the same function, so what was tested is
what runs.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .config import Settings


def indicators(df: pd.DataFrame, s: Settings) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    close = df["close"]
    out["close"] = close
    out["sma"] = close.rolling(s.trend_len).mean()
    out["ema_fast"] = close.ewm(span=s.fast_len, adjust=False).mean()
    out["ema_slow"] = close.ewm(span=s.slow_len, adjust=False).mean()
    out["prior_high"] = close.shift(1).rolling(s.breakout_len).max()
    prev_close = close.shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    out["atr"] = true_range.rolling(s.atr_len).mean()
    periods_per_year = 365 * 24 * 3600 / _timeframe_seconds(s.timeframe)
    out["vol"] = np.log(close).diff().rolling(s.vol_len).std() * math.sqrt(periods_per_year)
    return out


def target_weights(df: pd.DataFrame, s: Settings) -> pd.Series:
    """Fraction of total equity to hold in this coin after each candle closes (0 = all cash)."""
    ind = indicators(df, s)
    weights = np.zeros(len(ind))
    in_trade = False
    peak = 0.0
    for i, row in enumerate(ind.itertuples()):
        if i < s.warmup or any(math.isnan(v) for v in (row.sma, row.prior_high, row.atr, row.vol)):
            continue
        uptrend = row.close > row.sma and row.ema_fast > row.ema_slow
        if in_trade:
            peak = max(peak, row.close)
            if not uptrend or row.close < peak - s.atr_mult * row.atr:
                in_trade = False
        elif uptrend and row.close > row.prior_high:
            in_trade = True
            peak = row.close
        if in_trade:
            scale = min(1.0, s.target_vol / row.vol) if row.vol > 0 else 1.0
            weights[i] = s.max_weight * scale
    return pd.Series(weights, index=ind.index, name="weight")


def _timeframe_seconds(tf: str) -> int:
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[tf[-1]]
    return int(tf[:-1]) * unit
