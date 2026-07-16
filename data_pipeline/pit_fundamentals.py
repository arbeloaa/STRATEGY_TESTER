"""
pit_fundamentals.py
Build a long-format point-in-time (PIT) fundamentals database.

One row per (ticker, fiscal_year_end), stamped with:
  availability_date = fiscal_year_end + EARNINGS_LAG_DAYS
  price_at_anchor   = adjusted close on availability_date (or nearest prior)

Output files:
  pit_fundamentals.csv  -- consumed by portfolio_simulator.py
  pit_coverage.txt      -- summary: snapshot count, date range, SHALLOW flag

Usage:
  python pit_fundamentals.py [--out-dir <dir>]
"""

import sys
import os
import argparse
import pandas as pd
import yfinance as yf
from pathlib import Path

# Locate data_pipeline siblings
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # data_pipeline/

from data_fetcher import (
    compute_fundamentals_at,
    EARNINGS_LAG_DAYS,
    SECTORS,
    DELISTED_TICKERS,
    pick_fiscal_columns,
)
import db as _db

# ---------------------------------------------------------------------------
# Build combined ticker-sector map
# ---------------------------------------------------------------------------
TICKER_SECTOR_MAP = {}
for _sec, _tickers in SECTORS.items():
    for _tk in _tickers:
        TICKER_SECTOR_MAP[_tk] = _sec
for _tk, _val in DELISTED_TICKERS.items():
    # DELISTED_TICKERS values may be dicts {"sector": ..., "delisted_year": ...}
    # or plain strings depending on the version
    if isinstance(_val, dict):
        TICKER_SECTOR_MAP[_tk] = _val.get("sector", "Unknown")
    else:
        TICKER_SECTOR_MAP[_tk] = str(_val)

ALL_TICKERS = sorted(TICKER_SECTOR_MAP.keys())

# Minimum prior fiscal years required (T0 needs T1 and T2 for trend metrics)
MIN_PRIOR_YEARS = 2


def _nearest_price(price_series, target_date):
    """Return the closing price on target_date or the nearest prior trading day."""
    ts = pd.Timestamp(target_date)
    idx = price_series.index
    valid = idx[idx <= ts]
    if valid.empty:
        return None
    return float(price_series.loc[valid[-1]])


def _get_price_history(ticker):
    """Download max available adjusted-close history for ticker, cache to DB."""
    try:
        df = yf.download(ticker, period="max", auto_adjust=True, progress=False)
        if df.empty:
            return None
        # Flatten multi-level columns if present (yfinance 0.2.x)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close.index = pd.to_datetime(close.index).tz_localize(None)
        # Cache OHLCV to DB
        try:
            ohlcv_cols = [c for c in ["Close", "High", "Low", "Volume"] if c in df.columns]
            _db.upsert_prices(ticker, df[ohlcv_cols])
        except Exception as _dbe:
            print(f"  [DB WARN] upsert_prices {ticker}: {_dbe}")
        return close.sort_index()
    except Exception as e:
        print(f"  [PRICE ERROR] {ticker}: {e}")
        return None


def build_pit_row(ticker, sector, T0, income, balance, cash, q_income, q_cash,
                  price_series):
    """
    Compute one PIT row for (ticker, T0).
    Returns dict or None.
    """
    avail_date = pd.Timestamp(T0) + pd.Timedelta(days=EARNINGS_LAG_DAYS)
    price_at_anchor = _nearest_price(price_series, avail_date)
    if price_at_anchor is None or price_at_anchor <= 0:
        print(f"  [SKIP] {ticker} T0={T0.date()}: no price near {avail_date.date()}")
        return None

    fund = compute_fundamentals_at(
        income, balance, cash, q_income, q_cash,
        T0, price_at_anchor,
        ticker=ticker, sector=sector,
    )
    if fund is None:
        return None

    row = {
        "Ticker":            ticker,
        "Sector":            sector,
        "period_end":        str(T0.date()),
        "availability_date": str(avail_date.date()),
        "price_at_anchor":   round(price_at_anchor, 4),
    }
    row.update({k: (None if pd.isna(v) else v) for k, v in fund.items()})
    return row


