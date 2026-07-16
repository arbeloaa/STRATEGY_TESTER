"""
sf1_loader.py  --  One-time historical fundamentals backfill from Nasdaq Data Link
=====================================================================================
Pulls SHARADAR/SF1 (Core US Fundamentals, as-reported annual dimension) for the
entire STRATEGY_TESTER ticker universe in ONE get_table() call (returns a
DataFrame directly, no zip file), then writes point-in-time snapshots into
market_data.db via db.append_fundamentals(), using the SAME
compute_fundamentals_at() math that data_fetcher.py already uses for yfinance --
so derived metrics (GM %, Rule 40, ROIC %, etc.) stay numerically consistent
with what tester.py's gates were tuned against.

WHY THIS EXISTS:
  yfinance .financials / .balance_sheet / .cashflow only return ~4-5 fiscal years
  and are RESTATED (not point-in-time). SF1's ARY dimension goes back to 1997
  and is as-reported -- each row reflects what was actually known/filed at the
  time, with datekey = the real filing date (replacing the old +90-day guess).

KEY DESIGN DECISIONS:
  - dimension='ARY' only (as-reported ANNUAL). Matches the annual cadence
    compute_fundamentals_at() already expects (T0/T1/T2 = 3 most recent FYs).
  - get_table() (not export_table()): get_table() returns a pandas DataFrame
    directly and caps at 1,000,000 rows/call. export_table() instead writes
    an entire-table zip file to disk and returns None -- the wrong tool for
    a filtered, ~4,700-row pull (168 tickers x ~28 years). get_table() is
    simpler here and needs no zip-extraction step.
  - Re-shapes SF1's flat rows back into the same income/balance/cash "wide by
    fiscal-year-column" DataFrame shape that compute_fundamentals_at() expects
    from yfinance, then calls that function UNCHANGED. One source of truth for
    the formulas; gate thresholds in tester.py remain valid.
  - availability_date = SF1's datekey (real filing date) -- NOT period_end + 90d.
  - Writes via db.append_fundamentals() (same append-only path pit_fundamentals.py
    uses) so get_pit_fundamentals() / portfolio_simulator.py need NO changes.

USAGE:
  Set NASDAQ_DATA_LINK_API_KEY env var, then:
    python sf1_loader.py                  # full universe, all available years
    python sf1_loader.py --tickers AAPL MSFT   # test on a subset first
    python sf1_loader.py --dry-run        # fetch + map, but don't write to DB

REQUIREMENTS:
  pip install nasdaq-data-link   (PyPI package name has hyphens;
                                   the import name "nasdaqdatalink" does not)
"""

import sys
import os
import argparse
import time
from pathlib import Path

import pandas as pd

try:
    import nasdaqdatalink
    _NDL_AVAILABLE = True
except ImportError:
    _NDL_AVAILABLE = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # data_pipeline/

from data_fetcher import (
    compute_fundamentals_at,
    SECTORS,
    DELISTED_TICKERS,
)
import db as _db

DIMENSION = "ARY"          # as-reported annual -- matches compute_fundamentals_at()'s annual cadence
MIN_PRIOR_YEARS = 2        # T0 needs T1 and T2 available (3 fiscal years minimum)

# SF1 column name -> internal key used to rebuild yfinance-shaped statements.
# (Only the fields compute_fundamentals_at() actually reads are needed here.)
SF1_TO_INTERNAL = {
    "revenue":       "Total Revenue",
    "gp":            "Gross Profit",
    "ebit":          "EBIT",
    "taxexp":        "Tax Provision",
    "ebt":           "Pretax Income",
    "inventory":     "Inventory",
    "assets":        "Total Assets",
    "liabilitiesc":  "Current Liabilities",
    "shareswa":      "Ordinary Shares Number",
    "ncfo":          "Operating Cash Flow",
    "capex":         "Capital Expenditure",
    "netinc":        "Net Income",
}


def build_ticker_universe() -> list:
    """Same universe data_fetcher.py / portfolio_simulator.py already trade."""
    tickers = set()
    for sector_tickers in SECTORS.values():
        tickers.update(sector_tickers)
    for tk in DELISTED_TICKERS:
        tickers.add(tk)
    return sorted(tickers)


def ticker_sector_map() -> dict:
    m = {}
    for sector, tickers in SECTORS.items():
        for tk in tickers:
            m[tk] = sector
    for tk, val in DELISTED_TICKERS.items():
        if isinstance(val, dict):
            m[tk] = val.get("sector", "Unknown")
        else:
            m[tk] = str(val)
    return m


