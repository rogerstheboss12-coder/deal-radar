"""One trading pass: read prices and balances, compute target positions, place the orders.

Meant to run once per candle, shortly after it closes (00:05 UTC for daily candles). It keeps no
state of its own: positions come from the exchange (or the paper ledger), and signals are recomputed
from candle history each run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import ccxt

from .backtest import needs_trade
from .config import Settings
from .data import fetch_recent, make_exchange
from .strategy import target_weights

log = logging.getLogger("cryptobot")


@dataclass
class Order:
    symbol: str
    side: str
    amount: float  # in coins
    usd: float


class LiveBroker:
    def __init__(self, exchange: ccxt.Exchange):
        self.ex = exchange
        self.ex.load_markets()

    def balances(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.ex.fetch_balance()["total"].items() if v}

    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def prepare(self, order: Order) -> Order | None:
        """Round to the exchange's step size and drop orders below its minimums."""
        market = self.ex.market(order.symbol)
        amount = float(self.ex.amount_to_precision(order.symbol, order.amount))
        min_amount = (market.get("limits", {}).get("amount") or {}).get("min") or 0
        min_cost = (market.get("limits", {}).get("cost") or {}).get("min") or 0
        if amount <= 0 or amount < min_amount or amount * order.usd / order.amount < min_cost:
            log.info("skip %s %s %.8f: below exchange minimum", order.side, order.symbol, order.amount)
            return None
        return Order(order.symbol, order.side, amount, order.usd * amount / order.amount)

    def execute(self, order: Order) -> dict:
        return self.ex.create_order(order.symbol, "market", order.side, order.amount)


class PaperBroker:
    """Simulated account stored in a JSON file. Prices are real; fills include fees and slippage."""

    def __init__(self, exchange: ccxt.Exchange, path: Path, s: Settings):
        self.ex, self.path, self.s = exchange, path, s
        if path.exists():
            self.state = json.loads(path.read_text())
        else:
            self.state = {"balances": {s.quote: s.starting_cash}, "fills": []}

    def balances(self) -> dict[str, float]:
        return dict(self.state["balances"])

    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def prepare(self, order: Order) -> Order | None:
        return order

    def execute(self, order: Order) -> dict:
        base, quote = order.symbol.split("/")
        bal = self.state["balances"]
        price = order.usd / order.amount
        if order.side == "buy":
            price *= 1 + self.s.slippage
            cost = order.amount * price * (1 + self.s.fee)
            if cost > bal.get(quote, 0):
                order.amount = bal.get(quote, 0) / (price * (1 + self.s.fee))
                cost = bal.get(quote, 0)
            bal[quote] = bal.get(quote, 0) - cost
            bal[base] = bal.get(base, 0) + order.amount
        else:
            price *= 1 - self.s.slippage
            amount = min(order.amount, bal.get(base, 0))
            bal[base] = bal.get(base, 0) - amount
            bal[quote] = bal.get(quote, 0) + amount * price * (1 - self.s.fee)
        fill = {"symbol": order.symbol, "side": order.side, "amount": order.amount, "price": price,
                "time": self.ex.iso8601(self.ex.milliseconds())}
        self.state["fills"].append(fill)
        return fill

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))


def plan(s: Settings, weights: dict[str, float], prices: dict[str, float], balances: dict[str, float]) -> tuple[float, list[Order]]:
    """Orders that move the account to the target weights. Sells come first."""
    held = {sym: balances.get(sym.split("/")[0], 0.0) for sym in s.symbols}
    equity = balances.get(s.quote, 0.0) + sum(held[sym] * prices[sym] for sym in s.symbols)
    orders = []
    for sym in s.symbols:
        target_value = weights[sym] * equity
        delta = target_value - held[sym] * prices[sym]
        if not needs_trade(delta, target_value, equity, s):
            continue
        if delta < 0:
            amount = held[sym] if target_value == 0 else -delta / prices[sym]
            orders.append(Order(sym, "sell", amount, amount * prices[sym]))
        else:
            orders.append(Order(sym, "buy", delta / prices[sym], delta))
    orders.sort(key=lambda o: o.side != "sell")
    # Leave room for fees and price movement so buys don't fail for lack of cash.
    cash = balances.get(s.quote, 0.0) + sum(o.usd for o in orders if o.side == "sell") * (1 - s.fee)
    budget = cash * 0.98
    for o in orders:
        if o.side == "buy" and o.usd > budget:
            o.amount *= budget / o.usd
            o.usd = budget
        if o.side == "buy":
            budget -= o.usd
    return equity, [o for o in orders if o.usd >= s.min_order_usd or (o.side == "sell" and weights[o.symbol] == 0)]


def run_once(s: Settings, broker, data_exchange: ccxt.Exchange, dry_run: bool, halt: bool = False) -> dict:
    candles = s.warmup + 200
    weights, prices = {}, {}
    for sym in s.symbols:
        df = fetch_recent(data_exchange, sym, s.timeframe, candles)
        weights[sym] = float(target_weights(df, s).iloc[-1])
        prices[sym] = broker.price(sym)
    balances = broker.balances()
    equity, _ = plan(s, {sym: 0.0 for sym in s.symbols}, prices, balances)

    status = "ok"
    if halt:
        status = "halted (CRYPTOBOT_HALT is set): no orders placed"
        return report(s, status, equity, weights, prices, balances, [], dry_run)
    if equity < s.kill_equity:
        status = f"KILL SWITCH: equity {equity:.2f} < {s.kill_equity:.2f}, selling everything"
        weights = {sym: 0.0 for sym in s.symbols}
        log.warning(status)

    _, orders = plan(s, weights, prices, balances)
    placed = []
    for order in orders:
        order = broker.prepare(order)
        if order is None:
            continue
        log.info("%s %s %.8f (~$%.2f)%s", order.side, order.symbol, order.amount, order.usd, " [dry run]" if dry_run else "")
        if not dry_run:
            broker.execute(order)
        placed.append(order.__dict__)
    if not dry_run and isinstance(broker, PaperBroker):
        broker.save()
    return report(s, status, equity, weights, prices, broker.balances(), placed, dry_run)


def report(s, status, equity, weights, prices, balances, orders, dry_run) -> dict:
    return {
        "status": status,
        "dry_run": dry_run,
        "equity_usd": round(equity, 2),
        "target_weights": {k: round(v, 3) for k, v in weights.items()},
        "prices": prices,
        "balances": {k: v for k, v in balances.items() if k == s.quote or f"{k}/{s.quote}" in s.symbols},
        "orders": orders,
    }


def build_broker(s: Settings, mode: str, api_key: str | None, secret: str | None, paper_file: Path):
    data_exchange = make_exchange(s.exchange)
    if mode == "paper":
        return PaperBroker(data_exchange, paper_file, s), data_exchange
    if not api_key or not secret:
        raise SystemExit("Live trading needs CRYPTOBOT_API_KEY and CRYPTOBOT_API_SECRET.")
    return LiveBroker(make_exchange(s.exchange, api_key, secret)), data_exchange