def process_ticker(ticker, sector):
    """
    Fetch statements + price history, emit one row per qualifying fiscal year.
    Returns list of row dicts (may be empty).
    """
    print(f"  {ticker} ({sector})")
    rows = []
    try:
        s       = yf.Ticker(ticker)
        income  = s.financials
        balance = s.balance_sheet
        cash    = s.cashflow

        if income is None or income.empty:
            print(f"    [SKIP] no annual income statement")
            return rows

        q_income = s.quarterly_financials
        q_cash   = s.quarterly_cashflow

        price_series = _get_price_history(ticker)
        if price_series is None:
            print(f"    [SKIP] no price history")
            return rows

        # Candidate T0 columns: all annual columns with at least T1+T2 available
        all_ann = sorted(
            [c for c in income.columns if isinstance(c, pd.Timestamp)],
            reverse=True
        )

        for i, T0 in enumerate(all_ann):
            # Need T1 and T2 after T0 in the sorted list
            if i + MIN_PRIOR_YEARS >= len(all_ann):
                break
            if T0 not in balance.columns or T0 not in cash.columns:
                continue

            avail_date = T0 + pd.Timedelta(days=EARNINGS_LAG_DAYS)
            # Skip if availability date is in the future
            if avail_date > pd.Timestamp.today():
                continue

            row = build_pit_row(ticker, sector, T0, income, balance, cash,
                                q_income, q_cash, price_series)
            if row:
                rows.append(row)
                # Write snapshot to DB (append-only)
                avail_date_str = row["availability_date"]
                metrics = {k: v for k, v in row.items()
                           if k not in ("Ticker", "Sector", "period_end",
                                        "availability_date")}
                try:
                    _db.append_fundamentals(
                        ticker           = ticker,
                        period_end       = str(T0.date()),
                        availability_date = avail_date_str,
                        sector           = sector,
                        metrics_dict     = metrics,
                        captured_at      = avail_date_str,
                    )
                except Exception as _dbe:
                    print(f"    [DB WARN] append_fundamentals {ticker}: {_dbe}")

    except Exception as e:
        print(f"    [ERROR] {ticker}: {e}")
    return rows


def build_coverage_text(df):
    """Return ASCII coverage report string."""
    lines = []
    lines.append("PIT FUNDAMENTALS COVERAGE REPORT")
    lines.append("=" * 60)
    lines.append(f"{'Ticker':<8} {'Sector':<12} {'Snapshots':>9} "
                 f"{'First Avail':>12} {'Last Avail':>12} {'Flag'}")
    lines.append("-" * 60)
    for ticker, grp in df.groupby("Ticker"):
        sector     = grp["Sector"].iloc[0]
        n          = len(grp)
        first_av   = grp["availability_date"].min()
        last_av    = grp["availability_date"].max()
        flag       = "SHALLOW" if n == 1 else ""
        lines.append(f"{ticker:<8} {sector:<12} {n:>9} "
                     f"{first_av:>12} {last_av:>12} {flag}")
    lines.append("-" * 60)
    lines.append(f"Total rows: {len(df)}  |  Tickers: {df['Ticker'].nunique()}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build PIT fundamentals CSV")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "data"),
                        help="Directory for output files (default: project data dir)")
    args = parser.parse_args()

    out_dir  = args.out_dir
    csv_path = os.path.join(out_dir, "pit_fundamentals.csv")
    cov_path = str(PROJECT_ROOT / "logs" / "pit_coverage.txt")

    # Ensure DB is initialised
    try:
        _db.init_db()
    except Exception as _dbe:
        print(f"[DB WARN] init_db failed: {_dbe}")

    all_rows = []
    total    = len(ALL_TICKERS)
    for idx, ticker in enumerate(ALL_TICKERS, 1):
        sector = TICKER_SECTOR_MAP[ticker]
        print(f"[{idx}/{total}] {ticker}")
        rows = process_ticker(ticker, sector)
        all_rows.extend(rows)
        print(f"    -> {len(rows)} snapshot(s)")

    if not all_rows:
        print("ERROR: no rows generated -- check data_fetcher imports")
        sys.exit(1)

    df = pd.DataFrame(all_rows)
    df = df.sort_values(["Ticker", "availability_date"]).reset_index(drop=True)
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nWrote {len(df)} rows -> {csv_path}")

    cov_text = build_coverage_text(df)
    with open(cov_path, "w", encoding="ascii", errors="replace") as fh:
        fh.write(cov_text)
    print(f"Wrote coverage  -> {cov_path}")
    print()
    print(cov_text)


if __name__ == "__main__":
    main()
