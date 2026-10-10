"""Bot settings. Every field can be overridden with a CRYPTOBOT_<NAME> environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


@dataclass
class Settings:
    # Where orders go. Kraken: available in the US, spot-only API keys, ~0.25-0.40% fees on small accounts.
    exchange: str = "kraken"
    quote: str = "USD"
    symbols: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    timeframe: str = "1d"

    # Strategy: long-only trend following on daily candles, flat in downtrends.
    trend_len: int = 100        # price must be above this SMA
    fast_len: int = 20          # ...and fast EMA above slow EMA
    slow_len: int = 50
    breakout_len: int = 20      # (re-)entry needs a close above the prior N-day high
    atr_len: int = 14
    atr_mult: float = 3.0       # trailing stop: highest close since entry minus atr_mult * ATR
    vol_len: int = 30
    target_vol: float = 0.60    # scale each position down when annualised volatility exceeds this

    # Money management.
    max_weight: float = 0.5           # max share of equity in any one coin
    min_order_usd: float = 10.0       # below this, orders are skipped (exchange minimums, fees)
    rebalance_band: float = 0.10      # ignore target changes smaller than 10% of equity
    pause_drawdown: float = 0.30      # if equity drops 30% from where it started (or last resumed), go to cash...
    pause_days: int = 30              # ...for this long, then trade again with a new floor

    # Backtest cost model: market orders pay the taker fee plus slippage.
    fee: float = 0.004
    slippage: float = 0.0005
    starting_cash: float = 100.0

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        for f in fields(cls):
            raw = os.environ.get(f"CRYPTOBOT_{f.name.upper()}")
            if raw is None:
                continue
            current = getattr(s, f.name)
            if isinstance(current, list):
                value = [x.strip() for x in raw.split(",") if x.strip()]
            elif isinstance(current, bool):
                value = raw.lower() in ("1", "true", "yes")
            else:
                value = type(current)(raw)
            setattr(s, f.name, value)
        return s

    @property
    def warmup(self) -> int:
        """Candles needed before the first signal is valid."""
        return max(self.trend_len, self.slow_len * 3, self.breakout_len, self.atr_len, self.vol_len) + 1