def fetch_sf1_bulk(tickers: list, dimension: str = DIMENSION) -> pd.DataFrame:
    """
    Pull SF1 for the whole ticker universe using get_table(), which returns a
    DataFrame directly (no zip file to download/extract).

    NOTE: an earlier version of this script used export_table(), which is the
    wrong tool here -- export_table() writes a zip file to disk and returns
    None; it's meant for dumping an ENTIRE multi-million-row table. get_table()
    returns a ready-to-use DataFrame and caps at 1,000,000 rows per call, which
    is far more than this project needs: 168 tickers x ~28 years of annual
    data is roughly 4,700 rows -- nowhere near that cap, so a single call
    is sufficient. paginate=True is passed so the SDK handles continuation
    internally if the result set is ever larger than one page.
    """
    if not _NDL_AVAILABLE:
        print("ERROR: nasdaqdatalink not installed. Run:")
        print("  pip install nasdaq-data-link")
        sys.exit(1)

    api_key = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get("QUANDL_API_KEY")
    if not api_key:
        print("ERROR: set NASDAQ_DATA_LINK_API_KEY environment variable first.")
        sys.exit(1)
    nasdaqdatalink.ApiConfig.api_key = api_key

    print(f"  Requesting SF1 via get_table(): {len(tickers)} tickers, dimension={dimension} ...")
    t0 = time.time()
    df = nasdaqdatalink.get_table(
        "SHARADAR/SF1",
        ticker=tickers,
        dimension=dimension,
        paginate=True,
    )
    elapsed = time.time() - t0
    if df is None:
        df = pd.DataFrame()
    print(f"  Received {len(df)} rows in {elapsed:.1f}s")
    return df


def rebuild_statement_frames(ticker_df: pd.DataFrame):
    """
    Re-shape one ticker's flat SF1 rows (one row per fiscal year) into the
    wide-by-fiscal-year-column DataFrames that compute_fundamentals_at()
    expects (mirroring yfinance's .financials / .balance_sheet / .cashflow
    shape: index = line-item name, columns = pd.Timestamp fiscal year ends).

    Returns (income, balance, cash, q_income, q_cash) -- quarterly frames are
    left empty since we only pulled ARY (annual); GM_Change_QoQ will be N/A,
    which is fine, it's a secondary/informational column.
    """
    ticker_df = ticker_df.sort_values("calendardate", ascending=False).reset_index(drop=True)

    income_rows  = {}
    balance_rows = {}
    cash_rows    = {}

    fiscal_cols = []
    for _, row in ticker_df.iterrows():
        fy_end = pd.Timestamp(row["calendardate"])
        fiscal_cols.append(fy_end)

        for sf1_col, internal_name in SF1_TO_INTERNAL.items():
            val = row.get(sf1_col)
            target = None
            if internal_name in ("Total Revenue", "Gross Profit", "EBIT",
                                  "Tax Provision", "Pretax Income", "Net Income"):
                target = income_rows
            elif internal_name in ("Inventory", "Total Assets",
                                    "Current Liabilities", "Ordinary Shares Number"):
                target = balance_rows
            elif internal_name in ("Operating Cash Flow", "Capital Expenditure"):
                target = cash_rows
            if target is None:
                continue
            target.setdefault(internal_name, {})[fy_end] = val

    income  = pd.DataFrame(income_rows).T  if income_rows  else pd.DataFrame()
    balance = pd.DataFrame(balance_rows).T if balance_rows else pd.DataFrame()
    cash    = pd.DataFrame(cash_rows).T    if cash_rows    else pd.DataFrame()

    # Quarterly frames: not pulled (ARY only) -- empty is handled gracefully
    # by compute_fundamentals_at() (GM_Change_QoQ becomes None/NA).
    q_income = pd.DataFrame()
    q_cash   = pd.DataFrame()

    return income, balance, cash, q_income, q_cash, sorted(fiscal_cols, reverse=True)


