"""
Rebalancer — compares current holdings against newly scored candidates and
decides when to sell underperforming positions and redeploy into better ones.

This implements the core "sell 1 stock at peak, buy N cheaper stocks going up"
logic by:
    1. Re-scoring everything currently held
    2. Finding new candidates that score meaningfully better
    3. Generating sell orders for the weakest held positions
    4. Generating buy orders for the best new candidates using the freed capital
"""

import logging
from datetime import datetime
from typing import Dict, List

from pyrobot.scorer import MomentumScorer
from pyrobot.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class Rebalancer:

    def __init__(
        self,
        robot,
        scorer: MomentumScorer,
        position_sizer: PositionSizer,
        rebalance_threshold: float = 0.15,
        stop_loss_pct: float = 0.05
    ) -> None:
        """Initializes the rebalancer.

        Arguments:
        ----
        robot -- The PyRobot instance (provides trading, portfolio, session access).
        scorer {MomentumScorer} -- Scores stocks by momentum.
        position_sizer {PositionSizer} -- Calculates share quantities from budget.
        rebalance_threshold {float} -- A new candidate must score this much better
            (as a fraction) than the worst held stock to trigger a swap.
            e.g. 0.15 means 15% better score required. (default: 0.15)
        stop_loss_pct {float} -- Stop loss percentage applied to new buy orders.
            e.g. 0.05 = 5% below entry price. (default: 0.05)
        """
        self.robot = robot
        self.scorer = scorer
        self.position_sizer = position_sizer
        self.rebalance_threshold = rebalance_threshold
        self.stop_loss_pct = stop_loss_pct

    def get_current_holdings(self) -> Dict[str, dict]:
        """Returns all positions currently tracked in the portfolio.

        Returns:
        ----
        Dict[str, dict] -- Portfolio positions keyed by symbol.
        """
        return self.robot.portfolio.positions

    def score_current_holdings(self) -> Dict[str, float]:
        """Re-scores every currently held stock using fresh price history.

        Returns:
        ----
        Dict[str, float] -- {symbol: composite_score}
        """
        holdings = self.get_current_holdings()
        scores = {}

        logger.info(f"Rebalancer: Re-scoring {len(holdings)} current holdings...")
        for symbol in holdings:
            try:
                scores[symbol] = self.scorer.composite_score(symbol)
                logger.info(f"  {symbol}: score = {scores[symbol]:.3f}")
            except Exception as e:
                logger.warning(f"  {symbol}: scoring failed ({e}), assigning 0.0")
                scores[symbol] = 0.0

        return scores

    def find_replacements(
        self,
        current_scores: Dict[str, float],
        candidate_scores: List[Dict]
    ) -> Dict[str, str]:
        """Identifies which held stocks should be swapped for better candidates.

        For each new candidate, checks if it scores better than the worst
        currently held stock by more than the rebalance_threshold. If so,
        marks that holding for replacement.

        Arguments:
        ----
        current_scores {Dict[str, float]} -- Scores of currently held stocks.
        candidate_scores {List[Dict]} -- Ranked list from scorer.rank_candidates().

        Returns:
        ----
        Dict[str, str] -- {symbol_to_sell: symbol_to_buy}
        """
        replacements = {}
        held_symbols = set(current_scores.keys())

        for candidate in candidate_scores:
            new_symbol = candidate['symbol']
            new_score = candidate['score']

            # Skip stocks we already hold
            if new_symbol in held_symbols:
                continue

            # No holdings left to replace
            if not current_scores:
                break

            worst_held_symbol = min(current_scores, key=current_scores.get)
            worst_held_score = current_scores[worst_held_symbol]

            # Only swap if the new pick is meaningfully better
            required_score = worst_held_score * (1 + self.rebalance_threshold)
            if new_score > required_score:
                logger.info(f"Rebalancer: Replacing {worst_held_symbol} (score: {worst_held_score:.3f}) "
                            f"with {new_symbol} (score: {new_score:.3f})")
                replacements[worst_held_symbol] = new_symbol

                # Update tracking so the same held stock isn't replaced twice
                del current_scores[worst_held_symbol]
                current_scores[new_symbol] = new_score
                held_symbols.discard(worst_held_symbol)
                held_symbols.add(new_symbol)

        return replacements

    def _execute_sell(self, symbol: str, quantity: int) -> dict:
        """Builds and executes (or simulates) a market sell order.

        Arguments:
        ----
        symbol {str} -- Symbol to sell.
        quantity {int} -- Number of shares to sell.

        Returns:
        ----
        dict -- Order response with action details.
        """
        sell_trade = self.robot.create_trade(
            trade_id=f'sell_{symbol}_{int(datetime.now().timestamp())}',
            enter_or_exit='exit',
            long_or_short='long',
            order_type='mkt'
        )
        sell_trade.instrument(symbol=symbol, quantity=quantity, asset_type='EQUITY')
        sell_trade.modify_session(session='normal')

        if not self.robot.paper_trading:
            return self.robot.execute_orders(trade_obj=sell_trade)
        else:
            return {
                'action': 'SELL',
                'symbol': symbol,
                'quantity': quantity,
                'order_id': f'paper_sell_{symbol}_{int(datetime.now().timestamp())}',
                'timestamp': datetime.now().isoformat()
            }

    def _execute_buy(self, symbol: str, shares: int, price: float) -> dict:
        """Builds and executes (or simulates) a limit buy order with a stop loss.

        Arguments:
        ----
        symbol {str} -- Symbol to buy.
        shares {int} -- Number of shares to buy.
        price {float} -- Current market price used as the limit price.

        Returns:
        ----
        dict -- Order response with action details.
        """
        buy_trade = self.robot.create_trade(
            trade_id=f'buy_{symbol}_{int(datetime.now().timestamp())}',
            enter_or_exit='enter',
            long_or_short='long',
            order_type='lmt',
            price=price
        )
        buy_trade.instrument(symbol=symbol, quantity=shares, asset_type='EQUITY')
        buy_trade.modify_session(session='normal')
        buy_trade.add_stop_loss(stop_size=self.stop_loss_pct, percentage=True)

        if not self.robot.paper_trading:
            return self.robot.execute_orders(trade_obj=buy_trade)
        else:
            return {
                'action': 'BUY',
                'symbol': symbol,
                'quantity': shares,
                'price': price,
                'total_cost': shares * price,
                'order_id': f'paper_buy_{symbol}_{int(datetime.now().timestamp())}',
                'timestamp': datetime.now().isoformat()
            }

    def execute_rebalance(
        self,
        replacements: Dict[str, str],
        buy_scores: Dict[str, float] = None
    ) -> List[dict]:
        """Executes all sell and buy orders for the planned replacements.

        Sells each exiting position at market, calculates freed capital,
        then buys the replacement stocks. If buy_scores are provided and the
        position_sizer is configured for score_weighted mode, capital is
        allocated proportionally to score rather than equally.

        Arguments:
        ----
        replacements {Dict[str, str]} -- {symbol_to_sell: symbol_to_buy}
        buy_scores {Dict[str, float]} -- Optional composite scores for the buy
            symbols, used for score-weighted allocation. (default: None)

        Returns:
        ----
        List[dict] -- All order responses (sells and buys).
        """
        order_responses = []
        holdings = self.get_current_holdings()

        # Guard: filter out any buy targets already in the portfolio
        # (can happen if saved state is restored and the same stock is still top-ranked)
        buy_symbols = []
        for buy_symbol in replacements.values():
            if buy_symbol in holdings:
                logger.warning(
                    f"Rebalancer: Skipping buy of {buy_symbol} — already in portfolio. "
                    f"State restored from disk may have caused a duplicate."
                )
            else:
                buy_symbols.append(buy_symbol)

        if not buy_symbols:
            logger.info("Rebalancer: All replacement buys skipped (already held). No orders placed.")
            return []

        # Estimate freed capital from the sells
        sell_prices = self.position_sizer.get_current_prices(list(replacements.keys()))
        freed_capital = sum(
            holdings.get(sym, {}).get('quantity', 0) * sell_prices.get(sym, 0)
            for sym in replacements.keys()
        )

        logger.info(f"Rebalancer: Freed capital from sells: ${freed_capital:.2f}")

        # Execute sells
        for sell_symbol in replacements.keys():
            quantity = holdings.get(sell_symbol, {}).get('quantity', 0)
            if quantity > 0:
                response = self._execute_sell(sell_symbol, quantity)
                order_responses.append(response)
                self.robot.portfolio.remove_position(symbol=sell_symbol)
                self._print_order(response)

        # Calculate buy allocations with the freed capital, passing scores if available
        buy_allocations = self.position_sizer.get_allocations(
            buy_symbols,
            freed_capital,
            scores=buy_scores
        )

        # Execute buys
        for buy_symbol, allocation in buy_allocations.items():
            response = self._execute_buy(
                symbol=buy_symbol,
                shares=allocation['shares'],
                price=allocation['price']
            )
            order_responses.append(response)
            self.robot.portfolio.add_position(
                symbol=buy_symbol,
                asset_type='equity',
                quantity=allocation['shares'],
                purchase_price=allocation['price'],
                purchase_date=datetime.now().strftime('%Y-%m-%d')
            )
            self._print_order(response)

        return order_responses

    def rebalance(self, candidate_symbols: List[str]) -> List[dict]:
        """Full rebalance pipeline: score holdings → find swaps → execute.

        Arguments:
        ----
        candidate_symbols {List[str]} -- Fresh candidate list from the scanner.

        Returns:
        ----
        List[dict] -- All order responses. Empty list if no rebalance needed.
        """
        if not self.get_current_holdings():
            logger.info("Rebalancer: No current holdings to rebalance.")
            return []

        # Clear cache so we use fresh price data for this rebalance run
        self.scorer.clear_cache()

        current_scores = self.score_current_holdings()
        candidate_scores = self.scorer.rank_candidates(candidate_symbols)
        replacements = self.find_replacements(dict(current_scores), candidate_scores)

        if not replacements:
            logger.info("Rebalancer: No replacements needed — current holdings are still top performers.")
            return []

        # Build a scores dict for the buy symbols to enable score-weighted allocation
        candidate_scores_dict = {item['symbol']: item['score'] for item in candidate_scores}
        buy_scores = {
            buy_sym: candidate_scores_dict.get(buy_sym, 0.0)
            for buy_sym in replacements.values()
        }

        logger.info(f"Rebalancer: Executing {len(replacements)} replacement(s)...")
        return self.execute_rebalance(replacements, buy_scores=buy_scores)

    def _print_order(self, response: dict) -> None:
        """Prints a colored summary line for a completed order."""
        action = response.get('action', '')
        symbol = response.get('symbol', '')
        qty = response.get('quantity', '')

        if action == 'BUY':
            price = response.get('price', 0)
            cost = response.get('total_cost', 0)
            logger.info(f"\033[92m  BUY  {symbol}: {qty} shares @ ${price:.2f} = ${cost:.2f}\033[0m")
        elif action == 'SELL':
            logger.info(f"\033[91m  SELL {symbol}: {qty} shares\033[0m")
