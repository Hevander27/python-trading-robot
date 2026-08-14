"""
UniverseScanner — loads tickers from universe.csv (or falls back to
nasdaq_symbols.csv), fetches live quotes in batches, and filters down to
stocks that are affordable, liquid, and showing positive momentum.

To build universe.csv from multiple exchanges and indices:
    python scripts/build_universe.py

To get a basic NASDAQ-only symbol list:
    1. Go to https://www.nasdaq.com/market-activity/stocks/screener
    2. Select Exchange = NASDAQ, click Download CSV
    3. Save it to data/nasdaq_symbols.csv in the project root
"""

import time
import logging
import pathlib
import pandas as pd
from typing import List, Dict

from pyrobot.utils import retry_with_backoff

logger = logging.getLogger(__name__)


class NasdaqScanner:

    # Schwab's get_quotes endpoint accepts up to 500 symbols per call
    QUOTE_BATCH_SIZE = 500

    # Fallback if universe.csv is not present
    FALLBACK_CSV = 'data/nasdaq_symbols.csv'

    def __init__(
        self,
        schwab_client,
        symbols_csv_path: str,
        budget: float,
        max_positions: int,
        min_volume: int,
        pre_filter_momentum: bool = True,
        min_net_change_pct: float = 0.0
    ) -> None:
        """Initializes the scanner with filtering parameters.

        Arguments:
        ----
        schwab_client -- Authenticated Schwab API client (robot.session).
        symbols_csv_path {str} -- Path to the universe CSV. If not found, falls
            back to data/nasdaq_symbols.csv automatically.
        budget {float} -- Total capital available to deploy.
        max_positions {int} -- Maximum number of stocks to hold simultaneously.
        min_volume {int} -- Minimum average daily volume to consider a stock liquid.
        pre_filter_momentum {bool} -- If True, drops stocks with a negative net
            percent change before fetching full price history. Cuts the candidate
            list by 60-70%, making scoring 5-10x faster. (default: True)
        min_net_change_pct {float} -- Minimum net percent change a stock must have
            to pass the momentum pre-filter. Only used when pre_filter_momentum=True.
            (default: 0.0)
        """
        self.schwab_client = schwab_client
        self.symbols_csv_path = symbols_csv_path
        self.budget = budget
        self.max_positions = max_positions
        self.min_volume = min_volume
        self.pre_filter_momentum = pre_filter_momentum
        self.min_net_change_pct = min_net_change_pct

        # Populated by load_symbols() — maps symbol → {sector, exchange, name}
        self.symbol_metadata: Dict[str, dict] = {}

    def load_symbols(self) -> List[str]:
        """Loads ticker symbols and metadata from universe.csv or nasdaq_symbols.csv.

        Tries the configured path first. If not found, falls back to
        data/nasdaq_symbols.csv so the existing setup still works.

        Returns:
        ----
        List[str] -- All valid ticker symbols found in the CSV.
        """
        path = pathlib.Path(self.symbols_csv_path)
        if not path.exists():
            fallback = pathlib.Path(self.FALLBACK_CSV)
            if fallback.exists():
                logger.info(
                    f"Scanner: {path.name} not found — falling back to {fallback.name}. "
                    f"Run 'python scripts/build_universe.py' to build the expanded universe."
                )
                path = fallback
            else:
                raise FileNotFoundError(
                    f"No symbol file found at {self.symbols_csv_path} or {self.FALLBACK_CSV}."
                )

        df = pd.read_csv(path, dtype=str)
        df.columns = df.columns.str.strip()

        # universe.csv has 'ticker'; nasdaq_symbols.csv has 'Symbol'
        if 'ticker' in df.columns:
            ticker_col = 'ticker'
        elif 'Symbol' in df.columns:
            ticker_col = 'Symbol'
        else:
            ticker_col = df.columns[0]

        sector_col = next((c for c in df.columns if c.lower() == 'sector'), None)
        exchange_col = next((c for c in df.columns if c.lower() == 'exchange'), None)
        name_col = next((c for c in df.columns if c.lower() == 'name'), None)

        symbols = []
        self.symbol_metadata = {}

        for _, row in df.iterrows():
            symbol = str(row[ticker_col]).strip()
            if not symbol or not symbol.replace('.', '').isalpha():
                continue
            if len(symbol) > 5:
                continue

            symbols.append(symbol)
            self.symbol_metadata[symbol] = {
                'sector':   str(row[sector_col]).strip() if sector_col else 'Unknown',
                'exchange': str(row[exchange_col]).strip() if exchange_col else 'Unknown',
                'name':     str(row[name_col]).strip() if name_col else ''
            }

        return symbols

    def get_symbol_sector(self, symbol: str) -> str:
        """Returns the sector for a symbol from the universe metadata.

        Arguments:
        ----
        symbol {str} -- Ticker symbol.

        Returns:
        ----
        str -- Sector name, or 'Unknown' if not in metadata.
        """
        return self.symbol_metadata.get(symbol, {}).get('sector', 'Unknown')

    def get_symbol_exchange(self, symbol: str) -> str:
        """Returns the exchange for a symbol from the universe metadata."""
        return self.symbol_metadata.get(symbol, {}).get('exchange', 'Unknown')

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def _fetch_single_batch(self, batch: List[str]) -> Dict[str, dict]:
        """Fetches quotes for a single batch of symbols. Retried on failure."""
        response = self.schwab_client.get_quotes(batch).json()
        return response if isinstance(response, dict) else {}

    def _fetch_quotes_in_batches(self, symbols: List[str]) -> Dict[str, dict]:
        """Fetches live quotes from Schwab in batches to respect API limits."""
        all_quotes = {}

        for i in range(0, len(symbols), self.QUOTE_BATCH_SIZE):
            batch = symbols[i:i + self.QUOTE_BATCH_SIZE]
            try:
                all_quotes.update(self._fetch_single_batch(batch))
            except Exception as e:
                logger.warning(f"Skipping batch at index {i} after retries: {e}")

            if i + self.QUOTE_BATCH_SIZE < len(symbols):
                time.sleep(0.5)

        return all_quotes

    def _filter_by_price(self, quotes: Dict[str, dict]) -> List[str]:
        """Keeps only symbols priced at or below budget / max_positions."""
        max_price_per_stock = self.budget / self.max_positions
        affordable = []
        for symbol, data in quotes.items():
            if not isinstance(data, dict) or 'quote' not in data:
                continue
            price = data['quote'].get('lastPrice', 0)
            if 0 < price <= max_price_per_stock:
                affordable.append(symbol)
        return affordable

    def _filter_by_volume(self, quotes: Dict[str, dict], symbols: List[str]) -> List[str]:
        """Removes thinly traded stocks to avoid liquidity traps."""
        return [
            symbol for symbol in symbols
            if quotes.get(symbol, {}).get('quote', {}).get('totalVolume', 0) >= self.min_volume
        ]

    def _filter_by_momentum(self, quotes: Dict[str, dict], symbols: List[str]) -> List[str]:
        """Pre-filters using netPercentChange already in the quote response."""
        passing = []
        for symbol in symbols:
            net_pct = quotes.get(symbol, {}).get('quote', {}).get('netPercentChange', None)
            if net_pct is not None and net_pct > self.min_net_change_pct:
                passing.append(symbol)
        return passing

    def get_candidate_symbols(self) -> List[str]:
        """Full scan pipeline: load → quote → filter by price → volume → momentum.

        Returns:
        ----
        List[str] -- Affordable, liquid, optionally pre-filtered symbols ready to score.
        """
        logger.info("Scanner: Loading universe...")
        all_symbols = self.load_symbols()
        logger.info(f"Scanner: Loaded {len(all_symbols)} symbols. Fetching quotes...")

        quotes = self._fetch_quotes_in_batches(all_symbols)
        logger.info(f"Scanner: Received quotes for {len(quotes)} symbols. Filtering...")

        affordable = self._filter_by_price(quotes)
        logger.info(f"Scanner: {len(affordable)} symbols within price budget.")

        liquid = self._filter_by_volume(quotes, affordable)
        logger.info(f"Scanner: {len(liquid)} symbols pass volume filter.")

        if self.pre_filter_momentum:
            candidates = self._filter_by_momentum(quotes, liquid)
            logger.info(
                f"Scanner: {len(candidates)} symbols pass momentum pre-filter "
                f"(net change > {self.min_net_change_pct}%). Scan complete."
            )
        else:
            candidates = liquid
            logger.info("Scanner: Momentum pre-filter disabled. Scan complete.")

        return candidates
