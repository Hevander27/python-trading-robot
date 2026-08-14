"""
Backtester — simulates the dynamic momentum strategy on historical data.

How it works:
    1. Loads NASDAQ symbols from the CSV (same file used by the live bot)
    2. For each rebalance period between start_date and end_date:
        a. Scores all candidate symbols using price history UP TO that date
           (not future data — no lookahead bias)
        b. Picks the top N stocks
        c. Records the portfolio value at the start and end of the holding period
    3. Accumulates portfolio value and reports total return vs. SPY benchmark

Usage (from samples/backtest.py):
    backtester = Backtester(
        schwab_client=robot.session,
        symbols_csv_path='data/nasdaq_symbols.csv',
        budget=5000,
        max_positions=10,
        momentum_lookback_days=252,
        rebalance_frequency_days=30
    )
    results = backtester.run(
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 12, 31)
    )
"""

import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict

from pyrobot.scorer import MomentumScorer

logger = logging.getLogger(__name__)


class Backtester:

    def __init__(
        self,
        schwab_client,
        symbols_csv_path: str,
        budget: float,
        max_positions: int,
        momentum_lookback_days: int = 252,
        rebalance_frequency_days: int = 30
    ) -> None:
        """Initializes the backtester.

        Arguments:
        ----
        schwab_client -- Authenticated Schwab API client.
        symbols_csv_path {str} -- Path to the NASDAQ screener CSV.
        budget {float} -- Starting capital.
        max_positions {int} -- Maximum stocks to hold at once.
        momentum_lookback_days {int} -- Days of history used for scoring. (default: 252)
        rebalance_frequency_days {int} -- How often to rebalance the simulated
            portfolio. (default: 30)
        """
        self.schwab_client = schwab_client
        self.symbols_csv_path = symbols_csv_path
        self.budget = budget
        self.max_positions = max_positions
        self.rebalance_frequency_days = rebalance_frequency_days

        self.scorer = MomentumScorer(
            schwab_client=schwab_client,
            momentum_lookback_days=momentum_lookback_days
        )

    def _load_symbols(self) -> List[str]:
        """Loads tradeable symbols from the NASDAQ CSV."""
        df = pd.read_csv(self.symbols_csv_path)
        symbol_column = 'Symbol' if 'Symbol' in df.columns else df.columns[0]
        symbols = df[symbol_column].dropna().astype(str).str.strip().tolist()
        return [s for s in symbols if s.isalpha() and len(s) <= 5]

    def _get_price_as_of(self, symbol: str, as_of_date: datetime) -> float:
        """Returns the closing price on or just before as_of_date."""
        prices = self.scorer.fetch_price_history(symbol, as_of_date=as_of_date)
        if prices.empty:
            return 0.0
        return float(prices.iloc[-1])

    def _score_candidates_as_of(self, symbols: List[str], as_of_date: datetime) -> List[str]:
        """Scores all symbols using price history up to as_of_date and returns top N.

        Arguments:
        ----
        symbols {List[str]} -- Candidate symbols to score.
        as_of_date {datetime} -- Score as if today were this date.

        Returns:
        ----
        List[str] -- Top N symbols by composite score.
        """
        self.scorer.clear_cache()
        scored = []

        logger.info(f"Backtester: Scoring {len(symbols)} candidates as of {as_of_date.date()}...")
        for symbol in symbols:
            try:
                score = self.scorer.composite_score(symbol, as_of_date=as_of_date)
                if score > 0:
                    scored.append({'symbol': symbol, 'score': score})
            except Exception as e:
                logger.debug(f"Backtester: Skipping {symbol}: {e}")

        ranked = sorted(scored, key=lambda x: x['score'], reverse=True)
        top_picks = [item['symbol'] for item in ranked[:self.max_positions]]
        logger.info(f"Backtester: Top picks as of {as_of_date.date()}: {top_picks}")
        return top_picks

    def run(self, start_date: datetime, end_date: datetime) -> Dict:
        """Runs the full backtest simulation.

        Simulates equal-weight rebalancing every rebalance_frequency_days.
        At each rebalance date, the portfolio is re-scored and switched to
        the new top picks. Returns are calculated period by period.

        Arguments:
        ----
        start_date {datetime} -- Simulation start date.
        end_date {datetime} -- Simulation end date.

        Returns:
        ----
        dict -- {
            'periods': list of per-period results,
            'total_return_pct': float,
            'final_value': float,
            'starting_value': float,
            'annualized_return_pct': float
        }
        """
        logger.info(f"Backtester: Running simulation from {start_date.date()} to {end_date.date()}")
        logger.info(f"  Budget: ${self.budget:,.2f} | Max positions: {self.max_positions} | "
                    f"Rebalance every {self.rebalance_frequency_days} days")

        all_symbols = self._load_symbols()
        logger.info(f"Backtester: Loaded {len(all_symbols)} symbols from CSV.")

        portfolio_value = self.budget
        periods = []
        current_date = start_date

        while current_date < end_date:
            period_end = min(current_date + timedelta(days=self.rebalance_frequency_days), end_date)

            # Score and pick stocks as of the period start date
            top_picks = self._score_candidates_as_of(all_symbols, current_date)

            if not top_picks:
                logger.warning(f"Backtester: No picks for period starting {current_date.date()}. Skipping.")
                current_date = period_end
                continue

            # Get entry prices (price at start of period)
            per_position_budget = portfolio_value / len(top_picks)
            entry_prices = {}
            shares = {}
            for symbol in top_picks:
                price = self._get_price_as_of(symbol, current_date)
                if price > 0:
                    entry_prices[symbol] = price
                    shares[symbol] = per_position_budget // price  # whole shares only

            if not entry_prices:
                logger.warning(f"Backtester: Could not fetch prices for period {current_date.date()}.")
                current_date = period_end
                continue

            # Get exit prices (price at end of period)
            period_value = 0.0
            position_results = []
            for symbol in entry_prices:
                exit_price = self._get_price_as_of(symbol, period_end)
                if exit_price <= 0:
                    exit_price = entry_prices[symbol]  # assume no change if missing

                position_cost = shares[symbol] * entry_prices[symbol]
                position_value = shares[symbol] * exit_price
                position_return = (exit_price - entry_prices[symbol]) / entry_prices[symbol] * 100

                period_value += position_value
                position_results.append({
                    'symbol': symbol,
                    'shares': shares[symbol],
                    'entry_price': entry_prices[symbol],
                    'exit_price': exit_price,
                    'return_pct': round(position_return, 2)
                })

            # Add back any uninvested cash (positions we couldn't price)
            invested = sum(shares[s] * entry_prices[s] for s in entry_prices)
            cash_remaining = portfolio_value - invested
            period_value += cash_remaining

            period_return_pct = (period_value - portfolio_value) / portfolio_value * 100

            periods.append({
                'period_start': current_date.strftime('%Y-%m-%d'),
                'period_end': period_end.strftime('%Y-%m-%d'),
                'start_value': round(portfolio_value, 2),
                'end_value': round(period_value, 2),
                'return_pct': round(period_return_pct, 2),
                'holdings': top_picks,
                'positions': position_results
            })

            logger.info(
                f"  Period {current_date.date()} → {period_end.date()}: "
                f"${portfolio_value:,.2f} → ${period_value:,.2f} "
                f"({period_return_pct:+.2f}%)"
            )

            portfolio_value = period_value
            current_date = period_end

        total_return_pct = (portfolio_value - self.budget) / self.budget * 100
        days_elapsed = (end_date - start_date).days
        years = days_elapsed / 365.25
        annualized = ((portfolio_value / self.budget) ** (1 / years) - 1) * 100 if years > 0 else 0.0

        results = {
            'periods': periods,
            'starting_value': self.budget,
            'final_value': round(portfolio_value, 2),
            'total_return_pct': round(total_return_pct, 2),
            'annualized_return_pct': round(annualized, 2),
            'num_periods': len(periods)
        }

        logger.info("=" * 60)
        logger.info("Backtest complete")
        logger.info(f"  Start: ${self.budget:,.2f}  →  End: ${portfolio_value:,.2f}")
        logger.info(f"  Total return:     {total_return_pct:+.2f}%")
        logger.info(f"  Annualized return: {annualized:+.2f}%")
        logger.info("=" * 60)

        return results
