import dataclasses

import numpy as np
import pandas as pd
import pytest

from cryptobot import backtest
from cryptobot.config import Settings
from cryptobot.strategy import target_weights
from cryptobot.trader import PaperBroker, plan, run_once


def candles(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="D", tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.01,
                         "low": np.minimum(opens, closes) * 0.99, "close": closes, "volume": 1.0}, index=idx)


def wobble(n, drift, seed=0):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.02, n)))


def test_no_signal_during_warmup():
    s = Settings()
    w = target_weights(candles(wobble(400, 0.01)), s)
    assert (w.iloc[: s.warmup] == 0).all()


def test_long_in_uptrend_flat_in_downtrend():
    s = Settings()
    up = target_weights(candles(wobble(400, 0.01)), s)
    down = target_weights(candles(wobble(400, -0.01)), s)
    assert up.iloc[-50:].min() > 0
    assert (down == 0).all()


def test_weights_never_exceed_cap():
    s = Settings()
    w = target_weights(candles(wobble(600, 0.003, seed=3)), s)
    assert w.max() <= s.max_weight + 1e-12


def test_signal_ignores_future_candles():
    s = Settings()
    prices = wobble(500, 0.004, seed=7)
    full = target_weights(candles(prices), s)
    cut = target_weights(candles(prices[:400]), s)
    pd.testing.assert_series_equal(full.iloc[:400], cut)


def test_trailing_stop_exits_after_crash():
    s = Settings()
    prices = np.concatenate([wobble(300, 0.01), wobble(300, 0.01)[-1] * np.linspace(1, 0.6, 15)])
    w = target_weights(candles(prices), s)
    assert w.iloc[299] > 0 and w.iloc[-1] == 0


def test_backtest_pays_fees_and_stays_solvent():
    s = Settings()
    data = {"BTC/USD": candles(wobble(800, 0.001, seed=1)), "ETH/USD": candles(wobble(800, 0.001, seed=2))}
    r = backtest.run(data, s)
    assert r.trades > 0 and r.fees > 0
    assert (r.equity > 0).all()
    assert r.equity.iloc[: s.warmup + 1].eq(s.starting_cash).all()


def test_flat_market_with_no_trades_keeps_cash():
    s = Settings()
    data = {"BTC/USD": candles(np.full(400, 100.0))}
    r = backtest.run(data, dataclasses.replace(s, symbols=["BTC/USD"]))
    assert r.trades == 0 and r.equity.iloc[-1] == pytest.approx(s.starting_cash)


def test_plan_sells_before_buying_and_respects_cash():
    s = Settings()
    prices = {"BTC/USD": 50_000.0, "ETH/USD": 2_000.0}
    balances = {"USD": 0.0, "BTC": 0.002, "ETH": 0.0}  # $100 all in BTC
    equity, orders = plan(s, {"BTC/USD": 0.0, "ETH/USD": 0.5}, prices, balances)
    assert equity == pytest.approx(100)
    assert [o.side for o in orders] == ["sell", "buy"]
    assert orders[0].amount == pytest.approx(0.002)  # full exit sells the whole position
    assert orders[1].usd <= 100 * (1 - s.fee) * 0.98 + 1e-9


def test_plan_skips_small_adjustments():
    s = Settings()
    prices = {"BTC/USD": 50_000.0, "ETH/USD": 2_000.0}
    balances = {"USD": 52.0, "BTC": 0.00096, "ETH": 0.0}  # BTC worth $48, target $50
    _, orders = plan(s, {"BTC/USD": 0.5, "ETH/USD": 0.0}, prices, balances)
    assert orders == []


class FakeExchange:
    """Stands in for ccxt: serves fixed candles and a ticker."""

    rateLimit = 0

    def __init__(self, frames):
        self.frames = frames

    def milliseconds(self):
        last = max(df.index[-1] for df in self.frames.values())
        return int((last + pd.Timedelta(days=2)).timestamp() * 1000)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        df = self.frames[symbol].tail(limit)
        return [[int(ts.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume] for ts, r in zip(df.index, df.itertuples())]

    def fetch_ticker(self, symbol):
        return {"last": float(self.frames[symbol]["close"].iloc[-1])}

    def iso8601(self, ms):
        return str(ms)


def test_paper_round_trip(tmp_path):
    s = Settings()
    ex = FakeExchange({"BTC/USD": candles(wobble(500, 0.01, seed=1)), "ETH/USD": candles(wobble(500, -0.01, seed=2))})
    broker = PaperBroker(ex, tmp_path / "paper.json", s)
    out = run_once(s, broker, ex, dry_run=False)
    assert [o["symbol"] for o in out["orders"]] == ["BTC/USD"]
    assert out["balances"]["BTC"] > 0 and out["balances"]["USD"] < 100
    # Second pass with unchanged prices should do nothing.
    again = run_once(s, PaperBroker(ex, tmp_path / "paper.json", s), ex, dry_run=False)
    assert again["orders"] == []


def test_kill_switch_sells_everything(tmp_path):
    s = Settings()
    ex = FakeExchange({"BTC/USD": candles(wobble(500, 0.01, seed=1)), "ETH/USD": candles(wobble(500, -0.01, seed=2))})
    broker = PaperBroker(ex, tmp_path / "paper.json", s)
    price = ex.fetch_ticker("BTC/USD")["last"]
    broker.state["balances"] = {"USD": 10.0, "BTC": 50.0 / price}  # equity $60 < $70
    out = run_once(s, broker, ex, dry_run=False)
    assert out["status"].startswith("KILL SWITCH")
    assert out["balances"]["BTC"] == 0


def test_dry_run_and_halt_place_nothing(tmp_path):
    s = Settings()
    ex = FakeExchange({"BTC/USD": candles(wobble(500, 0.01, seed=1)), "ETH/USD": candles(wobble(500, 0.01, seed=2))})
    out = run_once(s, PaperBroker(ex, tmp_path / "p.json", s), ex, dry_run=True)
    assert out["orders"] and not (tmp_path / "p.json").exists()
    out = run_once(s, PaperBroker(ex, tmp_path / "p.json", s), ex, dry_run=False, halt=True)
    assert out["orders"] == [] and out["status"].startswith("halted")
