# Python Trading Robot

A trading robot written in Python that runs automated strategies using technical
analysis, connected to the **Charles Schwab Trader API** via
[`schwab-py`](https://schwab-py.readthedocs.io/).

This fork of [areed1192/python-trading-robot](https://github.com/areed1192/python-trading-robot)
adds a full migration from the retired TD Ameritrade API to Schwab, a dynamic
momentum strategy with a market-regime filter, and a set of live-trading
safeguards developed after real-world testing.

## Table of Contents

- [Overview](#overview)
- [What This Fork Adds](#what-this-fork-adds)
- [Setup](#setup)
- [Configuration](#configuration)
- [Usage](#usage)
- [Live Trading Safeguards](#live-trading-safeguards)
- [Tests](#tests)
- [Support These Projects](#support-these-projects)

## Overview

The core library mimics a few common scenarios:

1. **Portfolio** — maintain a portfolio of multiple instruments, calculate common
   risk metrics, and get real-time feedback as you trade.

2. **Trade** — define simple or complex orders in Python, including brackets like
   a take profit and stop loss attached to the entry order.

3. **StockFrame** — a real-time data table holding both historical and streaming
   prices, indexed for easy selection and further analysis.

4. **Indicators** — define indicator inputs (RSI, SMA, EMA, ...), calculate them,
   and have their values refresh as new prices arrive.

## What This Fork Adds

**Schwab API migration** (TD Ameritrade shut down its API after the Schwab
acquisition):

- Authentication through `schwab.easy_client()` with browser-assisted OAuth and
  token refresh (`token.json`).
- Account-hash resolution, httpx-style responses, Schwab quote/price-history
  payload shapes, and order placement via the Trader API.

**Dynamic momentum strategy** (`samples/dynamic_strategy.py`) — an automated,
self-resuming strategy built from new components in `pyrobot/`:

| Component | File | Role |
|---|---|---|
| Scanner | `pyrobot/scanner.py` | Filters a ~7,000-symbol NASDAQ/NYSE universe by price (budget / max_positions), average volume, and same-day momentum |
| Scorer | `pyrobot/scorer.py` | Composite score: 12-month momentum + EMA trend + RSI, weights configurable |
| Position sizer | `pyrobot/position_sizer.py` | Equal-weight or score-weighted allocation, whole shares only |
| Rebalancer | `pyrobot/rebalancer.py` | Periodically swaps the weakest holding for a meaningfully better candidate |
| Regime filter | `pyrobot/regime_filter.py` | SPY vs. its 200-day EMA; bear market pauses new entries and tightens stops |
| Backtester | `pyrobot/backtester.py` + `samples/backtest.py` | Test the strategy on historical data |
| Universe builder | `scripts/build_universe.py` | Merges NASDAQ/NYSE screener CSVs, S&P 500, and Russell 1000 into `data/universe.csv` with sector metadata |

The strategy persists its positions to `data/portfolio_state.json` after every
trade so a restart resumes exactly where it left off, monitors intraday RSI for
exits, applies a software stop loss, and enforces a per-sector position cap.

## Setup

Requires **Python 3.10+** (a `schwab-py` requirement).

If you are planning to make modifications to this project, install it in
`editable` mode:

```console
pip install -e .
```

If you don't plan to make modifications but want to use it across projects, do a
local install:

```console
pip install .
```

## Configuration

Copy the example config and fill in your Schwab developer credentials:

```console
cp config/config.example.ini config/config.ini
```

```ini
[main]
api_key = YOUR_SCHWAB_APP_KEY
app_secret = YOUR_SCHWAB_APP_SECRET
callback_url = https://127.0.0.1:8182
token_path = token.json
account_number = YOUR_ACCOUNT_NUMBER
```

You get the key and secret by registering an app on the
[Schwab Developer Portal](https://developer.schwab.com/). The callback URL must
use `127.0.0.1` (not `localhost`) and match the app registration exactly.

The `[strategy]`, `[universe]`, `[regime]`, and `[sector]` sections control the
dynamic strategy: total `budget`, `max_positions`, `min_volume`, stop-loss
percentages, scoring weights, allocation mode, rebalance cadence, and the
per-sector cap. See `config/config.example.ini` for the full annotated list.

`config/config.ini`, `token.json`, and runtime state files are gitignored —
never commit credentials.

## Usage

Run the dynamic momentum strategy (scan → score → buy → monitor → rebalance):

```console
cd python-trading-robot
python -u samples/dynamic_strategy.py
```

On first run (and whenever the 7-day Schwab refresh token expires) a browser
window opens for Schwab login; the token is then saved to `token.json`.
Progress is logged to the console and `logs/trading.log`.

The bot starts in **paper mode** (`paper_trading=True` in
`samples/dynamic_strategy.py`): orders are simulated, nothing is sent to
Schwab. Set `paper_trading=False` to trade live — and delete any stale
`data/portfolio_state.json` first so the bot doesn't resume phantom positions.

Using the core library directly:

```python
from pyrobot.robot import PyRobot

trading_robot = PyRobot(
    api_key='YOUR_SCHWAB_APP_KEY',
    app_secret='YOUR_SCHWAB_APP_SECRET',
    callback_url='https://127.0.0.1:8182',
    token_path='token.json',
    trading_account='YOUR_ACCOUNT_NUMBER',
    paper_trading=True
)
```

For more detailed examples, see `samples/trading_robot.py`.

## Live Trading Safeguards

Added after live testing surfaced real failure modes (a stale state file once
caused ~1,100 duplicate order submissions — all rejected, but only by luck):

- **Price precision** — every price is rounded to what Schwab accepts (2
  decimals ≥ $1, 4 decimals < $1), including bracketed child stop orders.
- **Rejection handling** — `execute_orders` checks both the HTTP response and
  Schwab's actual order status; a rejected order raises `OrderRejectedError`
  and is never recorded as a position.
- **Idempotent signals** — each buy/sell signal fires at most once per symbol;
  a signal that stays true across bars cannot resubmit the same order.
- **Broker reconciliation** — on resume, the saved state is compared against
  the real Schwab account; in live mode a mismatch refuses to start.
- **Loop pacing** — the bar-wait logic can no longer spin in a tight loop when
  the last bar is stale; one iteration per minute bar, always.

## Tests

Offline safety tests (no network, no credentials needed):

```console
python -m unittest tests.test_order_safety -v
```

## Support These Projects

This fork builds on the original project by [Alex Reed](https://github.com/areed1192).

**Patreon:**
Help support the original project and future projects by donating to his
[Patreon Page](https://www.patreon.com/sigmacoding).

**YouTube:**
If you'd like to watch more of his content, feel free to visit his YouTube
channel [Sigma Coding](https://www.youtube.com/c/SigmaCoding).
