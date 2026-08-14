"""
Dynamic Momentum Strategy — main entry point for the automated trading system.

Startup flow:
    - If data/portfolio_state.json exists: restore previous positions and resume
    - Otherwise: scan NASDAQ → score → size → buy initial positions

Main loop (runs every minute during market hours):
    - Checks intraday RSI on current holdings for early exits (overbought > 70)
    - Every rebalance_frequency_days: full re-scan, re-score, and swap underperformers
    - Saves portfolio state to disk after every trade and rebalance

State is persisted to data/portfolio_state.json so the bot resumes exactly
where it left off after a restart.

To run:
    cd python-trading-robot
    python samples/dynamic_strategy.py

Requirements:
    - data/nasdaq_symbols.csv must exist (download from nasdaq.com/market-activity/stocks/screener)
    - config/config.ini must have both [main] and [strategy] sections filled in
"""

import json
import os
import logging
import time as time_lib
import pprint
import operator
import pathlib
from datetime import datetime, timedelta
from configparser import ConfigParser

from pyrobot.robot import PyRobot
from pyrobot.indicators import Indicators
from pyrobot.scanner import NasdaqScanner
from pyrobot.scorer import MomentumScorer
from pyrobot.position_sizer import PositionSizer
from pyrobot.rebalancer import Rebalancer
from pyrobot.regime_filter import RegimeFilter


