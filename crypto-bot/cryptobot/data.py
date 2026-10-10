"""Historical candles through ccxt, with a CSV cache so repeat backtests don't re-download."""

from __future__ import annotations

import time
from pathlib import Path

import ccxt
import pandas as pd

from .strategy import _timeframe_seconds

CACHE = Path(__file__).resolve().parent.parent / "data"
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def make_exchange(name: str, api_key: str | None = None, secret: str | None = None) -> ccxt.Exchange:
    params = {"enableRateLimit": True}
    if api_key:
        params |= {"apiKey": api_key, "secret": secret}
    return getattr(ccxt, name)(params)


def fetch_history(exchange: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int) -> pd.DataFrame:
    """Page forward from since_ms until the latest candle. Drops the still-forming candle."""
    rows: list[list] = []
    cursor = since_ms
    step = _timeframe_seconds(timeframe) * 1000
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        batch = [r for r in batch if r[0] >= cursor]
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + step
        if cursor > exchange.milliseconds():
            break
        time.sleep(exchange.rateLimit / 1000)
    return to_frame(rows, step, exchange.milliseconds())


def fetch_recent(exchange: ccxt.Exchange, symbol: str, timeframe: str, candles: int) -> pd.DataFrame:
    """The last `candles` closed candles (enough for the strategy's warmup)."""
    step = _timeframe_seconds(timeframe) * 1000
    rows = exchange.fetch_ohlcv(symbol, timeframe, limit=candles + 1)
    return to_frame(rows, step, exchange.milliseconds())


def to_frame(rows: list[list], step_ms: int, now_ms: int) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("timestamp").sort_values("timestamp")
    df = df[df["timestamp"] + step_ms <= now_ms]  # only fully closed candles
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df.astype(float)


def load_or_fetch(exchange_name: str, symbol: str, timeframe: str, since: str, refresh: bool = False) -> pd.DataFrame:
    path = CACHE / f"{exchange_name}_{symbol.replace('/', '-')}_{timeframe}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    exchange = make_exchange(exchange_name)
    df = fetch_history(exchange, symbol, timeframe, exchange.parse8601(f"{since}T00:00:00Z"))
    if df.empty:
        raise RuntimeError(f"{exchange_name} returned no candles for {symbol} {timeframe}")
    CACHE.mkdir(exist_ok=True)
    df.to_csv(path)
    return df
