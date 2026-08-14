"""
PositionSizer — translates a budget and a ranked list of stocks into concrete
share quantities for each position.

Supports two allocation modes (set in config.ini under allocation_mode):
    equal_weight  — each stock gets budget / num_positions dollars (default)
    score_weighted — each stock gets budget × (score / total_scores) dollars,
                     so higher-scoring stocks receive proportionally more capital
"""

import math
import logging
from typing import Dict, List, Optional

from pyrobot.utils import retry_with_backoff

logger = logging.getLogger(__name__)


class PositionSizer:

    def __init__(self, schwab_client, allocation_mode: str = 'equal_weight') -> None:
        """Initializes the sizer with a Schwab client for fetching current prices.

        Arguments:
        ----
        schwab_client -- Authenticated Schwab API client (robot.session).
        allocation_mode {str} -- 'equal_weight' divides budget evenly across all
            positions. 'score_weighted' allocates proportionally to each stock's
            composite score — higher-scoring picks receive more capital.
            (default: 'equal_weight')
        """
        self.schwab_client = schwab_client
        self.allocation_mode = allocation_mode

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def get_current_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Fetches the latest ask price for each symbol.

        Arguments:
        ----
        symbols {List[str]} -- Symbols to price.

        Returns:
        ----
        Dict[str, float] -- {symbol: last_price}
        """
        if not symbols:
            return {}

        try:
            response = self.schwab_client.get_quotes(symbols).json()
        except Exception as e:
            logger.error(f"PositionSizer: Failed to fetch prices: {e}")
            return {}

        prices = {}
        for symbol, data in response.items():
            if isinstance(data, dict) and 'quote' in data:
                prices[symbol] = data['quote'].get('lastPrice', 0)

        return prices

    def calculate_per_position_budget(self, total_budget: float, num_positions: int) -> float:
        """Divides total budget equally across all positions.

        Arguments:
        ----
        total_budget {float} -- Total capital to deploy.
        num_positions {int} -- Number of positions to open.

        Returns:
        ----
        float -- Dollar amount allocated to each position.
        """
        if num_positions <= 0:
            return 0.0
        return total_budget / num_positions

    def calculate_shares(self, per_position_budget: float, current_price: float) -> int:
        """Converts a dollar allocation into a whole number of shares.

        Uses math.floor so we never spend more than the allocated amount.

        Arguments:
        ----
        per_position_budget {float} -- Dollars to spend on this stock.
        current_price {float} -- Current market price per share.

        Returns:
        ----
        int -- Number of whole shares to buy. Returns 0 if price is invalid.
        """
        if current_price <= 0:
            return 0
        return math.floor(per_position_budget / current_price)

    def _budget_per_symbol(self, symbols: List[str], budget: float, scores: Optional[Dict[str, float]]) -> Dict[str, float]:
        """Calculates the dollar allocation for each symbol based on allocation_mode.

        Arguments:
        ----
        symbols {List[str]} -- Symbols to allocate across.
        budget {float} -- Total capital to deploy.
        scores {dict} -- {symbol: composite_score}. Required for score_weighted mode;
            ignored in equal_weight mode.

        Returns:
        ----
        Dict[str, float] -- {symbol: dollar_budget}
        """
        if self.allocation_mode == 'score_weighted' and scores:
            total_score = sum(scores.get(s, 0.0) for s in symbols)
            if total_score <= 0:
                # Fall back to equal weight if all scores are zero
                equal = budget / len(symbols)
                return {s: equal for s in symbols}
            return {s: budget * (scores.get(s, 0.0) / total_score) for s in symbols}
        else:
            equal = budget / len(symbols) if symbols else 0.0
            return {s: equal for s in symbols}

    def get_allocations(
        self,
        symbols: List[str],
        budget: float,
        scores: Optional[Dict[str, float]] = None
    ) -> Dict[str, Dict]:
        """Calculates share quantities and costs for a list of symbols.

        Fetches current prices, divides budget per allocation_mode, and computes shares.
        Symbols where we cannot afford at least 1 share are skipped.

        Arguments:
        ----
        symbols {List[str]} -- Stocks to allocate budget across.
        budget {float} -- Total capital available for these positions.
        scores {dict} -- Optional {symbol: composite_score}. When provided and
            allocation_mode is 'score_weighted', higher-scoring stocks receive
            proportionally more capital. (default: None — uses equal_weight)

        Returns:
        ----
        Dict[str, Dict] -- {
            symbol: {
                'shares': int,
                'price': float,
                'total_cost': float,
                'remaining_cash': float
            }
        }
        """
        prices = self.get_current_prices(symbols)
        budget_per_symbol = self._budget_per_symbol(symbols, budget, scores)

        mode_label = 'score-weighted' if (self.allocation_mode == 'score_weighted' and scores) else 'equal-weight'
        logger.info(f"PositionSizer: Allocating ${budget:.2f} across {len(symbols)} symbols ({mode_label}).")

        allocations = {}
        for symbol in symbols:
            price = prices.get(symbol, 0)
            position_budget = budget_per_symbol.get(symbol, 0)
            shares = self.calculate_shares(position_budget, price)

            if shares > 0:
                total_cost = shares * price
                allocations[symbol] = {
                    'shares': shares,
                    'price': price,
                    'total_cost': total_cost,
                    'remaining_cash': position_budget - total_cost
                }
            else:
                logger.warning(f"PositionSizer: Skipping {symbol} — cannot afford 1 share at ${price:.2f} with ${position_budget:.2f} budget.")

        total_deployed = sum(a['total_cost'] for a in allocations.values())
        logger.info(f"PositionSizer: Deploying ${total_deployed:.2f} of ${budget:.2f} across {len(allocations)} positions.")
        return allocations
