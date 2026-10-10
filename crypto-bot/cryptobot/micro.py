"""Small-coin pattern search: one model trained on many small coins at once, picking the few it likes best.

Universe: every USD market on Coinbase (including delisted ones whose history is still served, so dead
coins count against us), minus stablecoins and the large caps, keeping the lowest-volume coins that still
trade enough to fill a $100 order.

Every `horizon` candles the model scores every coin; the strategy holds the top `TOP_K` (equal weight,
long-only, since Coinbase spot can't short) when their probability clears the bar, otherwise cash.
Retrained each quarter, scored only on the next unseen quarter. Compared against:
  - the same pipeline on shuffled labels (luck),
  - picking TOP_K random coins (no skill at all),
  - holding every coin in the universe equally (the small-coin "index").
"""

from __future__ import annotations

import logging
import time

import ccxt
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from .data import CACHE, to_frame
from .patterns import features
from .strategy import _timeframe_seconds

log = logging.getLogger("cryptobot")

EXCHANGE = "coinbaseexchange"
TIMEFRAME = "6h"
BIG = {"BTC", "ETH", "XRP", "SOL", "BNB", "DOGE", "ADA", "TRX", "AVAX", "LINK", "DOT", "LTC", "BCH", "XLM",
       "SHIB", "TON", "SUI", "HBAR", "UNI", "NEAR", "APT", "ICP", "ETC", "POL", "MATIC", "ATOM", "FIL", "WBTC",
       "CBETH", "STETH", "WETH"}
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "GUSD", "PAX", "USDP", "BUSD", "TUSD", "UST", "EURC", "PAXG", "FDUSD",
          "RLUSD", "USDS", "GYEN", "XSGD", "EUROC"}
MIN_DAILY_USD = 50_000     # enough liquidity that a $100 order barely moves the price
MAX_COINS = 70
MIN_HISTORY_DAYS = 365
COST = 0.009               # per side: 0.6% Coinbase taker fee + ~0.3% spread/slippage on small coins
TOP_K = 3
THRESHOLDS = (0.55, 0.60, 0.65)
HORIZONS = (1, 4)          # in 6h candles: 6 hours and 1 day
TRAIN_YEARS = 2
SHUFFLES = 3


def fetch_all(since: str = "2019-01-01", refresh: bool = False) -> dict[str, pd.DataFrame]:
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    markets = ex.load_markets()
    candidates = sorted(
        m["symbol"] for m in markets.values()
        if m["quote"] == "USD" and m.get("spot", True) and m["base"] not in BIG | STABLE)
    log.info("%d candidate small-coin USD markets (active and delisted)", len(candidates))
    step = _timeframe_seconds(TIMEFRAME) * 1000
    since_ms = ex.parse8601(f"{since}T00:00:00Z")
    out = {}
    for sym in candidates:
        path = CACHE / f"{EXCHANGE}_{sym.replace('/', '-')}_{TIMEFRAME}.csv"
        if path.exists() and not refresh:
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True)
        else:
            try:
                df = _history(ex, sym, step, since_ms)
            except Exception as exc:
                log.warning("skip %s: %s", sym, exc)
                continue
            CACHE.mkdir(exist_ok=True)
            df.to_csv(path)
        if len(df) * step / 86_400_000 >= MIN_HISTORY_DAYS:
            out[sym] = df
    return out


def _history(ex, sym: str, step: int, since_ms: int) -> pd.DataFrame:
    rows, cursor = [], since_ms
    while cursor < ex.milliseconds():
        batch = ex.fetch_ohlcv(sym, TIMEFRAME, since=cursor, limit=300)
        batch = [r for r in batch if r[0] >= cursor]
        if batch:
            rows.extend(batch)
            cursor = batch[-1][0] + step
        else:
            cursor += 300 * step  # gap (not listed yet, or halted): skip ahead
        time.sleep(ex.rateLimit / 1000)
    return to_frame(rows, step, ex.milliseconds())


