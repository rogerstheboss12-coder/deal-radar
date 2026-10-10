"""Command line.

    python -m cryptobot backtest                   # real history, fees included, vs buy-and-hold
    python -m cryptobot trade --mode paper         # simulated $100 account (paper.json)
    python -m cryptobot trade --mode live          # real account, dry run unless CRYPTOBOT_ALLOW_LIVE=yes
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from . import backtest, data, lottery
from .config import Settings
from .risk import Guard
from .trader import build_broker, run_once

ROOT = Path(__file__).resolve().parent.parent


def cmd_backtest(args, s: Settings) -> None:
    history = {sym: data.load_or_fetch(args.source, sym, s.timeframe, args.since, args.refresh) for sym in s.symbols}
    history = backtest.align(history)
    # Start measuring once every setting in the sensitivity grid below has warmed up, so all rows compare the same period.
    start = next(iter(history.values())).index[max(s.warmup, dataclasses.replace(s, trend_len=200).warmup)]

    def evaluate(settings: Settings, frames, start) -> tuple[dict, dict]:
        """Strategy vs buy-and-hold, both measured from `start` (candles before it only warm up the indicators)."""
        result = backtest.run(frames, settings)
        equity = result.equity[result.equity.index >= start]
        strat = backtest.summarize(equity, settings) | {
            "trades": result.trades, "fees_paid": round(result.fees, 2), "avg_exposure": round(result.exposure, 2),
            "pauses": result.pauses}
        bench_frames = {k: v[v.index >= start] for k, v in frames.items()}
        bench = backtest.summarize(backtest.buy_and_hold(bench_frames, settings), settings)
        return strat, bench

    strat, bench = evaluate(s, history, start)
    out = {"source": args.source, "symbols": s.symbols, "strategy": strat, "buy_and_hold": bench}

    # The same rules on each half of the history. A strategy that only works in one half is luck.
    index = next(iter(history.values())).index
    mid = index[s.warmup + (len(index) - s.warmup) // 2]
    halves = {}
    warm_from = index[index.get_loc(mid) - s.warmup]
    for name, frames, frm in (("first_half", {k: v[v.index < mid] for k, v in history.items()}, start),
                              ("second_half", {k: v[v.index >= warm_from] for k, v in history.items()}, mid)):
        halves[name] = dict(zip(("strategy", "buy_and_hold"), evaluate(s, frames, frm)))
    out["halves"] = halves

    # Nearby settings should give similar results; if one setting is great and its neighbours aren't, it's overfit.
    grid = []
    for trend_len in (50, 100, 200):
        for atr_mult in (2.0, 3.0, 4.0):
            variant = dataclasses.replace(s, trend_len=trend_len, atr_mult=atr_mult)
            st, _ = evaluate(variant, history, start)
            grid.append({"trend_len": trend_len, "atr_mult": atr_mult, "cagr_pct": st["cagr_pct"],
                         "max_drawdown_pct": st["max_drawdown_pct"], "sharpe": st["sharpe"], "trades": st["trades"],
                         "pauses": len(st["pauses"])})
    out["sensitivity"] = grid
    out["start_dates"] = start_date_test(s, history, evaluate)
    # Fees decide a lot on a small account: Kraken limit orders (0.25%), Kraken market (0.40%), Coinbase (0.60%).
    out["fee_levels"] = {f"{fee:.2%}": evaluate(dataclasses.replace(s, fee=fee), history, start)[0]["final_equity"]
                         for fee in (0.0025, 0.004, 0.006)}
    print(json.dumps(out, indent=2))


def start_date_test(s: Settings, history, evaluate, years: int = 2) -> dict:
    """Start with $100 at the beginning of every quarter and run `years` years. One backtest is one roll of
    the dice: this shows the spread of outcomes, including how bad an unlucky start can be."""
    index = next(iter(history.values())).index
    warm = max(s.warmup, dataclasses.replace(s, trend_len=200).warmup)
    runs = []
    for start in pd.date_range(index[warm], index[-1] - pd.DateOffset(years=years), freq="QS"):
        pos = index.searchsorted(start)
        end = index[pos] + pd.DateOffset(years=years)
        frames = {k: v[(v.index >= index[pos - s.warmup]) & (v.index < end)] for k, v in history.items()}
        strat, bench = evaluate(s, frames, index[pos])
        runs.append({"start": strat["start"], "strategy": strat["final_equity"], "buy_and_hold": bench["final_equity"],
                     "strategy_max_dd": strat["max_drawdown_pct"], "hold_max_dd": bench["max_drawdown_pct"],
                     "pauses": len(strat["pauses"])})
    if not runs:
        return {}
    df = pd.DataFrame(runs)

    def spread(col):
        return {"worst": round(df[col].min(), 2), "median": round(df[col].median(), 2), "best": round(df[col].max(), 2)}

    return {
        "runs": len(df),
        "years_each": years,
        "strategy_final_equity": spread("strategy"),
        "buy_and_hold_final_equity": spread("buy_and_hold"),
        "strategy_lost_money_pct": round((df["strategy"] < 100).mean() * 100),
        "buy_and_hold_lost_money_pct": round((df["buy_and_hold"] < 100).mean() * 100),
        "strategy_beat_hold_pct": round((df["strategy"] > df["buy_and_hold"]).mean() * 100),
        "worst_drawdown": {"strategy": df["strategy_max_dd"].min(), "buy_and_hold": df["hold_max_dd"].min()},
        "detail": runs,
    }


def cmd_lottery(args, s: Settings) -> None:
    history = {}
    for sym in args.coins.split(","):
        try:
            history[sym] = data.load_or_fetch(args.source, sym, "1d", args.since, args.refresh)
        except Exception as exc:  # a coin the exchange doesn't list shouldn't sink the whole test
            logging.warning("skipping %s: %s", sym, exc)
    result = lottery.run(history, step_days=args.step)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"{result['total_runs']:,} runs of $100 on {', '.join(result['coins'])}\n")
    print(lottery.table(result))
    print("\nDays when a perfect guess at 100x turned $100 into $1,000 in one day:")
    for coin, d in result["one_day_10x"].items():
        print(f"  {coin}: {d['winning_days']} of {d['days']} days ({d['pct']}%)")


def cmd_patterns(args, s: Settings) -> None:
    from . import patterns  # needs scikit-learn
    history = {}
    for sym in args.coins.split(","):
        try:
            history[sym] = data.load_or_fetch(args.source, sym, "1h", args.since, args.refresh)
        except Exception as exc:
            logging.warning("skipping %s: %s", sym, exc)
    rows = patterns.run(history)
    Path(args.out).write_text(json.dumps(rows, indent=2))
    print(patterns.table(rows))


def cmd_micro(args, s: Settings) -> None:
    from . import micro  # needs scikit-learn
    history = micro.fetch_all(args.since, args.refresh)
    universe = micro.pick_universe(history)
    btc = data.load_or_fetch(micro.EXCHANGE, "BTC/USD", micro.TIMEFRAME, args.since, args.refresh)
    result = micro.run(universe, btc)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(micro.table(result))


def cmd_ticks(args, s: Settings) -> None:
    from . import ticks
    result = ticks.run(args.coins, args.hours)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print("Real Kraken trades. Round-trip cost: 0.5% with limit (maker) orders, 0.8% with market (taker) orders.")
    print("PERFECT foresight = trade every move bigger than the cost, always in the right direction. No real")
    print("strategy can beat it; it also counts bid/ask bounce as a move, which nobody can capture.\n")
    print(ticks.table(result))


def cmd_trade(args, s: Settings) -> None:
    allow_live = os.environ.get("CRYPTOBOT_ALLOW_LIVE", "").lower() == "yes"
    dry_run = args.dry_run or (args.mode == "live" and not allow_live)
    if args.mode == "live" and dry_run:
        logging.warning("Dry run: no real orders. Set CRYPTOBOT_ALLOW_LIVE=yes to trade real money.")
    broker, data_exchange = build_broker(
        s, args.mode, os.environ.get("CRYPTOBOT_API_KEY"), os.environ.get("CRYPTOBOT_API_SECRET"), Path(args.paper_file))
    halt = os.environ.get("CRYPTOBOT_HALT", "").lower() in ("1", "yes", "true")
    # The circuit breaker's floor and pause survive between runs in a small file (Actions cache in CI).
    guard_file = ROOT / f"guard-{args.mode}.json"
    guard = Guard.from_dict(json.loads(guard_file.read_text()) if guard_file.exists() else None)
    result = run_once(s, broker, data_exchange, dry_run, halt, guard)
    if not dry_run:
        guard_file.write_text(json.dumps(guard.to_dict(), indent=2))
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="cryptobot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="test the strategy on historical candles")
    bt.add_argument("--source", default="bitstamp", help="exchange to pull history from (default: bitstamp, data back to 2017)")
    bt.add_argument("--since", default="2017-01-01")
    bt.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")

    lt = sub.add_parser("lottery", help="test all-in leveraged strategies from many start dates")
    lt.add_argument("--source", default="bitstamp")
    lt.add_argument("--coins", default="BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD")
    lt.add_argument("--since", default="2017-01-01")
    lt.add_argument("--step", type=int, default=7, help="days between start dates")
    lt.add_argument("--refresh", action="store_true")
    lt.add_argument("--out", default=str(ROOT / "lottery.json"))

    pt = sub.add_parser("patterns", help="machine-learning pattern search on hourly candles, walk-forward tested")
    pt.add_argument("--source", default="bitstamp")
    pt.add_argument("--coins", default="BTC/USD,ETH/USD,XRP/USD")
    pt.add_argument("--since", default="2018-01-01")
    pt.add_argument("--refresh", action="store_true")
    pt.add_argument("--out", default=str(ROOT / "patterns.json"))

    mc = sub.add_parser("micro", help="pattern search across many small coins on Coinbase")
    mc.add_argument("--since", default="2019-01-01")
    mc.add_argument("--refresh", action="store_true")
    mc.add_argument("--out", default=str(ROOT / "micro.json"))

    tk = sub.add_parser("ticks", help="second-by-second fee-wall check on real Kraken trades")
    tk.add_argument("--coins", type=int, default=10, help="number of small coins")
    tk.add_argument("--hours", type=float, default=72, help="hours of trades per small coin")
    tk.add_argument("--out", default=str(ROOT / "ticks.json"))

    tr = sub.add_parser("trade", help="run one trading pass")
    tr.add_argument("--mode", choices=["paper", "live"], default="paper")
    tr.add_argument("--dry-run", action="store_true", help="show the orders without placing them")
    tr.add_argument("--paper-file", default=str(ROOT / "paper.json"))

    args = parser.parse_args(argv)
    s = Settings.from_env()
    {"backtest": cmd_backtest, "lottery": cmd_lottery, "patterns": cmd_patterns, "micro": cmd_micro,
     "ticks": cmd_ticks, "trade": cmd_trade}[args.cmd](args, s)


if __name__ == "__main__":
    main()
