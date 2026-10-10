"""Machine-learning pattern search on hourly candles, tested walk-forward.

A gradient-boosted model looks at ~40 features of the recent past and predicts whether the price will be
higher `horizon` hours later. It is retrained every quarter on everything before that quarter (with a gap
so no training label overlaps the test period) and only ever scored on the following, unseen quarter.

To tell a real pattern from luck, the identical pipeline also runs with shuffled training labels, a model
that by construction can't have learned anything. A real edge has to clearly beat that.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

COST = 0.0007          # per unit of position change: 0.05% perp taker fee + 0.02% slippage
THRESHOLDS = (0.55, 0.60, 0.65)  # trade only when the model is at least this confident (all reported, none tuned)
SHUFFLES = 3            # luck baselines per setting
TRAIN_YEARS = 2        # the first two years only train; scoring starts after
HORIZONS = (4, 24)     # hours


def features(df: pd.DataFrame, btc: pd.DataFrame | None = None) -> pd.DataFrame:
    """Everything here uses candles up to and including the current one, nothing later."""
    c, v = df["close"], df["volume"]
    logret = np.log(c).diff()
    f = pd.DataFrame(index=df.index)
    for n in (1, 2, 4, 8, 12, 24, 48, 72, 168, 336):
        f[f"ret_{n}h"] = np.log(c / c.shift(n))
    for n in (12, 24, 72, 168):
        f[f"vol_{n}h"] = logret.rolling(n).std()
        hi, lo = df["high"].rolling(n).max(), df["low"].rolling(n).min()
        f[f"range_pos_{n}h"] = (c - lo) / (hi - lo).replace(0, np.nan)
    for n in (24, 72, 168, 500):
        f[f"dist_ma_{n}h"] = c / c.rolling(n).mean() - 1
    for n in (24, 168):
        f[f"volume_z_{n}h"] = (v - v.rolling(n).mean()) / v.rolling(n).std().replace(0, np.nan)
    f["vol_ratio"] = f["vol_24h"] / f["vol_168h"]
    f["candle_body"] = (df["close"] - df["open"]) / df["open"]
    f["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"]
    f["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["open"]
    gain, loss = logret.clip(lower=0), (-logret).clip(lower=0)
    f["rsi_14"] = gain.rolling(14).mean() / (gain.rolling(14).mean() + loss.rolling(14).mean())
    f["hour"] = df.index.hour
    f["weekday"] = df.index.weekday
    if btc is not None:
        b = btc["close"].reindex(df.index)
        for n in (1, 4, 24, 168):
            f[f"btc_ret_{n}h"] = np.log(b / b.shift(n))
        f["rel_strength_24h"] = f["ret_24h"] - f["btc_ret_24h"]
    return f


def walk_forward(df: pd.DataFrame, feats: pd.DataFrame, horizon: int, shuffle: bool, seed: int = 0) -> pd.DataFrame:
    """Out-of-sample probabilities for every hour after the first TRAIN_YEARS years."""
    close = df["close"]
    # One hour of delay before entering: signal at close t, in at close t+1, out at close t+1+horizon.
    fwd = close.shift(-(1 + horizon)) / close.shift(-1) - 1
    data = feats.assign(fwd=fwd).dropna()
    y = (data["fwd"] > 0).astype(int)
    X = data.drop(columns="fwd")
    X = X.loc[:, X.notna().any()]
    rng = np.random.default_rng(seed)

    start = data.index[0] + pd.DateOffset(years=TRAIN_YEARS)
    out = []
    for q_start in pd.date_range(start.normalize(), data.index[-1], freq="QS"):
        q_end = q_start + pd.DateOffset(months=3)
        cutoff = q_start - pd.Timedelta(hours=horizon + 2)  # purge labels that peek into the test quarter
        train = X.index < cutoff
        test = (X.index >= q_start) & (X.index < q_end)
        if not test.any():
            continue
        y_train = y[train].to_numpy()
        if shuffle:
            y_train = rng.permutation(y_train)
        model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=31,
                                               min_samples_leaf=500, l2_regularization=1.0, random_state=seed)
        model.fit(X[train], y_train)
        out.append(pd.DataFrame({"p_up": model.predict_proba(X[test])[:, 1], "fwd": data["fwd"][test]},
                                index=X.index[test]))
    return pd.concat(out)


def trade(pred: pd.DataFrame, horizon: int, threshold: float) -> dict:
    """Non-overlapping trades: decide every `horizon` hours, long/short/flat, pay costs on position changes."""
    p = pred.iloc[::horizon]
    pos = np.where(p["p_up"] > threshold, 1.0, np.where(p["p_up"] < 1 - threshold, -1.0, 0.0))
    turnover = np.abs(np.diff(np.concatenate([[0.0], pos])))
    rets = pos * p["fwd"].to_numpy() - turnover * COST
    traded = pos != 0
    out = {"decisions": int(len(pos)), "trades": int(traded.sum()),
           "hit_rate_pct": round(float((np.sign(p["fwd"].to_numpy()[traded]) == pos[traded]).mean() * 100), 1)
           if traded.any() else None,
           "avg_trade_bps": round(float(rets[traded].mean() * 1e4), 1) if traded.any() else None}
    for lev in (1, 3, 10):
        eq = np.cumprod(1 + np.maximum(rets * lev, -1.0)) * 100
        out[f"final_{lev}x"] = round(float(eq[-1]), 2)
        out[f"max_dd_{lev}x_pct"] = round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 1)
    return out


def run(history: dict[str, pd.DataFrame]) -> list[dict]:
    btc = history.get("BTC/USD")
    rows = []
    for coin, df in history.items():
        feats = features(df, None if coin == "BTC/USD" else btc)
        for horizon in HORIZONS:
            real = walk_forward(df, feats, horizon, shuffle=False)
            fakes = [walk_forward(df, feats, horizon, shuffle=True, seed=k + 1) for k in range(SHUFFLES)]
            hold = float(df["close"][real.index[-1]] / df["close"][real.index[0]] * 100)
            for threshold in THRESHOLDS:
                luck = [trade(f, horizon, threshold) for f in fakes]
                rows.append({"coin": coin, "horizon_h": horizon, "threshold": threshold,
                             "test_from": str(real.index[0].date()), "test_to": str(real.index[-1].date()),
                             "model": trade(real, horizon, threshold),
                             "shuffled_best_1x": max(x["final_1x"] for x in luck),
                             "shuffled_median_1x": float(np.median([x["final_1x"] for x in luck])),
                             "buy_and_hold_final": round(hold, 2)})
    return rows


def table(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        m = r["model"]
        lines.append({"coin": r["coin"], "horizon": f"{r['horizon_h']}h", "conf": r["threshold"], "trades": m["trades"],
                      "hit %": m["hit_rate_pct"], "avg bps": m["avg_trade_bps"], "$ 1x": m["final_1x"],
                      "luck med $": r["shuffled_median_1x"], "luck best $": r["shuffled_best_1x"],
                      "$ 3x": m["final_3x"], "$ 10x": m["final_10x"], "dd 3x %": m["max_dd_3x_pct"],
                      "hold $": r["buy_and_hold_final"]})
    period = f"{rows[0]['test_from']} .. {rows[0]['test_to']}" if rows else ""
    return f"Out-of-sample test period: {period}  ($100 start; 'luck' = same pipeline on shuffled labels)\n" + \
        pd.DataFrame(lines).to_string(index=False)
