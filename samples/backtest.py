"""
Backtest entry point — runs the momentum strategy on historical data.

This lets you evaluate the strategy before deploying real capital.
Uses the same scoring logic as dynamic_strategy.py but applied to past dates.

To run:
    cd python-trading-robot
    python samples/backtest.py

Configure:
    - BACKTEST_START / BACKTEST_END — the date range to simulate
    - All other parameters are read from config/config.ini (same as live bot)

Output:
    - Per-period returns printed to console and saved to logs/backtest.log
    - Summary: total return and annualized return over the period
"""

import os
import logging
import pathlib
import pprint
from datetime import datetime
from configparser import ConfigParser

from pyrobot.robot import PyRobot
from pyrobot.backtester import Backtester


# ── Logging ────────────────────────────────────────────────────────────────────

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('logs/backtest.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

config = ConfigParser()
config.read('config/config.ini')

API_KEY = config.get('main', 'api_key')
APP_SECRET = config.get('main', 'app_secret')
CALLBACK_URL = config.get('main', 'callback_url')
TOKEN_PATH = config.get('main', 'token_path')
ACCOUNT_NUMBER = config.get('main', 'account_number')

BUDGET = config.getfloat('strategy', 'budget')
MAX_POSITIONS = config.getint('strategy', 'max_positions')
MOMENTUM_LOOKBACK_DAYS = config.getint('strategy', 'momentum_lookback_days')
REBALANCE_FREQUENCY_DAYS = config.getint('strategy', 'rebalance_frequency_days')

NASDAQ_SYMBOLS_PATH = pathlib.Path('data/nasdaq_symbols.csv')

# ── Backtest date range ────────────────────────────────────────────────────────
# Edit these to change the simulation window.

BACKTEST_START = datetime(2024, 1, 1)
BACKTEST_END = datetime(2024, 12, 31)


# ── Validation ─────────────────────────────────────────────────────────────────

if not NASDAQ_SYMBOLS_PATH.exists():
    raise FileNotFoundError(
        "data/nasdaq_symbols.csv not found.\n"
        "Download it from: https://www.nasdaq.com/market-activity/stocks/screener"
    )


# ── Run backtest ───────────────────────────────────────────────────────────────

logger.info("=" * 60)
logger.info("Momentum Strategy Backtest")
logger.info("=" * 60)
logger.info(f"Period:         {BACKTEST_START.date()} → {BACKTEST_END.date()}")
logger.info(f"Budget:         ${BUDGET:,.2f}")
logger.info(f"Max positions:  {MAX_POSITIONS}")
logger.info(f"Rebalance every {REBALANCE_FREQUENCY_DAYS} days")
logger.info(f"Lookback:       {MOMENTUM_LOOKBACK_DAYS} days")
logger.info("=" * 60)

trading_robot = PyRobot(
    api_key=API_KEY,
    app_secret=APP_SECRET,
    callback_url=CALLBACK_URL,
    token_path=TOKEN_PATH,
    trading_account=ACCOUNT_NUMBER,
    paper_trading=True
)

backtester = Backtester(
    schwab_client=trading_robot.session,
    symbols_csv_path=str(NASDAQ_SYMBOLS_PATH),
    budget=BUDGET,
    max_positions=MAX_POSITIONS,
    momentum_lookback_days=MOMENTUM_LOOKBACK_DAYS,
    rebalance_frequency_days=REBALANCE_FREQUENCY_DAYS
)

results = backtester.run(
    start_date=BACKTEST_START,
    end_date=BACKTEST_END
)

# ── Print results ──────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("BACKTEST RESULTS")
print("=" * 60)
print(f"Period:            {BACKTEST_START.date()} → {BACKTEST_END.date()}")
print(f"Starting value:    ${results['starting_value']:>10,.2f}")
print(f"Final value:       ${results['final_value']:>10,.2f}")
print(f"Total return:      {results['total_return_pct']:>+10.2f}%")
print(f"Annualized return: {results['annualized_return_pct']:>+10.2f}%")
print(f"Periods simulated: {results['num_periods']}")
print("=" * 60)

print("\nPer-period breakdown:")
for period in results['periods']:
    arrow = "\033[92m▲\033[0m" if period['return_pct'] >= 0 else "\033[91m▼\033[0m"
    print(f"  {period['period_start']} → {period['period_end']}  "
          f"${period['start_value']:>10,.2f} → ${period['end_value']:>10,.2f}  "
          f"{arrow} {period['return_pct']:+.2f}%  "
          f"Holdings: {', '.join(period['holdings'][:5])}{'...' if len(period['holdings']) > 5 else ''}")

print("\nFull results saved to logs/backtest.log")