# ── Logging ────────────────────────────────────────────────────────────────────

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('logs/trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress httpx's per-request INFO logs (200 OK spam) — only show warnings/errors
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


# ── Configuration ─────────────────────────────────────────────────────────────

config = ConfigParser()
config.read('config/config.ini')

API_KEY = config.get('main', 'api_key')
APP_SECRET = config.get('main', 'app_secret')
CALLBACK_URL = config.get('main', 'callback_url')
TOKEN_PATH = config.get('main', 'token_path')
ACCOUNT_NUMBER = config.get('main', 'account_number')

BUDGET = config.getfloat('strategy', 'budget')
MAX_POSITIONS = config.getint('strategy', 'max_positions')
REBALANCE_THRESHOLD = config.getfloat('strategy', 'rebalance_threshold')
MIN_VOLUME = config.getint('strategy', 'min_volume')
MOMENTUM_LOOKBACK_DAYS = config.getint('strategy', 'momentum_lookback_days')
REBALANCE_FREQUENCY_DAYS = config.getint('strategy', 'rebalance_frequency_days')
STOP_LOSS_PCT = config.getfloat('strategy', 'stop_loss_pct')
PRE_FILTER_MOMENTUM = config.getboolean('strategy', 'pre_filter_momentum')
MIN_NET_CHANGE_PCT = config.getfloat('strategy', 'min_net_change_pct')
SCORE_WEIGHT_MOMENTUM = config.getfloat('strategy', 'score_weight_momentum', fallback=0.6)
SCORE_WEIGHT_EMA = config.getfloat('strategy', 'score_weight_ema', fallback=0.3)
SCORE_WEIGHT_RSI = config.getfloat('strategy', 'score_weight_rsi', fallback=0.1)
ALLOCATION_MODE = config.get('strategy', 'allocation_mode', fallback='equal_weight')
SHOW_STOCK_FRAME = config.getboolean('strategy', 'show_stock_frame', fallback=False)

UNIVERSE_PATH = pathlib.Path(config.get('universe', 'universe_path', fallback='data/universe.csv'))
NASDAQ_SYMBOLS_PATH = pathlib.Path('data/nasdaq_symbols.csv')
STATE_PATH = pathlib.Path('data/portfolio_state.json')

REGIME_ENABLED = config.getboolean('regime', 'enabled', fallback=True)
BEAR_STOP_LOSS_PCT = config.getfloat('regime', 'bear_stop_loss_pct', fallback=0.03)
CASH_ETF = config.get('regime', 'cash_etf', fallback='').strip()

MAX_POSITIONS_PER_SECTOR = config.getint('sector', 'max_positions_per_sector', fallback=3)


# ── Validation ─────────────────────────────────────────────────────────────────

if not UNIVERSE_PATH.exists() and not NASDAQ_SYMBOLS_PATH.exists():
    raise FileNotFoundError(
        "No symbol file found. Either:\n"
        "  1. Run 'python scripts/build_universe.py' to build data/universe.csv, or\n"
        "  2. Download NASDAQ screener CSV from https://www.nasdaq.com/market-activity/stocks/screener\n"
        "     and save it to data/nasdaq_symbols.csv"
    )


# ── Sector concentration cap helper ───────────────────────────────────────────

def apply_sector_cap(
    ranked: list,
    n: int,
    sector_cap: int,
    existing_positions: dict = None
) -> list:
    """Filters ranked picks to enforce a maximum number of positions per sector.

    Accounts for stocks already held so the cap applies to the total portfolio,
    not just the new picks being added.

    Arguments:
    ----
    ranked {list} -- Scored and ranked list of {'symbol', 'score'} dicts.
    n {int} -- Maximum total picks to return.
    sector_cap {int} -- Max positions per sector. 0 = disabled.
    existing_positions {dict} -- Current portfolio positions (may be None).

    Returns:
    ----
    list -- Up to n symbols respecting the sector cap.
    """
    if sector_cap <= 0:
        return [item['symbol'] for item in ranked[:n]]

    sector_counts = {}
    if existing_positions:
        for symbol in existing_positions:
            sector = scanner.get_symbol_sector(symbol)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    result = []
    for item in ranked:
        sector = scanner.get_symbol_sector(item['symbol'])
        if sector_counts.get(sector, 0) < sector_cap:
            result.append(item['symbol'])
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(result) >= n:
            break

    return result


# ── State persistence helpers ──────────────────────────────────────────────────

def save_state(portfolio, last_rebalance_date: datetime) -> None:
    """Saves current portfolio positions and rebalance date to disk.

    Called after every buy, sell, and rebalance so that a restart resumes
    from the exact same state rather than starting fresh.
    """
    state = {
        'last_updated': datetime.now().isoformat(),
        'last_rebalance_date': last_rebalance_date.isoformat(),
        'budget': BUDGET,
        'positions': portfolio.positions
    }
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    print(f"State saved to {STATE_PATH}")


def load_state() -> dict:
    """Loads saved portfolio state from disk.

    Returns:
    ----
    dict -- The saved state, or empty dict if no state file exists.
    """
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception as e:
        print(f"Warning: Could not load state file ({e}). Starting fresh.")
        return {}


# ── Initialise PyRobot ─────────────────────────────────────────────────────────

print("=" * 60)
print("Dynamic Momentum Strategy")
print("=" * 60)
print(f"Budget:         ${BUDGET:,.2f}")
print(f"Max positions:  {MAX_POSITIONS}")
print(f"Rebalance every {REBALANCE_FREQUENCY_DAYS} days if a pick scores {REBALANCE_THRESHOLD*100:.0f}%+ better")
print(f"Stop loss:      {STOP_LOSS_PCT*100:.0f}%")
print(f"Paper trading:  True")
print("=" * 60)

trading_robot = PyRobot(
    api_key=API_KEY,
    app_secret=APP_SECRET,
    callback_url=CALLBACK_URL,
    token_path=TOKEN_PATH,
    trading_account=ACCOUNT_NUMBER,
    paper_trading=True
)

trading_robot_portfolio = trading_robot.create_portfolio()


# ── Initialise strategy components ────────────────────────────────────────────

scanner = NasdaqScanner(
    schwab_client=trading_robot.session,
    symbols_csv_path=str(UNIVERSE_PATH),
    budget=BUDGET,
    max_positions=MAX_POSITIONS,
    min_volume=MIN_VOLUME,
    pre_filter_momentum=PRE_FILTER_MOMENTUM,
    min_net_change_pct=MIN_NET_CHANGE_PCT
)

scorer = MomentumScorer(
    schwab_client=trading_robot.session,
    momentum_lookback_days=MOMENTUM_LOOKBACK_DAYS,
    weight_momentum=SCORE_WEIGHT_MOMENTUM,
    weight_ema=SCORE_WEIGHT_EMA,
    weight_rsi=SCORE_WEIGHT_RSI
)

position_sizer = PositionSizer(
    schwab_client=trading_robot.session,
    allocation_mode=ALLOCATION_MODE
)

rebalancer = Rebalancer(
    robot=trading_robot,
    scorer=scorer,
    position_sizer=position_sizer,
    rebalance_threshold=REBALANCE_THRESHOLD,
    stop_loss_pct=STOP_LOSS_PCT
)

regime_filter = RegimeFilter(schwab_client=trading_robot.session)


# ── Startup: restore state or run fresh scan ───────────────────────────────────

saved_state = load_state()

if saved_state.get('positions'):
    # Resume from saved state — restore all previous positions
    print(f"\nResuming from saved state ({saved_state.get('last_updated', 'unknown time')})")
    for symbol, position in saved_state['positions'].items():
        trading_robot_portfolio.add_position(
            symbol=symbol,
            asset_type=position.get('asset_type', 'equity'),
            quantity=int(position.get('quantity', 0)),
            purchase_price=float(position.get('purchase_price', 0)),
            purchase_date=position.get('purchase_date', datetime.now().strftime('%Y-%m-%d'))
        )

    last_rebalance_date = datetime.fromisoformat(
        saved_state.get('last_rebalance_date', datetime.now().isoformat())
    )
    print(f"Restored {len(saved_state['positions'])} positions.")
    pprint.pprint(trading_robot_portfolio.positions)

else:
    # Fresh start — scan, score, and buy initial positions
    logger.info("No saved state found. Running fresh startup scan...")

    # Wait for market open — scanner needs live volume data which is 0 when closed
    if not trading_robot.regular_market_open:
        logger.info("Market is currently closed. Waiting for open before running startup scan...")
        while not trading_robot.regular_market_open:
            time_lib.sleep(60)
        logger.info("Market is now open. Running startup scan.")

    # Use tighter stop loss if in bear market, but always buy initial positions
    # (regime filter only blocks rebalancing into new stocks, not the initial entry)
    if REGIME_ENABLED:
        regime = regime_filter.get_regime(use_cache=False)
        active_stop_loss = BEAR_STOP_LOSS_PCT if not regime['is_bull'] else STOP_LOSS_PCT
        if not regime['is_bull']:
            logger.warning(
                "Bear market detected — using tighter stop loss "
                f"({active_stop_loss * 100:.0f}%) for initial positions."
            )
    else:
        active_stop_loss = STOP_LOSS_PCT

    candidate_symbols = scanner.get_candidate_symbols()
    if not candidate_symbols:
        raise RuntimeError(
            "Scanner returned no candidates. "
            "Check universe.csv (or nasdaq_symbols.csv) and API connectivity."
        )

    logger.info(f"Startup: Scoring {len(candidate_symbols)} candidates...")
    scorer.clear_cache()
    ranked = scorer.rank_candidates(candidate_symbols)
    top_picks = apply_sector_cap(ranked, MAX_POSITIONS, MAX_POSITIONS_PER_SECTOR)
    top_scores = {item['symbol']: item['score'] for item in ranked if item['symbol'] in top_picks}

    logger.info(f"Startup: Top picks after sector cap: {top_picks}")
    allocations = position_sizer.get_allocations(top_picks, BUDGET, scores=top_scores)

    logger.info("Startup: Buying initial positions...")
    for symbol, allocation in allocations.items():
        buy_trade = trading_robot.create_trade(
            trade_id=f'buy_{symbol}_init',
            enter_or_exit='enter',
            long_or_short='long',
            order_type='lmt',
            price=allocation['price']
        )
        buy_trade.instrument(symbol=symbol, quantity=allocation['shares'], asset_type='EQUITY')
        buy_trade.modify_session(session='normal')
        buy_trade.add_stop_loss(stop_size=active_stop_loss, percentage=True)

        if not trading_robot.paper_trading:
            trading_robot.execute_orders(trade_obj=buy_trade)

        trading_robot_portfolio.add_position(
            symbol=symbol,
            asset_type='equity',
            quantity=allocation['shares'],
            purchase_price=allocation['price'],
            purchase_date=datetime.now().strftime('%Y-%m-%d')
        )

        logger.info(
            f"\033[92m  BUY {symbol}: {allocation['shares']} shares "
            f"@ ${allocation['price']:.2f} = ${allocation['total_cost']:.2f}\033[0m"
        )

    last_rebalance_date = datetime.now()
    logger.info(f"Portfolio initialised with {len(allocations)} positions.")
    pprint.pprint(trading_robot_portfolio.positions)

        # Save state immediately after initial buys
    save_state(trading_robot_portfolio, last_rebalance_date)


# ── Set up intraday indicators for RSI-based exits ────────────────────────────

active_symbols = list(trading_robot_portfolio.positions.keys())
stock_frame = None
indicator_client = None
trades_dict = {}

if active_symbols:
    start_date = datetime.today()
    end_date = start_date - timedelta(days=30)

    historical_prices = trading_robot.grab_historical_prices(
        start=end_date,
        end=start_date,
        bar_size=1,
        bar_type='minute'
    )

    stock_frame = trading_robot.create_stock_frame(data=historical_prices['aggregated'])
    trading_robot.portfolio.stock_frame = stock_frame
    trading_robot.portfolio.historical_prices = historical_prices

    indicator_client = Indicators(price_data_frame=stock_frame)
    indicator_client.rsi(period=14)
    indicator_client.ema(period=50)

    indicator_client.set_indicator_signal(
        indicator='rsi',
        buy=30.0,
        sell=70.0,
        condition_buy=operator.le,
        condition_sell=operator.ge
    )

    for symbol in active_symbols:
        position = trading_robot_portfolio.positions.get(symbol, {})
        current_price = position.get('purchase_price', 0)
        quantity = position.get('quantity', 1)

        intraday_trade = trading_robot.create_trade(
            trade_id=f'intraday_{symbol}',
            enter_or_exit='enter',
            long_or_short='long',
            order_type='lmt',
            price=current_price
        )
        intraday_trade.instrument(symbol=symbol, quantity=quantity, asset_type='EQUITY')
        intraday_trade.modify_session(session='normal')
        intraday_trade.add_stop_loss(stop_size=STOP_LOSS_PCT, percentage=True)

        trades_dict[symbol] = {
            'buy': {
                'trade_func': trading_robot.trades[f'intraday_{symbol}'],
                'trade_id': f'intraday_{symbol}'
            },
            'sell': {
                'trade_func': trading_robot.trades[f'intraday_{symbol}'],
                'trade_id': f'intraday_{symbol}'
            }
        }


# ── Main loop ─────────────────────────────────────────────────────────────────

logger.info("Entering main trading loop. Press Ctrl+C to stop.")

while True:
  try:

    # Only trade during regular market hours
    if not trading_robot.regular_market_open:
        print("Market closed — sleeping 60 seconds...")
        time_lib.sleep(60)
        continue

    # Grab the latest 1-minute bar for all held symbols (skip if no positions yet)
    if stock_frame is not None and indicator_client is not None:
        latest_bars = trading_robot.get_latest_bar()
        stock_frame.add_rows(data=latest_bars)
        indicator_client.refresh()

        if SHOW_STOCK_FRAME:
            print("=" * 60)
            print(f"StockFrame — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 60)
            print(stock_frame.symbol_groups.tail())
            print("-" * 60)

    # Update each trade's limit price to the current market price
    current_quotes = trading_robot.grab_current_quotes()
    for symbol in list(trading_robot.trades.keys()):
        if symbol in current_quotes and 'quote' in current_quotes[symbol]:
            live_price = current_quotes[symbol]['quote']['lastPrice']
            trading_robot.trades[symbol].modify_price(
                new_price=live_price,
                price_type='limit-price'
            )

    # ── Software stop loss (paper trading fallback) ────────────────────────────
    # Schwab doesn't fill stop orders in paper mode, so we monitor manually.
    for symbol in list(trading_robot_portfolio.positions.keys()):
        position = trading_robot_portfolio.positions[symbol]
        purchase_price = float(position.get('purchase_price', 0))
        current_price = current_quotes.get(symbol, {}).get('quote', {}).get('lastPrice', 0)

        if purchase_price > 0 and current_price > 0:
            loss_pct = (purchase_price - current_price) / purchase_price
            if loss_pct >= STOP_LOSS_PCT:
                logger.warning(
                    f"STOP LOSS triggered: {symbol} down {loss_pct * 100:.1f}% "
                    f"(bought ${purchase_price:.2f}, now ${current_price:.2f})"
                )
                quantity = int(position.get('quantity', 0))
                if quantity > 0:
                    stop_trade = trading_robot.create_trade(
                        trade_id=f'stop_{symbol}_{int(time_lib.time())}',
                        enter_or_exit='exit',
                        long_or_short='long',
                        order_type='mkt'
                    )
                    stop_trade.instrument(symbol=symbol, quantity=quantity, asset_type='EQUITY')
                    stop_trade.modify_session(session='normal')

                    if not trading_robot.paper_trading:
                        trading_robot.execute_orders(trade_obj=stop_trade)

                    trading_robot_portfolio.remove_position(symbol=symbol)
                    trades_dict.pop(symbol, None)
                    logger.info(f"STOP LOSS executed: sold {quantity} shares of {symbol} at ~${current_price:.2f}")
                    save_state(trading_robot_portfolio, last_rebalance_date)

    # ── Unrealized P&L summary ─────────────────────────────────────────────────
    if trading_robot_portfolio.positions:
        price_map = {
            sym: current_quotes.get(sym, {}).get('quote', {}).get('lastPrice', 0)
            for sym in trading_robot_portfolio.positions
        }
        pnl = trading_robot_portfolio.get_unrealized_pnl(price_map)
        total = pnl['_total']

        W = 76
        print(f"\n{'─' * W}")
        print(f"  {'SYMBOL':<8} {'QTY':>5}  {'BOUGHT':>8}  {'NOW':>8}  {'VALUE':>10}  {'P&L':>10}  {'%':>7}")
        print(f"{'─' * W}")

        for sym, pos in pnl.items():
            if sym == '_total':
                continue
            gain  = pos['unrealized_pnl']
            pct   = pos['pct_change']
            color = '\033[92m' if gain >= 0 else '\033[91m'
            sign  = '+' if gain >= 0 else ''
            print(
                f"{color}  {sym:<8} {pos['quantity']:>5}  "
                f"${pos['purchase_price']:>7.2f}  ${pos['current_price']:>7.2f}  "
                f"${pos['market_value']:>9,.2f}  "
                f"{sign}${gain:>8.2f}  {sign}{pct:.2f}%\033[0m"
            )

        invested    = total['total_cost']
        value       = total['total_value']
        gain        = total['total_unrealized_pnl']
        pct         = total['total_pct_change']
        cash        = BUDGET - invested
        total_with_cash = value + cash
        pnl_color   = '\033[92m' if gain >= 0 else '\033[91m'
        sign        = '+' if gain >= 0 else ''

        print(f"{'─' * W}")
        print(f"  {'Invested:':<14} ${invested:>10,.2f}")
        print(f"  {'Stock Value:':<14} ${value:>10,.2f}")
        print(f"  {'P&L:':<14} {pnl_color}{sign}${gain:>10,.2f}  ({sign}{pct:.2f}%)\033[0m")
        print(f"  {'Cash:':<14} ${cash:>10,.2f}")
        print(f"  {'Total:':<14} ${total_with_cash:>10,.2f}")
        print(f"{'─' * W}\n")

    # Check intraday RSI signals and execute any triggered trades
    order_responses = []
    if indicator_client is not None and trades_dict:
        signals = indicator_client.check_signals()
        order_responses = trading_robot.execute_signals(
            signals=signals,
            trades_to_execute=trades_dict
        )

    if order_responses:
        for response in order_responses:
            body = response.get('request_body', {})
            leg = body.get('orderLegCollection', [{}])[0]
            instruction = leg.get('instruction', '').upper()
            symbol = leg.get('instrument', {}).get('symbol', '?')
            quantity = leg.get('quantity', '?')
            price = body.get('price', 0)
            order_type = body.get('orderType', '?')
            timestamp = datetime.now().strftime('%H:%M:%S')

            if 'BUY' in instruction:
                logger.info(
                    f"\033[92m▲ BUY  {symbol:<6}  {quantity} shares @ ${price:.2f}"
                    f"  [{order_type}]  {timestamp}\033[0m"
                )
            elif 'SELL' in instruction:
                logger.info(
                    f"\033[91m▼ SELL {symbol:<6}  {quantity} shares @ ${price:.2f}"
                    f"  [{order_type}]  {timestamp}\033[0m"
                )
        # Save state after any intraday trade
        save_state(trading_robot_portfolio, last_rebalance_date)

    # ── Regime check (once per loop) ──────────────────────────────────────────
    if REGIME_ENABLED:
        regime = regime_filter.get_regime()   # cached for the day
        active_stop_loss = STOP_LOSS_PCT if regime['is_bull'] else BEAR_STOP_LOSS_PCT
    else:
        regime = {'is_bull': True}
        active_stop_loss = STOP_LOSS_PCT

    # ── Buy initial positions if we were waiting for bull market ───────────────
    if regime['is_bull'] and not trading_robot_portfolio.positions and stock_frame is None:
        logger.info("Regime turned BULL and no positions held — running startup scan now.")
        candidate_symbols = scanner.get_candidate_symbols()
        if candidate_symbols:
            scorer.clear_cache()
            ranked = scorer.rank_candidates(candidate_symbols)
            top_picks = apply_sector_cap(ranked, MAX_POSITIONS, MAX_POSITIONS_PER_SECTOR)
            top_scores = {item['symbol']: item['score'] for item in ranked if item['symbol'] in top_picks}
            allocations = position_sizer.get_allocations(top_picks, BUDGET, scores=top_scores)

            for symbol, allocation in allocations.items():
                buy_trade = trading_robot.create_trade(
                    trade_id=f'buy_{symbol}_init',
                    enter_or_exit='enter',
                    long_or_short='long',
                    order_type='lmt',
                    price=allocation['price']
                )
                buy_trade.instrument(symbol=symbol, quantity=allocation['shares'], asset_type='EQUITY')
                buy_trade.modify_session(session='normal')
                buy_trade.add_stop_loss(stop_size=active_stop_loss, percentage=True)
                if not trading_robot.paper_trading:
                    trading_robot.execute_orders(trade_obj=buy_trade)
                trading_robot_portfolio.add_position(
                    symbol=symbol,
                    asset_type='equity',
                    quantity=allocation['shares'],
                    purchase_price=allocation['price'],
                    purchase_date=datetime.now().strftime('%Y-%m-%d')
                )
                logger.info(
                    f"\033[92m  BUY {symbol}: {allocation['shares']} shares "
                    f"@ ${allocation['price']:.2f} = ${allocation['total_cost']:.2f}\033[0m"
                )

            # Initialise indicators now that we have positions
            active_symbols = list(trading_robot_portfolio.positions.keys())
            start_date = datetime.today()
            hist_start = start_date - timedelta(days=30)
            historical_prices = trading_robot.grab_historical_prices(
                start=hist_start, end=start_date, bar_size=1, bar_type='minute'
            )
            stock_frame = trading_robot.create_stock_frame(data=historical_prices['aggregated'])
            trading_robot.portfolio.stock_frame = stock_frame
            indicator_client = Indicators(price_data_frame=stock_frame)
            indicator_client.rsi(period=14)
            indicator_client.ema(period=50)
            indicator_client.set_indicator_signal(
                indicator='rsi', buy=30.0, sell=70.0,
                condition_buy=operator.le, condition_sell=operator.ge
            )
            last_rebalance_date = datetime.now()
            save_state(trading_robot_portfolio, last_rebalance_date)

    # Periodic rebalance check — only runs in bull market
    days_since_rebalance = (datetime.now() - last_rebalance_date).days
    if days_since_rebalance >= REBALANCE_FREQUENCY_DAYS:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Rebalance triggered — {days_since_rebalance} days since last rebalance.")

        if not regime['is_bull']:
            logger.warning(
                "Bear market active — skipping rebalance into new positions. "
                f"Stop loss tightened to {active_stop_loss * 100:.0f}%."
            )
            if CASH_ETF:
                logger.info(f"Idle cash held in {CASH_ETF}.")
        else:
            fresh_candidates = scanner.get_candidate_symbols()
            rebalance_responses = rebalancer.rebalance(candidate_symbols=fresh_candidates)
            if rebalance_responses:
                logger.info(f"Rebalance complete — {len(rebalance_responses)} orders executed.")
            else:
                logger.info("Rebalance complete — no changes made.")

        last_rebalance_date = datetime.now()
        save_state(trading_robot_portfolio, last_rebalance_date)

    # Wait until the next 1-minute bar (skip if no stock frame yet)
    if stock_frame is not None:
        last_bar_timestamp = trading_robot.stock_frame.frame.tail(n=1).index.get_level_values(1)
        trading_robot.wait_till_next_bar(last_bar_timestamp=last_bar_timestamp)
    else:
        logger.info("No positions — checking again in 60 seconds...")
        time_lib.sleep(60)

  except KeyboardInterrupt:
    logger.info("Keyboard interrupt received. Saving state and shutting down.")
    save_state(trading_robot_portfolio, last_rebalance_date)
    break

  except Exception as e:
    error_str = str(e).lower()
    auth_keywords = ['token', 'unauthorized', 'unauthenticated', '401', 'refresh', 'expired', 'invalid_grant']
    if any(keyword in error_str for keyword in auth_keywords):
        logger.critical(
            "Authentication error — your Schwab refresh token has likely expired (tokens last 7 days). "
            "Re-run the bot to trigger re-authentication: python samples/dynamic_strategy.py\n"
            f"Original error: {e}"
        )
        save_state(trading_robot_portfolio, last_rebalance_date)
        break
    else:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
        logger.info("Sleeping 60s before retrying...")
        time_lib.sleep(60)
