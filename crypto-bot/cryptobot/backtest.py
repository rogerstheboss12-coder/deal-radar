"""Portfolio backtest. Each candle's signal is executed at the next candle's open, after fees and slippage."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .config import Settings
from .strategy import _timeframe_seconds, target_weights


@dataclass
class Result:
    equity: pd.Series
    trades: int
    fees: float
    exposure: float  # average share of equity invested

    def stats(self, s: Settings) -> dict:
        return summarize(self.equity, s) | {
            "trades": self.trades,
            "fees_paid": round(self.fees, 2),
            "avg_exposure": round(self.exposure, 2),
        }


def align(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Trim every symbol's candles to the dates all symbols have."""
    common = None
    for df in data.values():
        common = df.index if common is None else common.intersection(df.index)
    return {sym: df.loc[common] for sym, df in data.items()}


def run(data: dict[str, pd.DataFrame], s: Settings) -> Result:
    data = align(data)
    weights = {sym: target_weights(df, s) for sym, df in data.items()}
    index = next(iter(data.values())).index
    cash = s.starting_cash
    qty = {sym: 0.0 for sym in data}
    equity, trades, fees, exposure = [], 0, 0.0, []

    for i, ts in enumerate(index):
        if i > 0:
            opens = {sym: data[sym]["open"].iloc[i] for sym in data}
            total = cash + sum(qty[sym] * opens[sym] for sym in data)
            orders = []
            for sym in data:
                target_value = weights[sym].iloc[i - 1] * total
                delta = target_value - qty[sym] * opens[sym]
                if needs_trade(delta, target_value, total, s):
                    orders.append((sym, delta))
            # Sell first so the cash is there for the buys.
            for sym, delta in sorted(orders, key=lambda o: o[1]):
                price = opens[sym] * (1 + s.slippage if delta > 0 else 1 - s.slippage)
                if delta > 0:
                    delta = min(delta, cash / (1 + s.fee))
                    if delta <= 0:
                        continue
                    qty[sym] += delta / price
                    cash -= delta * (1 + s.fee)
                else:
                    sell_qty = min(qty[sym], -delta / opens[sym])
                    proceeds = sell_qty * price
                    qty[sym] -= sell_qty
                    cash += proceeds * (1 - s.fee)
                    delta = -proceeds
                fees += abs(delta) * s.fee
                trades += 1
        closes = {sym: data[sym]["close"].iloc[i] for sym in data}
        held = sum(qty[sym] * closes[sym] for sym in data)
        equity.append(cash + held)
        exposure.append(held / (cash + held) if cash + held > 0 else 0.0)

    return Result(pd.Series(equity, index=index), trades, fees, sum(exposure) / len(exposure))


def needs_trade(delta: float, target_value: float, total: float, s: Settings) -> bool:
    """Skip tiny adjustments that would only pay fees. Always allow a full exit."""
    if abs(delta) < s.min_order_usd:
        return False
    if target_value == 0:
        return True
    return abs(delta) >= s.rebalance_band * total


def buy_and_hold(data: dict[str, pd.DataFrame], s: Settings) -> pd.Series:
    """Benchmark: split the money evenly across the coins on day one and never trade again."""
    data = align(data)
    per_coin = s.starting_cash / len(data) * (1 - s.fee)
    equity = sum(per_coin / df["open"].iloc[0] * df["close"] for df in data.values())
    return equity


def summarize(equity: pd.Series, s: Settings) -> dict:
    years = len(equity) * _timeframe_seconds(s.timeframe) / (365 * 86400)
    rets = equity.pct_change().dropna()
    per_year = 365 * 86400 / _timeframe_seconds(s.timeframe)
    sharpe = rets.mean() / rets.std() * math.sqrt(per_year) if rets.std() > 0 else 0.0
    drawdown = (equity / equity.cummax() - 1).min()
    total = equity.iloc[-1] / equity.iloc[0] - 1
    return {
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return_pct": round(total * 100, 1),
        "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 1) if years > 0 and total > -1 else None,
        "max_drawdown_pct": round(drawdown * 100, 1),
        "sharpe": round(float(sharpe), 2),
    }
