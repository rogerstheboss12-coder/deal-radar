# Crypto bot

A small, cautious trading bot for a ~$100 spot account on Kraken (BTC and ETH against USD).

## What it does

Once a day, after the daily candle closes, it decides for each coin: **hold it or sit in cash.**

- **Buy** when the coin is in an uptrend (price above its 100-day average, 20-day average above
  50-day) *and* it just closed at a 20-day high.
- **Sell** when the uptrend breaks, or price falls 3× the average daily range (ATR) below its
  best close since buying.
- **Size:** at most half the account per coin, less when the coin is unusually volatile.
- **Kill switch:** if the account falls below **$70**, it sells everything and stops trading. It's a
  fixed dollar floor, simulated in the backtest too. It acts at the next daily check, so a sudden crash
  can take the account below $70 before it sells.

It is long-only: no leverage, no shorting, no futures. You can lose money, but not more than you
put in. It trades rarely on purpose: on a $100 account, Kraken's fee of roughly 0.4% per market
order is the biggest drag.

**What to expect.** Trend following like this usually **trails buy-and-hold in strong bull markets**,
because it buys late and sells early. It earns its keep in **crashes and long bear markets**, where
it sits in cash. The goal is smaller drawdowns, not beating the market every year. The backtest
prints both side by side so you can judge.

## How to use it

Everything runs in GitHub Actions (**Actions → Crypto bot → Run workflow**), so you don't need a server.

1. **Backtest:** runs on every push to `crypto-bot/`. It reports returns, max drawdown, fees and
   trade count against buy-and-hold since 2017, plus each half of that history separately and a
   grid of nearby settings (if only one setting looks good, that's luck, not an edge).
2. **Paper trade:** runs daily with a fake $100 and real prices. Let it run for a few weeks first.
3. **Live:**
   1. Create a Kraken account, verify it, and deposit $100.
   2. Create an API key with **only** *Query Funds* and *Create & Modify Orders* turned on.
      **Never** turn on *Withdraw Funds*: then even a leaked key can't move money out.
   3. In GitHub → Settings → Secrets and variables → Actions, add secrets `CRYPTOBOT_API_KEY` and
      `CRYPTOBOT_API_SECRET`.
   4. Without anything else, the daily run is a **dry run**: it logs the orders it *would* place.
      Check those logs.
   5. To trade for real, add the repository **variable** `CRYPTOBOT_ALLOW_LIVE` = `yes`.

**To stop it:** set the variable `CRYPTOBOT_HALT` = `yes` (no orders are placed), or delete
`CRYPTOBOT_ALLOW_LIVE`. To cash out, sell on Kraken yourself. The bot only manages BTC, ETH and
USD, and ignores anything else in the account.

The daily schedule only runs from the repository's default branch, so it starts after this is merged.

## Running it locally

```sh
cd crypto-bot
pip install -r requirements.txt
python -m pytest -q
python -m cryptobot backtest                  # downloads history from Bitstamp, caches it in data/
python -m cryptobot trade --mode paper        # one pass on a fake $100 (paper.json)
python -m cryptobot trade --mode live         # dry run unless CRYPTOBOT_ALLOW_LIVE=yes
```

Any setting in `cryptobot/config.py` can be overridden with an environment variable, e.g.
`CRYPTOBOT_SYMBOLS=BTC/USD`, `CRYPTOBOT_KILL_EQUITY=80` or `CRYPTOBOT_EXCHANGE=coinbase` (any exchange
[ccxt](https://github.com/ccxt/ccxt) supports).

## Files

| File | What's in it |
| --- | --- |
| `cryptobot/strategy.py` | buy/sell rules, shared by the backtest and live trading |
| `cryptobot/backtest.py` | simulation with fees, slippage and next-day-open fills |
| `cryptobot/trader.py` | one live/paper pass: balances → target → orders, kill switch |
| `cryptobot/data.py` | candle downloads through ccxt |
| `cryptobot/config.py` | all settings |
