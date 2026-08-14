"""
RegimeFilter — determines whether the market is in a bull or bear regime
by comparing the SPY price to its 200-day EMA.

    Bull market: SPY price > 200-day EMA → proceed with normal strategy
    Bear market: SPY price < 200-day EMA → stop new entries, tighten stops

Based on Gary Antonacci's Global Momentum framework. The 200-day EMA filter
acts as a circuit breaker that keeps the bot out of sustained downtrends
while allowing it to stay long during healthy bull markets.
"""

import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict

from pyrobot.utils import retry_with_backoff

logger = logging.getLogger(__name__)


class RegimeFilter:

    BENCHMARK = 'SPY'
    EMA_PERIOD = 200

    def __init__(self, schwab_client) -> None:
        """Initializes the filter with a Schwab client.

        Arguments:
        ----
        schwab_client -- Authenticated Schwab API client (robot.session).
        """
        self.schwab_client = schwab_client
        self._cached_regime: Dict = {}
        self._cache_date: datetime = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def _fetch_spy_prices(self) -> pd.Series:
        """Fetches 300 days of SPY daily closing prices."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=300)

        response = self.schwab_client.get_price_history(
            self.BENCHMARK,
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

    def get_regime(self, use_cache: bool = True) -> Dict:
        """Returns the current market regime based on SPY vs its 200-day EMA.

        Caches the result for the rest of the trading day to avoid repeated
        API calls on every loop tick.

        Arguments:
        ----
        use_cache {bool} -- If True, returns cached result if already checked
            today. Set to False to force a fresh API call. (default: True)

        Returns:
        ----
        dict -- {
            'is_bull':        bool   — True if SPY > 200-day EMA,
            'spy_price':      float  — Current SPY price,
            'spy_ema_200':    float  — Current 200-day EMA value,
            'pct_vs_ema':     float  — How far SPY is above/below EMA (%),
            'checked_at':     str    — Timestamp of this check
        }
        Returns is_bull=True on error so the bot defaults to operating normally
        if the regime check fails.
        """
        today = datetime.now().date()
        if use_cache and self._cache_date == today and self._cached_regime:
            return self._cached_regime

        try:
            prices = self._fetch_spy_prices()

            if prices.empty or len(prices) < self.EMA_PERIOD:
                logger.warning(
                    f"RegimeFilter: Not enough SPY data ({len(prices)} candles). "
                    f"Defaulting to bull market."
                )
                return self._bull_fallback()

            ema_200 = prices.ewm(span=self.EMA_PERIOD, adjust=False).mean().iloc[-1]
            spy_price = prices.iloc[-1]
            is_bull = spy_price > ema_200
            pct_vs_ema = (spy_price - ema_200) / ema_200 * 100

            regime = {
                'is_bull':     is_bull,
                'spy_price':   round(spy_price, 2),
                'spy_ema_200': round(ema_200, 2),
                'pct_vs_ema':  round(pct_vs_ema, 2),
                'checked_at':  datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            label = 'BULL' if is_bull else 'BEAR'
            color = '\033[92m' if is_bull else '\033[91m'
            logger.info(
                f"{color}Market Regime: {label} — "
                f"SPY ${spy_price:.2f} vs 200-EMA ${ema_200:.2f} "
                f"({pct_vs_ema:+.2f}%)\033[0m"
            )

            self._cached_regime = regime
            self._cache_date = today
            return regime

        except Exception as e:
            logger.error(f"RegimeFilter: Failed to determine regime: {e}. Defaulting to bull.")
            return self._bull_fallback()

    def is_bull_market(self) -> bool:
        """Convenience method — returns True if market is in a bull regime."""
        return self.get_regime()['is_bull']

    def _bull_fallback(self) -> Dict:
        """Returns a safe default (bull) when the check cannot be completed."""
        return {
            'is_bull':     True,
            'spy_price':   0.0,
            'spy_ema_200': 0.0,
            'pct_vs_ema':  0.0,
            'checked_at':  datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
