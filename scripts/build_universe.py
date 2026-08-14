"""
Universe builder — merges ticker lists from multiple exchanges and indices into
a single deduplicated universe.csv that the scanner uses at startup.

Run this script once (or whenever you want to refresh the universe):
    cd python-trading-robot
    python scripts/build_universe.py

Expected source files in data/ (place whichever you have — each is optional
except you need at least one):
    data/nasdaq_raw.csv     — NASDAQ screener download
                              https://www.nasdaq.com/market-activity/stocks/screener
    data/nyse_raw.csv       — NYSE screener download (same URL, filter by NYSE)
    data/russell1000.csv    — iShares IWB holdings CSV
                              https://www.ishares.com/us/products/239707/
    data/sp500.csv          — S&P 500 list from DataHub
                              https://datahub.io/core/s-and-p-500-companies

Output:
    data/universe.csv — columns: ticker, exchange, name, sector
"""

import pathlib
import logging
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path('data')
OUTPUT_PATH = DATA_DIR / 'universe.csv'


def _load_nasdaq_or_nyse(path: pathlib.Path, default_exchange: str) -> pd.DataFrame:
    """Loads a NASDAQ or NYSE screener CSV.

    NASDAQ/NYSE screener columns:
        Symbol, Name, Last Sale, Net Change, % Change, Market Cap,
        Country, IPO Year, Volume, Sector, Industry
    """
    df = pd.read_csv(path, dtype=str)
    # Handle both 'Symbol' and first-column fallbacks
    ticker_col = 'Symbol' if 'Symbol' in df.columns else df.columns[0]
    sector_col = 'Sector' if 'Sector' in df.columns else None
    name_col = 'Name' if 'Name' in df.columns else None

    result = pd.DataFrame()
    result['ticker'] = df[ticker_col].str.strip()
    result['exchange'] = default_exchange
    result['name'] = df[name_col].str.strip() if name_col else ''
    result['sector'] = df[sector_col].str.strip() if sector_col else 'Unknown'
    return result


def _load_ishares_russell(path: pathlib.Path) -> pd.DataFrame:
    """Loads an iShares Russell 1000 (IWB) holdings CSV.

    iShares format has several header rows before the actual data starts.
    Columns include: Ticker, Name, Asset Class, Exchange, ...
    """
    # iShares files have junk rows at the top — find the real header
    raw = pd.read_csv(path, header=None, dtype=str)
    header_row = None
    for i, row in raw.iterrows():
        if 'Ticker' in row.values or 'ticker' in row.values:
            header_row = i
            break

    if header_row is None:
        logger.warning(f"Could not find header row in {path.name} — skipping.")
        return pd.DataFrame()

    df = pd.read_csv(path, skiprows=header_row, dtype=str)
    df.columns = df.columns.str.strip()

    ticker_col = next((c for c in df.columns if c.lower() == 'ticker'), None)
    name_col = next((c for c in df.columns if c.lower() == 'name'), None)
    sector_col = next((c for c in df.columns if 'sector' in c.lower()), None)
    exchange_col = next((c for c in df.columns if c.lower() == 'exchange'), None)

    if ticker_col is None:
        logger.warning(f"No ticker column found in {path.name} — skipping.")
        return pd.DataFrame()

    result = pd.DataFrame()
    result['ticker'] = df[ticker_col].str.strip()
    result['exchange'] = df[exchange_col].str.strip() if exchange_col else 'US'
    result['name'] = df[name_col].str.strip() if name_col else ''
    result['sector'] = df[sector_col].str.strip() if sector_col else 'Unknown'
    return result


def _load_sp500(path: pathlib.Path) -> pd.DataFrame:
    """Loads an S&P 500 CSV from DataHub or Wikipedia.

    DataHub format: Symbol, Security, GICS Sector, GICS Sub-Industry, ...
    Wikipedia format: Symbol, Security, GICS Sector, ...
    """
    df = pd.read_csv(path, dtype=str)
    df.columns = df.columns.str.strip()

    ticker_col = next((c for c in df.columns if c.lower() in ('symbol', 'ticker')), None)
    name_col = next((c for c in df.columns if c.lower() in ('security', 'name', 'company')), None)
    sector_col = next((c for c in df.columns if 'sector' in c.lower()), None)

    if ticker_col is None:
        logger.warning(f"No ticker column found in {path.name} — skipping.")
        return pd.DataFrame()

    result = pd.DataFrame()
    result['ticker'] = df[ticker_col].str.strip()
    result['exchange'] = 'US'
    result['name'] = df[name_col].str.strip() if name_col else ''
    result['sector'] = df[sector_col].str.strip() if sector_col else 'Unknown'
    return result


def build_universe() -> None:
    """Merges all available source files and writes universe.csv."""
    frames = []

    sources = [
        (DATA_DIR / 'nasdaq_raw.csv', 'NASDAQ', _load_nasdaq_or_nyse),
        (DATA_DIR / 'nyse_raw.csv',   'NYSE',   _load_nasdaq_or_nyse),
        (DATA_DIR / 'russell1000.csv', None,    _load_ishares_russell),
        (DATA_DIR / 'sp500.csv',       None,    _load_sp500),
    ]

    for path, exchange_label, loader in sources:
        if not path.exists():
            logger.info(f"  Skipping {path.name} — file not found.")
            continue
        logger.info(f"  Loading {path.name}...")
        try:
            if exchange_label:
                df = loader(path, exchange_label)
            else:
                df = loader(path)
            logger.info(f"    {len(df)} rows loaded.")
            frames.append(df)
        except Exception as e:
            logger.warning(f"  Failed to load {path.name}: {e}")

    if not frames:
        raise RuntimeError(
            "No source files found in data/. Download at least one of:\n"
            "  nasdaq_raw.csv, nyse_raw.csv, russell1000.csv, sp500.csv"
        )

    combined = pd.concat(frames, ignore_index=True)
    logger.info(f"\nCombined: {len(combined)} total rows across all sources.")

    # Drop empty/null tickers
    combined = combined[combined['ticker'].notna()]
    combined = combined[combined['ticker'].str.strip() != '']

    # Drop tickers with dots or dashes (warrants, preferred shares, foreign ordinaries)
    combined = combined[~combined['ticker'].str.contains(r'[.\-]', regex=True)]

    # Deduplicate — keep first occurrence (NASDAQ/NYSE take priority over index files
    # since they were loaded first and have exchange info)
    before = len(combined)
    combined = combined.drop_duplicates(subset='ticker', keep='first')
    logger.info(f"After deduplication: {len(combined)} unique tickers (removed {before - len(combined)} duplicates).")

    # Fill missing sectors
    combined['sector'] = combined['sector'].fillna('Unknown').replace('', 'Unknown')

    # Sort for readability
    combined = combined.sort_values('ticker').reset_index(drop=True)

    DATA_DIR.mkdir(exist_ok=True)
    combined.to_csv(OUTPUT_PATH, index=False)
    logger.info(f"\nSaved {len(combined)} tickers to {OUTPUT_PATH}")

    # Summary by exchange
    logger.info("\nBreakdown by exchange:")
    for exchange, count in combined['exchange'].value_counts().items():
        logger.info(f"  {exchange}: {count}")

    logger.info("\nBreakdown by sector (top 10):")
    for sector, count in combined['sector'].value_counts().head(10).items():
        logger.info(f"  {sector}: {count}")


if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Building universe.csv")
    logger.info("=" * 60)
    build_universe()
    logger.info("\nDone. Your bot will use data/universe.csv on the next run.")