def pick_universe(history: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """The lowest-volume coins that still clear the liquidity floor."""
    per_day = 86_400 / _timeframe_seconds(TIMEFRAME)
    dollar_vol = {s: float((df["close"] * df["volume"]).median() * per_day) for s, df in history.items()}
    liquid = sorted((v, s) for s, v in dollar_vol.items() if v >= MIN_DAILY_USD)
    chosen = [s for _, s in liquid[:MAX_COINS]]
    log.info("universe: %d coins, median daily volume $%s .. $%s", len(chosen),
             f"{liquid[0][0]:,.0f}" if liquid else "-", f"{liquid[len(chosen) - 1][0]:,.0f}" if liquid else "-")
    return {s: history[s] for s in chosen}


def panel(universe: dict[str, pd.DataFrame], btc: pd.DataFrame | None, horizon: int) -> pd.DataFrame:
    """One row per (time, coin): features, plus the forward return from the next candle's open."""
    frames = []
    for sym, df in universe.items():
        f = features(df, btc)
        # Decide at candle close t, buy at open t+1, sell at open t+1+horizon.
        f["fwd"] = df["open"].shift(-(1 + horizon)) / df["open"].shift(-1) - 1
        f["coin"] = sym
        frames.append(f)
    p = pd.concat(frames).replace([np.inf, -np.inf], np.nan)
    p = p.dropna(subset=["fwd", "ret_24h"])
    # Cross-sectional context: how each coin ranks against the others right now.
    for col in ("ret_4h", "ret_24h", "ret_168h", "volume_z_24h", "vol_72h"):
        p[f"xs_rank_{col}"] = p.groupby(level=0)[col].rank(pct=True)
    p["xs_breadth_24h"] = p.groupby(level=0)["ret_24h"].transform(lambda s: (s > 0).mean())
    return p.sort_index()


def walk_forward(p: pd.DataFrame, horizon: int, shuffle: bool, seed: int = 0) -> pd.DataFrame:
    step = pd.Timedelta(seconds=_timeframe_seconds(TIMEFRAME))
    X = p.drop(columns=["fwd", "coin"])
    X = X.loc[:, X.notna().any()]  # a feature with no values at all (e.g. missing volume) breaks the model
    # Label: did the trade clear its round-trip cost? That is the only kind of "up" worth predicting here.
    y = (p["fwd"] > 2 * COST).astype(int).to_numpy()
    rng = np.random.default_rng(seed)
    start = p.index.min() + pd.DateOffset(years=TRAIN_YEARS)
    out = []
    for q in pd.date_range(start.normalize(), p.index.max(), freq="QS"):
        train = p.index < q - step * (horizon + 2)
        test = (p.index >= q) & (p.index < q + pd.DateOffset(months=3))
        if not test.any() or train.sum() < 5_000:
            continue
        y_train = rng.permutation(y[train]) if shuffle else y[train]
        model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=31,
                                               min_samples_leaf=500, l2_regularization=1.0, random_state=seed)
        model.fit(X[train], y_train)
        out.append(pd.DataFrame({"coin": p["coin"][test], "fwd": p["fwd"][test],
                                 "p": model.predict_proba(X[test])[:, 1]}, index=p.index[test]))
    return pd.concat(out)


def trade(pred: pd.DataFrame, horizon: int, threshold: float | None, rng: np.random.Generator | None = None) -> dict:
    """Every `horizon` candles hold the TOP_K highest-scoring coins (equal weight) or cash.
    threshold=None with rng: pick TOP_K random coins every time (the no-skill baseline)."""
    groups = dict(tuple(pred.groupby(level=0)))
    times = sorted(groups)[::horizon]
    held: set[str] = set()
    equity, curve, trades = 100.0, [], 0
    for t in times:
        now = groups[t]
        if rng is not None:
            picks = set(rng.choice(now["coin"].to_numpy(), size=min(TOP_K, len(now)), replace=False))
        else:
            best = now[now["p"] > threshold].nlargest(TOP_K, "p")
            picks = set(best["coin"])
        # Each slot is 1/TOP_K of equity; pay COST on every slot bought or sold.
        changed = len(picks ^ held)
        trades += len(picks - held)
        equity *= 1 - changed / TOP_K * COST
        if picks:
            r = now.set_index("coin").loc[list(picks), "fwd"].to_numpy()
            equity *= 1 + r.sum() / TOP_K  # empty slots sit in cash
        held = picks
        curve.append(equity)
    curve = np.array(curve)
    return {"final": round(float(curve[-1]), 2), "trades": trades,
            "max_dd_pct": round(float((curve / np.maximum.accumulate(curve) - 1).min() * 100), 1)}


def index_hold(pred: pd.DataFrame, horizon: int) -> float:
    """Hold every coin in the universe equally, rebalanced each period (no trading costs: a generous benchmark)."""
    means = pred.groupby(level=0)["fwd"].mean()
    eq = 100.0
    for r in means.iloc[::horizon]:
        eq *= 1 + float(r)
    return round(eq, 2)


def run(universe: dict[str, pd.DataFrame], btc: pd.DataFrame | None) -> dict:
    rows = []
    for horizon in HORIZONS:
        p = panel(universe, btc, horizon)
        real = walk_forward(p, horizon, shuffle=False)
        fakes = [walk_forward(p, horizon, shuffle=True, seed=k + 1) for k in range(SHUFFLES)]
        randoms = [trade(real, horizon, None, np.random.default_rng(k))["final"] for k in range(20)]
        bench = index_hold(real, horizon)
        for th in THRESHOLDS:
            m = trade(real, horizon, th)
            luck = [trade(f, horizon, th)["final"] for f in fakes]
            rows.append({"horizon": f"{horizon * 6}h", "conf": th, "trades": m["trades"], "$ final": m["final"],
                         "max dd %": m["max_dd_pct"], "luck med $": float(np.median(luck)), "luck best $": max(luck),
                         "random picks med $": float(np.median(randoms)), "hold all $": bench,
                         "from": str(real.index.min().date()), "to": str(real.index.max().date())})
    return {"coins": sorted(universe), "rows": rows}


def table(result: dict) -> str:
    df = pd.DataFrame(result["rows"])
    head = f"{len(result['coins'])} small coins: {', '.join(c.split('/')[0] for c in result['coins'])}\n"
    period = f"Out-of-sample: {df['from'].iloc[0]} .. {df['to'].iloc[0]}, $100 start, {COST:.1%} cost per side\n"
    return head + period + df.drop(columns=["from", "to"]).to_string(index=False)
