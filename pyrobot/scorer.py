"""
MomentumScorer — ranks candidate stocks using a composite score based on
12-month price momentum (Dual Momentum strategy), EMA trend confirmation,
and RSI position to avoid entering overbought stocks.

Scoring weights (tunable):
    60% — 12-month raw return (primary momentum signal)
    30% — EMA trend (price above 50/200 EMA confirms uptrend)
    10% — RSI position (penalizes overbought entries above 70)
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from pyrobot.utils import retry_with_backoff

logger = logging.getLogger(__name__)


class MomentumScorer:

    def __init__(
        self,
        schwab_client,
        momentum_lookback_days: int = 252,
        weight_momentum: float = 0.6,
        weight_ema: float = 0.3,
        weight_rsi: float = 0.1
    ) -> None:
        """Initializes the scorer with a Schwab client for fetching price history.

        Arguments:
        ----
        schwab_client -- Authenticated Schwab API client (robot.session).
        momentum_lookback_days {int} -- Trading days of history to use for scoring.
            252 = approximately 1 year. (default: 252)
        weight_momentum {float} -- Weight for the 12-month return signal. (default: 0.6)
        weight_ema {float} -- Weight for the EMA trend signal. (default: 0.3)
        weight_rsi {float} -- Weight for the RSI position signal. (default: 0.1)
        """
        self.schwab_client = schwab_client
        self.momentum_lookback_days = momentum_lookback_days
        self.weight_momentum = weight_momentum
        self.weight_ema = weight_ema
        self.weight_rsi = weight_rsi

        # Cache price history to avoid re-fetching within the same scoring run
        self._price_history_cache: Dict[str, pd.Series] = {}

    def clear_cache(self) -> None:
        """Clears the price history cache. Call before each new scoring run."""
        self._price_history_cache = {}

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def _fetch_price_history_from_api(self, symbol: str, start_date: datetime, end_date: datetime) -> pd.Series:
        """Makes the actual Schwab API call. Retried on failure."""
        response = self.schwab_client.get_price_history(
            symbol,
            start_datetime=start_date,
            end_datetime=end_date,
            need_extended_hours_data=False
        ).json()

        candles = response.get('candles', [])
        if not candles:
            return pd.Series(dtype=float)

        return pd.Series(
            [c['close'] for c in candles],
            index=pd.to_datetime([c['datetime'] for c in candles], unit='ms')
        )

    def fetch_price_history(self, symbol: str, as_of_date: Optional[datetime] = None) -> pd.Series:
        """Fetches daily closing prices for the momentum lookback period.

        Returns a cached result if already fetched this session.

        Arguments:
        ----
        symbol {str} -- The ticker symbol to fetch history for.
        as_of_date {datetime} -- If provided, fetches history up to this date instead
            of today. Used by the backtester to score stocks at a historical point in
            time. (default: None — uses today)

        Returns:
        ----
        pd.Series -- Closing prices indexed by date. Empty series on failure.
        """
        cache_key = f"{symbol}_{as_of_date.date() if as_of_date else 'live'}"
        if cache_key in self._price_history_cache:
            return self._price_history_cache[cache_key]

        end_date = as_of_date or datetime.now()
        # Fetch extra days to account for weekends and holidays
        start_date = end_date - timedelta(days=self.momentum_lookback_days + 60)

        try:
            prices = self._fetch_price_history_from_api(symbol, start_date, end_date)
            self._price_history_cache[cache_key] = prices
            return prices
        except Exception as e:
            logger.warning(f"Scorer: Could not fetch history for {symbol}: {e}")
            return pd.Series(dtype=float)

    def score_12_month_return(self, prices: pd.Series) -> float:
        """Calculates the raw 12-month price return.

        This is the core signal from Dual Momentum — stocks with strong
        12-month returns tend to continue outperforming in the near term.

        Returns value normalized between -1.0 and beyond 1.0 (raw return ratio).
        """
        if len(prices) < 2:
            return 0.0
        return (prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0]

    def score_rsi_position(self, prices: pd.Series, period: int = 14) -> float:
        """Scores how favorable the current RSI level is for a new entry.

        Rewards stocks in a healthy momentum range (RSI 40-60).
        Penalizes stocks already overbought (RSI > 70) to avoid buying at peaks.
        Mildly rewards oversold stocks (RSI < 30) as potential bounce candidates.

        Returns:
        ----
        float -- Score between 0.0 and 1.0.
        """
        if len(prices) < period + 1:
            return 0.5

        delta = prices.diff()
        gains = delta.clip(lower=0).rolling(period).mean()
        losses = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gains / losses.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]

        if pd.isna(current_rsi):
            return 0.5
        elif 40 <= current_rsi <= 60:
            return 1.0   # Ideal momentum range
        elif current_rsi > 70:
            return 0.2   # Overbought — risky entry
        elif current_rsi < 30:
            return 0.4   # Oversold — possible bounce but may keep falling
        else:
            return 0.7   # Acceptable range (30-40 or 60-70)

    def score_ema_trend(self, prices: pd.Series) -> float:
        """Scores trend strength using 50-day and 200-day EMAs.

        Price above both EMAs with 50 above 200 (golden cross) = strongest signal.
        This confirms that momentum is backed by a genuine uptrend.

        Returns:
        ----
        float -- Score between 0.0 and 1.0.
        """
        if len(prices) < 50:
            return 0.5

        ema_50 = prices.ewm(span=50, adjust=False).mean().iloc[-1]
        current_price = prices.iloc[-1]

        if len(prices) >= 200:
            ema_200 = prices.ewm(span=200, adjust=False).mean().iloc[-1]
            if current_price > ema_50 > ema_200:
                return 1.0   # Strong uptrend: price > 50 EMA > 200 EMA
            elif current_price > ema_200:
                return 0.6   # Above long-term trend but 50 EMA lagging
            elif current_price > ema_50:
                return 0.4   # Short-term momentum but below long-term trend
            else:
                return 0.1   # Downtrend
        else:
            # Not enough history for 200 EMA — use 50 EMA only
            return 0.8 if current_price > ema_50 else 0.3

    def composite_score(self, symbol: str, as_of_date: Optional[datetime] = None) -> float:
        """Calculates the weighted composite momentum score for a single stock.

        Formula: (0.6 × 12mo_return) + (0.3 × ema_trend) + (0.1 × rsi_position)

        Arguments:
        ----
        symbol {str} -- The ticker to score.
        as_of_date {datetime} -- If provided, scores the stock as of this historical
            date. Used by the backtester. (default: None — uses today)

        Returns:
        ----
        float -- Composite score. Higher = better momentum candidate.
                 Returns 0.0 if price history cannot be fetched.
        """
        prices = self.fetch_price_history(symbol, as_of_date=as_of_date)
        if prices.empty:
            return 0.0

        momentum_score = self.score_12_month_return(prices)
        ema_score = self.score_ema_trend(prices)
        rsi_score = self.score_rsi_position(prices)

        return (self.weight_momentum * momentum_score) + (self.weight_ema * ema_score) + (self.weight_rsi * rsi_score)

    def rank_candidates(self, symbols: List[str]) -> List[Dict]:
        """Scores and ranks all candidate symbols from best to worst.

        Arguments:
        ----
        symbols {List[str]} -- Symbols to score (typically scanner output).

        Returns:
        ----
        List[Dict] -- Sorted list of {'symbol': str, 'score': float}, best first.
        """
        logger.info(f"Scorer: Scoring {len(symbols)} candidates...")
        scored = []

        for symbol in symbols:
            try:
                score = self.composite_score(symbol)
                scored.append({'symbol': symbol, 'score': score})
            except Exception as e:
                logger.warning(f"Scorer: Skipping {symbol} due to error: {e}")

        ranked = sorted(scored, key=lambda x: x['score'], reverse=True)
        if ranked:
            logger.info(f"Scorer: Ranking complete. Top pick: {ranked[0]['symbol']} (score: {ranked[0]['score']:.3f})")
        else:
            logger.warning("Scorer: No valid scores.")
        return ranked

    def get_top_picks(self, symbols: List[str], n: int) -> List[str]:
        """Returns the top N symbols by composite momentum score.

        Arguments:
        ----
        symbols {List[str]} -- Candidate symbols from the scanner.
        n {int} -- How many top stocks to return.

        Returns:
        ----
        List[str] -- Top N symbols, best momentum first.
        """
        ranked = self.rank_candidates(symbols)
        return [item['symbol'] for item in ranked[:n]]