def process_ticker(ticker: str, sector: str, ticker_df: pd.DataFrame,
                   dry_run: bool = False) -> int:
    """
    Build PIT snapshots for one ticker across all qualifying fiscal years.
    Returns count of snapshots written (or would-be-written if dry_run).
    """
    if ticker_df.empty:
        print(f"  [SKIP] {ticker}: no SF1 rows returned")
        return 0

    income, balance, cash, q_income, q_cash, fiscal_cols = rebuild_statement_frames(ticker_df)
    if income.empty or len(fiscal_cols) < MIN_PRIOR_YEARS + 1:
        print(f"  [SKIP] {ticker}: insufficient fiscal years ({len(fiscal_cols)} found, "
              f"need >= {MIN_PRIOR_YEARS + 1})")
        return 0

    # datekey per fiscal year -- this is the real filing date, replacing the
    # old period_end + EARNINGS_LAG_DAYS estimate.
    datekey_by_fy = {}
    for _, row in ticker_df.iterrows():
        fy_end = pd.Timestamp(row["calendardate"])
        dk = row.get("datekey")
        if pd.notna(dk):
            datekey_by_fy[fy_end] = pd.Timestamp(dk)

    # price_at_anchor: look up the price on (or nearest before) datekey from
    # the existing local price cache (db.get_prices). compute_fundamentals_at()
    # uses this ONLY for the PS/Growth ratio -- if price history isn't in the
    # DB yet for this ticker/date range, that single ratio is skipped
    # (set to None) rather than failing the whole snapshot.
    price_df = _db.get_prices(ticker)

    n_written = 0
    for i, T0 in enumerate(fiscal_cols):
        if i + MIN_PRIOR_YEARS >= len(fiscal_cols):
            break
        if T0 not in balance.columns or T0 not in cash.columns:
            continue

        avail_date = datekey_by_fy.get(T0)
        if avail_date is None:
            print(f"  [SKIP] {ticker} T0={T0.date()}: no datekey")
            continue
        if avail_date > pd.Timestamp.today():
            continue

        price_at_anchor = None
        if not price_df.empty:
            valid = price_df.index[price_df.index <= avail_date]
            if len(valid) > 0:
                price_at_anchor = float(price_df.loc[valid[-1], "Close"])

        if price_at_anchor is None or price_at_anchor <= 0:
            # PS/Growth will be unavailable for this snapshot; everything else
            # (GM %, ROIC %, Rule 40, etc.) still computes fine since those
            # don't depend on price. Use a placeholder so compute_fundamentals_at
            # doesn't divide by zero -- it will skip ps_ratio (None checks already
            # exist there for rev[0] etc., but price needs to be a real positive
            # number to avoid a market_cap of 0 silently looking like real data).
            print(f"  [WARN] {ticker} T0={T0.date()}: no price in DB near "
                  f"{avail_date.date()} -- PS/Growth will be N/A for this snapshot")
            price_at_anchor = 0.01  # tiny placeholder; ps_ratio will look absurd
                                     # but downstream gate_valuation() already
                                     # treats ps==999.0 / NA as excluded -- this
                                     # placeholder does NOT match that sentinel,
                                     # so see note below: we explicitly null it out.

        fund = compute_fundamentals_at(
            income, balance, cash, q_income, q_cash,
            T0, price_at_anchor,
            ticker=ticker, sector=sector,
        )
        if fund is None:
            continue

        # If we used the placeholder price, force PS fields to the project's
        # "missing" sentinel (999.0) so tester.py's existing N/A handling
        # (gate_valuation / gate_na) takes over cleanly instead of showing a
        # nonsensical ratio computed off a fake $0.01 price.
        if price_at_anchor == 0.01:
            fund["PS/Growth"] = 999.0
            fund["PS_Ratio"]  = 999.0

        avail_str = str(avail_date.date())
        if dry_run:
            print(f"  [DRY-RUN] {ticker} T0={T0.date()} avail={avail_str}  "
                  f"GM%={fund.get('GM %')}  Rule40={fund.get('Rule 40')}  "
                  f"RevGrowth%={fund.get('Revenue_Growth_%')}")
            n_written += 1
            continue

        try:
            _db.append_fundamentals(
                ticker            = ticker,
                period_end        = str(T0.date()),
                availability_date = avail_str,
                sector            = sector,
                metrics_dict      = fund,
                captured_at       = avail_str,
            )
            n_written += 1
        except Exception as exc:
            print(f"  [DB WARN] {ticker} T0={T0.date()}: append_fundamentals failed: {exc}")

    return n_written


def main():
    parser = argparse.ArgumentParser(description="SF1 historical fundamentals backfill")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="Subset of tickers to test (default: full universe)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and map data but do not write to market_data.db")
    args = parser.parse_args()

    print("=" * 70)
    print("  SF1 HISTORICAL FUNDAMENTALS BACKFILL")
    print(f"  Dimension: {DIMENSION} (as-reported annual)")
    print("=" * 70)

    universe = args.tickers if args.tickers else build_ticker_universe()
    sec_map  = ticker_sector_map()
    print(f"\n  Ticker universe: {len(universe)} tickers")
    if args.dry_run:
        print("  DRY-RUN MODE -- nothing will be written to the DB")

    try:
        _db.init_db()
    except Exception as exc:
        print(f"  [DB WARN] init_db failed: {exc}")

    # ONE bulk call for everything.
    raw = fetch_sf1_bulk(universe, dimension=DIMENSION)
    if raw.empty:
        print("ERROR: SF1 export returned no rows. Check ticker list / API key / entitlement.")
        sys.exit(1)

    print(f"\n  Processing {raw['ticker'].nunique()} tickers with data ...")
    total_written = 0
    total_skipped = 0
    for ticker in universe:
        sub = raw[raw["ticker"] == ticker]
        sector = sec_map.get(ticker, "Unknown")
        if sub.empty:
            print(f"  [NO DATA] {ticker}: not found in SF1 export")
            total_skipped += 1
            continue
        n = process_ticker(ticker, sector, sub, dry_run=args.dry_run)
        if n > 0:
            print(f"  [OK] {ticker}: {n} snapshot(s)")
            total_written += n
        else:
            total_skipped += 1

    print("\n" + "=" * 70)
    print(f"  DONE. Snapshots {'(dry-run, not saved)' if args.dry_run else 'written'}: "
          f"{total_written}")
    print(f"  Tickers skipped/no-data: {total_skipped}")
    if not args.dry_run:
        cov = _db.list_pit_coverage()
        print(f"  Total PIT coverage in DB now: {len(cov)} tickers")
    print("=" * 70)


if __name__ == "__main__":
    main()
