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

from . import backtest, data
from .config import Settings
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
            "kill_switch_hit": result.killed_on}
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
                         "kill_switch_hit": st["kill_switch_hit"]})
    out["sensitivity"] = grid
    print(json.dumps(out, indent=2))


def cmd_trade(args, s: Settings) -> None:
    allow_live = os.environ.get("CRYPTOBOT_ALLOW_LIVE", "").lower() == "yes"
    dry_run = args.dry_run or (args.mode == "live" and not allow_live)
    if args.mode == "live" and dry_run:
        logging.warning("Dry run: no real orders. Set CRYPTOBOT_ALLOW_LIVE=yes to trade real money.")
    broker, data_exchange = build_broker(
        s, args.mode, os.environ.get("CRYPTOBOT_API_KEY"), os.environ.get("CRYPTOBOT_API_SECRET"), Path(args.paper_file))
    halt = os.environ.get("CRYPTOBOT_HALT", "").lower() in ("1", "yes", "true")
    print(json.dumps(run_once(s, broker, data_exchange, dry_run, halt), indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="cryptobot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="test the strategy on historical candles")
    bt.add_argument("--source", default="bitstamp", help="exchange to pull history from (default: bitstamp, data back to 2017)")
    bt.add_argument("--since", default="2017-01-01")
    bt.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")

    tr = sub.add_parser("trade", help="run one trading pass")
    tr.add_argument("--mode", choices=["paper", "live"], default="paper")
    tr.add_argument("--dry-run", action="store_true", help="show the orders without placing them")
    tr.add_argument("--paper-file", default=str(ROOT / "paper.json"))

    args = parser.parse_args(argv)
    s = Settings.from_env()
    {"backtest": cmd_backtest, "trade": cmd_trade}[args.cmd](args, s)


if __name__ == "__main__":
    main()
