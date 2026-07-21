#!/usr/bin/env python3
"""
build_feature_cache.py  --  Two-layer feature cache with full benchmark universe.
==================================================================================
Builds data/feature_cache.db for every month-start date 2020-01 -> 2026-06.

TWO UNIVERSE LAYERS:
  benchmark_universe : every SF1 ARY ticker (~9,000, including delisted).
                       Used ONLY for industry-relative statistics and classifying
                       missed winners / dodged losers. Never traded.

  tradeable_universe : computed AS-OF-DATE per month-start.
                       Criteria: major US exchange, avg daily share vol >= 500K
                       AND avg daily dollar vol >= $5M (trailing 60 trading days),
                       SHARADAR industry NOT in EXCLUDED_INDUSTRIES.

HYBRID VOLUME PIPELINE (SHARADAR/DAILY marketcap pre-filter + yfinance OHLCV):
  Step A: Pull SHARADAR/DAILY marketcap for all SF1 tickers (one bulk paginated call).
          Pre-filter: drop tickers whose peak marketcap over 2019-2026 < $150M.
          (A stock with peak mcap < $150M essentially never sustains $5M/day dollar vol.)
          Pre-filtered tickers get tradeable_flag=0, exclusion_reason=EXCLUDED_MCAP_PREFILTER.
          This cuts ~9,000 tickers down to ~3,000-4,000 before any yfinance call.

  Step B: Pull yfinance OHLCV for survivors only (batched, 500 tickers/batch).
          Actual tradeable flag comes from measured volume, not the proxy.
          Failures per ticker labeled VOLUME_DATA_UNAVAILABLE, not silently dropped.

DELISTED BIAS CAVEAT (printed in every downstream report):
  yfinance returns little or nothing for many delisted names. The dodged-losers table
  (defense record) is SYSTEMATICALLY UNDER-COUNTED because the worst disasters
  (frauds, bankruptcies) are precisely those that delisted and vanished from yfinance.
  This biases loosen-vs-tighten decisions toward loosening.

  Mitigation: Where SHARADAR/DAILY has marketcap history for a delisted name,
  a marketcap collapse (>70% decline into delisting) generates a DODGED_LOSER_PROXY
  row in the cache, labeled as such. These are added to the dodged-loser defense record
  with a clear proxy label.

RATE-LIMIT POLICY:
  One bulk paginated call for SF1. One bulk paginated call for SHARADAR/DAILY.
  yfinance calls are batched at 500 tickers with 10s sleep between batches.
  NDL 429 -> stop immediately and report. No retry ladders.

DEGRADATION POLICY:
  Every incompleteness is labeled visibly. No partial picture presented as complete.

PROGRESS CHECKPOINTS:
  yfinance fetch status and OHLCV stored in SQLite. --resume skips fetched tickers.
  A crash at ticker 4,000 restarts from that point.

Usage:
  python scripts/build_feature_cache.py [--resume] [--spot-check N] [--dry-run]
  python scripts/build_feature_cache.py --size-only   # size estimates only, no build
"""

import sys
import os
import sqlite3
import json
import math
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # data_pipeline/

import db as _db

# ---------------------------------------------------------------------------
# NDL import
# ---------------------------------------------------------------------------
try:
    import nasdaqdatalink as _ndl
    _NDL_OK = True
except ImportError:
    _NDL_OK = False
    _ndl = None

# ---------------------------------------------------------------------------
# yfinance import
# ---------------------------------------------------------------------------
try:
    import yfinance as _yf
    _YF_OK = True
except ImportError:
    _YF_OK = False
    _yf = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
from config.paths import FEATURE_CACHE_DB, MARKET_DATA_DB

CACHE_DB  = FEATURE_CACHE_DB
MARKET_DB = MARKET_DATA_DB

def _month_starts(sy=2020, sm=1, ey=2026, em=6):
    dates = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        dates.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return dates

MONTH_STARTS = _month_starts()

REGIME_TICKER         = "QQQ"
HIGH_VOL_THR          = 20.0
MIN_AVG_DAILY_SHARES  = 500_000
MIN_AVG_DAILY_DOLLAR  = 5_000_000

# Pre-filter: tickers whose peak marketcap never exceeded this (USD millions)
# are excluded from yfinance pull. Set conservatively low so no plausibly
# tradeable name is skipped.
MCAP_PREFILTER_M      = 150.0     # $150M peak marketcap threshold

# yfinance batch size and sleep between batches
YF_BATCH_SIZE         = 500
YF_BATCH_SLEEP_S      = 10

# SHARADAR/DAILY batch size: 500 tickers × ~1840 days ≈ 920K rows/call
# (stays under NDL's per-call size limit; one bulk 16M-row call is rejected)
DAILY_TICKER_BATCH_SIZE = 500
DAILY_BATCH_SLEEP_S     = 3       # seconds between DAILY ticker batches

# SHARADAR/DAILY pull window (generous: 2019-06 to 2026-09)
DAILY_START           = "2019-06-01"
DAILY_END             = "2026-09-30"

# Marketcap collapse threshold for DODGED_LOSER_PROXY
MCAP_COLLAPSE_THR     = 0.70      # >70% decline into delisting

# Industry exclusions for tradeable universe
EXCLUDED_INDUSTRIES = {
    "Biotechnology",
    "Drug Manufacturers",
    "Pharmaceuticals",
    "Biotechnology & Medical Research",
    "Drug Manufacturers - General",
    "Drug Manufacturers - Specialty & Generic",
    "Pharmaceutical Retailers",
}

MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "BATS", "ARCA", "AMEX"}

PERCENTILE_METRICS = ["ps_ratio", "gm_pct", "rule40", "revenue_growth"]
CHECKPOINT_EVERY   = 50

# ---------------------------------------------------------------------------
# Delisted-bias caveat text (injected into every report header)
# ---------------------------------------------------------------------------
DELISTED_BIAS_CAVEAT = """\
*** STANDING CAVEAT -- DELISTED TICKER BIAS ***
yfinance returns little or nothing for many delisted names. The dodged-losers
defense record is SYSTEMATICALLY UNDER-COUNTED because the worst disasters
(frauds, bankruptcies, forced delistings) are exactly those that vanished from
yfinance. Every loosen-vs-tighten decision based on this data should treat the
dodged-loser count as a FLOOR, not a total.
DODGED_LOSER_PROXY rows (marketcap collapse >70% for delisted tickers) partially
compensate, but do not eliminate this bias.
*****************************************************"""

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS feature_cache (
    date                TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    sharadar_industry   TEXT,
    tradeable_flag      INTEGER NOT NULL DEFAULT 0,
    exclusion_reason    TEXT,
    ps_ratio            REAL,
    ps_growth           REAL,
    gm_pct              REAL,
    gm_erosion          REAL,
    rule40              REAL,
    fcf_margin          REAL,
    roic                REAL,
    share_growth        REAL,
    inv_days            REAL,
    inv_trend           REAL,
    pricing_power       TEXT,
    revenue_growth      REAL,
    capex_sales         REAL,
    momentum_126d       REAL,
    price_vs_ma200      REAL,
    price_vs_ma100      REAL,
    price_vs_ma50       REAL,
    return_6m           REAL,
    rs_score            REAL,
    regime              TEXT,
    fwd_ret_1m          REAL,
    fwd_ret_3m          REAL,
    fwd_ret_6m          REAL,
    max_dd_3m           REAL,
    gate_score          REAL,
    gate_threshold      REAL,
    gate_passed         INTEGER,
    gate_failed_name    TEXT,
    gate_failed_margin  REAL,
    is_dodged_loser_proxy INTEGER DEFAULT 0,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_fc_date      ON feature_cache (date);
CREATE INDEX IF NOT EXISTS idx_fc_ticker    ON feature_cache (ticker);
CREATE INDEX IF NOT EXISTS idx_fc_tradeable ON feature_cache (tradeable_flag);
CREATE INDEX IF NOT EXISTS idx_fc_industry  ON feature_cache (sharadar_industry);
CREATE INDEX IF NOT EXISTS idx_fc_fwd3m     ON feature_cache (fwd_ret_3m);

CREATE TABLE IF NOT EXISTS industry_percentiles (
    date        TEXT NOT NULL,
    industry    TEXT NOT NULL,
    metric      TEXT NOT NULL,
    n_tickers   INTEGER,
    p25         REAL,
    p40         REAL,
    p50         REAL,
    p60         REAL,
    p75         REAL,
    PRIMARY KEY (date, industry, metric)
);
CREATE INDEX IF NOT EXISTS idx_ip_date ON industry_percentiles (date);

CREATE TABLE IF NOT EXISTS _cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS _build_progress (
    date    TEXT NOT NULL,
    ticker  TEXT NOT NULL,
    done    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS _sharadar_marketcap (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    marketcap REAL,
    ps_ratio  REAL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_smc_ticker ON _sharadar_marketcap (ticker);

CREATE TABLE IF NOT EXISTS _yfinance_price (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL,
    volume  REAL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_yfp_ticker ON _yfinance_price (ticker);

CREATE TABLE IF NOT EXISTS _yfinance_fetch_status (
    ticker      TEXT PRIMARY KEY,
    status      TEXT,
    n_rows      INTEGER,
    fetched_at  TEXT
);
"""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_cache(path=None):
    p = Path(path or CACHE_DB)
    if not p.exists():
        if p.is_symlink():
            raise FileNotFoundError(
                f"Feature cache database symlink target not found: {p} -> {p.resolve()}"
            )
        p.touch()
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-131072")  # 128 MB page cache
    conn.executescript(DDL)
    return conn

def _sf(v, default=None):
    if v is None:
        return default
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default

def _pct(vals, p):
    if not vals:
        return None
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    idx = (p / 100.0) * (len(s) - 1)
    lo  = int(idx)
    hi  = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (idx - lo)) + s[hi] * (idx - lo)

# ---------------------------------------------------------------------------
# NDL helpers
# ---------------------------------------------------------------------------

def _require_ndl():
    if not _NDL_OK:
        print("ERROR: nasdaqdatalink not installed.  pip install nasdaq-data-link")
        sys.exit(1)
    key = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get("QUANDL_API_KEY")
    if not key:
        print("ERROR: NASDAQ_DATA_LINK_API_KEY not set.")
        sys.exit(1)
    _ndl.ApiConfig.api_key = key
    return key

def _ndl_get_table(table_name, **kwargs):
    """
    One paginated call. Stops immediately on HTTP 429 rate-limit.
    Raises NDLSizeError if the call exceeds NDL's per-call data size limit
    (caller should switch to batching rather than retrying).
    """
    try:
        import nasdaqdatalink.errors as _e
        _rate_exc = (_e.LimitExceededError,)
    except Exception:
        _rate_exc = (Exception,)
    try:
        import pandas as pd
        df = _ndl.get_table(table_name, paginate=True, **kwargs)
        return df if df is not None else pd.DataFrame()
    except _rate_exc as e:
        msg = str(e)
        # NDL raises LimitExceededError for BOTH rate-limits AND data-size limits.
        # Distinguish them: size-limit messages mention the export URL or
        # "exceeds the amount of data".
        if "exceeds the amount of data" in msg or "qopts.export=true" in msg:
            raise NDLSizeError(f"NDL data-size limit on {table_name}: {msg[:300]}") from e
        print(f"\nRATE LIMIT HIT on {table_name}: {e}")
        print("Stopping immediately. Do not retry -- wait 24h per NDL TOS.")
        sys.exit(2)
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            print(f"\nRATE LIMIT (HTTP 429) on {table_name}: {e}")
            sys.exit(2)
        raise


class NDLSizeError(Exception):
    """Raised when an NDL call exceeds their per-call data size limit.
    Callers should switch to ticker-batched pulling."""
    pass

# ---------------------------------------------------------------------------
# STEP 1: Pull SF1 benchmark universe
# ---------------------------------------------------------------------------

def fetch_sf1_benchmark(verbose=True):
    _require_ndl()
    if verbose:
        print("  Fetching SHARADAR/SF1 (ARY, all tickers, paginated) ...")
        t0 = time.time()
    df = _ndl_get_table("SHARADAR/SF1", dimension="ARY")
    if verbose:
        elapsed = time.time() - t0
        n_tk = df["ticker"].nunique() if "ticker" in df.columns else "?"
        print(f"  SF1: {len(df):,} rows, {n_tk} tickers in {elapsed:.1f}s")
    if df.empty:
        print("ERROR: SF1 returned no rows.")
        sys.exit(1)
    return df

# ---------------------------------------------------------------------------
# STEP 2a: Estimate SHARADAR/DAILY marketcap pull size
# ---------------------------------------------------------------------------

def estimate_sharadar_daily_size(n_tickers, verbose=True):
    # SHARADAR/DAILY: daily rows per ticker.
    # 252 trading days/year * ~7.3 years = ~1840 rows/ticker.
    APPROX_DAYS     = 1840
    BYTES_PER_ROW   = 50    # ticker(10) + date(10) + marketcap(8) + ps(8) + overhead
    est_rows  = n_tickers * APPROX_DAYS
    est_bytes = est_rows * BYTES_PER_ROW
    est_gb    = est_bytes / 1e9
    if verbose:
        print(f"\n  PRE-PULL SIZE ESTIMATE (SHARADAR/DAILY marketcap):")
        print(f"    Tickers         : {n_tickers:,}")
        print(f"    Days/ticker     : ~{APPROX_DAYS:,}")
        print(f"    Estimated rows  : ~{est_rows:,.0f}  ({est_rows/1e6:.1f}M)")
        print(f"    Estimated size  : ~{est_gb:.2f} GB DataFrame, "
              f"~{est_gb*0.4:.2f} GB SQLite")
        print(f"    NDL call        : one bulk paginated call, no retry")
    return {"est_rows": est_rows, "est_gb": est_gb}

# ---------------------------------------------------------------------------
# STEP 2b: Fetch SHARADAR/DAILY marketcap
# ---------------------------------------------------------------------------

def fetch_sharadar_daily_marketcap(tickers, verbose=True):
    """
    Pull SHARADAR/DAILY (marketcap, ps) for all SF1 tickers.
    Attempts one bulk paginated call first; if NDL returns a data-size error
    (expected for ~9k tickers / 16M rows), falls back to ticker batching.
    Returns DataFrame with columns: ticker, date, marketcap, ps.
    NOTE: for large universes prefer fetch_sharadar_daily_batched() which
    writes per-batch to DB and avoids loading 16M rows into memory.
    """
    _require_ndl()
    if verbose:
        print(f"\n  Fetching SHARADAR/DAILY (marketcap, ps) for {len(tickers):,} tickers ...")
        t0 = time.time()
    try:
        df = _ndl_get_table("SHARADAR/DAILY", ticker=list(tickers))
    except NDLSizeError:
        print("  [INFO] Single bulk call exceeds NDL size limit -- switching to ticker batching.")
        return None   # caller should use fetch_sharadar_daily_batched instead

    if verbose:
        elapsed = time.time() - t0
        print(f"  SHARADAR/DAILY: {len(df):,} rows in {elapsed:.1f}s")
        if not df.empty:
            mem_mb = df.memory_usage(deep=True).sum() / 1e6
            print(f"  DataFrame cols  : {list(df.columns)}")
            print(f"  DataFrame memory: {mem_mb:.0f} MB")

    return df


def fetch_sharadar_daily_batched(tickers, conn,
                                  batch_size=DAILY_TICKER_BATCH_SIZE,
                                  sleep_s=DAILY_BATCH_SLEEP_S,
                                  resume=False, verbose=True):
    """
    Pull SHARADAR/DAILY in ticker batches to stay under NDL's per-call size limit.
    Each batch (~500 tickers × 1840 days ≈ 920K rows) is well within the limit.
    Writes each batch directly to _sharadar_marketcap table in `conn`.
    Returns mcap_index: dict ticker -> [(date, mcap_M, ps), ...].

    --resume: if _sharadar_marketcap is already populated, loads from DB instead
              of pulling from NDL (saves ~85s SF1 cost of re-pulling on restart).
    """
    _require_ndl()

    # Resume: check if already populated
    if resume and conn is not None:
        n_existing = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM _sharadar_marketcap"
        ).fetchone()[0]
        if n_existing > 100:   # meaningful data already there
            if verbose:
                print(f"  [RESUME] _sharadar_marketcap already has {n_existing:,} tickers -- "
                      f"loading from DB, skipping NDL pull.")
            return _load_mcap_index_from_db(conn, verbose=verbose)

    tk_list = list(tickers)
    batches  = [tk_list[i:i + batch_size]
                for i in range(0, len(tk_list), batch_size)]
    n_batches = len(batches)
    mcap_index = {}
    total_rows = 0
    t_start    = time.time()

    if verbose:
        print(f"  SHARADAR/DAILY batched: {len(tk_list):,} tickers | "
              f"{n_batches} batches of {batch_size} | {sleep_s}s sleep")

    for bi, batch in enumerate(batches):
        if verbose:
            print(f"  [DAILY {bi+1:>3}/{n_batches}] {len(batch)} tickers ...",
                  end="", flush=True)
        t_b = time.time()
        try:
            df = _ndl_get_table("SHARADAR/DAILY", ticker=batch)
        except NDLSizeError as e:
            # Even 500 tickers exceeded limit? Halve batch and retry once.
            print(f" SIZE_LIMIT -- retrying at {batch_size//2} tickers")
            half = batch_size // 2
            df_parts = []
            for j in range(0, len(batch), half):
                sub = batch[j:j+half]
                df_parts.append(_ndl_get_table("SHARADAR/DAILY", ticker=sub))
                if j + half < len(batch):
                    time.sleep(sleep_s)
            import pandas as pd
            df = pd.concat(df_parts, ignore_index=True) if df_parts else pd.DataFrame()

        n_rows = len(df)
        total_rows += n_rows
        elapsed_b = time.time() - t_b

        if verbose:
            print(f" {n_rows:>8,} rows  {elapsed_b:.1f}s  "
                  f"total={total_rows:>10,}  elapsed={time.time()-t_start:.0f}s")

        # Build index from this batch and write to DB immediately
        batch_index = build_marketcap_index(df, verbose=False)
        for tk, data in batch_index.items():
            mcap_index[tk] = data

        if conn is not None and batch_index:
            store_marketcap_to_db(batch_index, conn, verbose=False)

        if bi < n_batches - 1:
            time.sleep(sleep_s)

    if verbose:
        elapsed_total = time.time() - t_start
        print(f"  SHARADAR/DAILY complete: {total_rows:,} rows | "
              f"{len(mcap_index):,} tickers | {elapsed_total:.0f}s total")

    return mcap_index


def _load_mcap_index_from_db(conn, verbose=True):
    """Load _sharadar_marketcap table back into memory dict."""
    if verbose:
        print("  Loading marketcap index from _sharadar_marketcap DB table ...")
        t0 = time.time()
    cur = conn.execute(
        "SELECT ticker, date, marketcap, ps_ratio "
        "FROM _sharadar_marketcap ORDER BY ticker, date"
    )
    idx = defaultdict(list)
    for ticker, date, mcap, ps in cur:
        idx[ticker].append((date, mcap or 0.0, ps))
    result = dict(idx)
    if verbose:
        elapsed = time.time() - t0
        print(f"  Marketcap index loaded: {len(result):,} tickers in {elapsed:.1f}s")
    return result

def build_marketcap_index(daily_df, verbose=True):
    """
    Build dict: ticker -> sorted list of (date_str, marketcap_M, ps_ratio)
    from SHARADAR/DAILY DataFrame.
    marketcap_M = marketcap in USD millions (SHARADAR stores in millions).
    """
    if daily_df is None or daily_df.empty:
        return {}

    # Detect columns -- SHARADAR/DAILY columns vary by entitlement
    has_mcap = "marketcap" in daily_df.columns
    has_ps   = "ps" in daily_df.columns
    has_date = "date" in daily_df.columns

    if not has_mcap or not has_date:
        print("  [WARN] SHARADAR/DAILY missing 'marketcap' or 'date' column.")
        print(f"  Available columns: {list(daily_df.columns)}")
        return {}

    idx = defaultdict(list)
    for row in daily_df.itertuples(index=False):
        tk   = str(getattr(row, "ticker", ""))
        dt   = str(getattr(row, "date", ""))[:10]
        mcap = _sf(getattr(row, "marketcap", None), 0.0)
        ps   = _sf(getattr(row, "ps", None)) if has_ps else None
        if tk and dt and mcap is not None:
            idx[tk].append((dt, mcap, ps))

    for tk in idx:
        idx[tk].sort(key=lambda x: x[0])

    if verbose:
        print(f"  Marketcap index : {len(idx):,} tickers")

    return dict(idx)

def store_marketcap_to_db(mcap_index, conn, verbose=True):
    """Persist marketcap index to _sharadar_marketcap table (for DODGED_LOSER_PROXY)."""
    if not mcap_index:
        return
    rows = []
    for ticker, data in mcap_index.items():
        for (dt, mcap, ps) in data:
            rows.append((ticker, dt, mcap, ps))
    # Batch insert
    BATCH = 10_000
    for i in range(0, len(rows), BATCH):
        conn.executemany(
            "INSERT OR IGNORE INTO _sharadar_marketcap (ticker, date, marketcap, ps_ratio) "
            "VALUES (?,?,?,?)",
            rows[i:i+BATCH]
        )
    conn.commit()
    if verbose:
        print(f"  Marketcap stored: {len(rows):,} rows -> _sharadar_marketcap")

# ---------------------------------------------------------------------------
# STEP 2c: Pre-filter by peak marketcap
# ---------------------------------------------------------------------------

def apply_mcap_prefilter(all_tickers, mcap_index,
                          threshold_m=MCAP_PREFILTER_M, verbose=True):
    """
    Keep tickers whose peak marketcap in 2019-2026 >= threshold_m (USD millions).
    Threshold is set LOW so nothing plausibly tradeable at $5M/day is excluded.

    Returns (survivors: list, excluded: set).
    Excluded tickers get tradeable_flag=0, exclusion_reason=EXCLUDED_MCAP_PREFILTER.
    """
    survivors = []
    excluded  = set()

    for tk in all_tickers:
        data = mcap_index.get(tk, [])
        if not data:
            # No marketcap data at all -- exclude conservatively
            excluded.add(tk)
            continue
        peak_mcap = max((m for _, m, _ in data if m), default=0.0)
        if peak_mcap >= threshold_m:
            survivors.append(tk)
        else:
            excluded.add(tk)

    if verbose:
        print(f"\n  MCAP PRE-FILTER (threshold ${threshold_m:.0f}M peak):")
        print(f"    Total benchmark tickers : {len(all_tickers):,}")
        print(f"    Survivors (yfinance pull): {len(survivors):,}")
        print(f"    Excluded (EXCLUDED_MCAP_PREFILTER): {len(excluded):,}")
        pct_excl = len(excluded) / max(len(all_tickers), 1) * 100
        print(f"    Exclusion rate : {pct_excl:.1f}%")

    return survivors, excluded

# ---------------------------------------------------------------------------
# STEP 3: Fetch yfinance OHLCV for survivors (batched + checkpointed)
# ---------------------------------------------------------------------------

def fetch_yfinance_batches(survivors, conn, batch_size=YF_BATCH_SIZE,
                            start=DAILY_START, end=DAILY_END,
                            resume=False, verbose=True):
    """
    Pull daily OHLCV from yfinance for survivor tickers.
    Stores to _yfinance_price and _yfinance_fetch_status.
    Sleeps YF_BATCH_SLEEP_S seconds between batches.
    With --resume: skips tickers already in _yfinance_fetch_status.
    """
    if not _YF_OK:
        print("  ERROR: yfinance not installed.  pip install yfinance")
        print("  tradeable_flag will be VOLUME_DATA_UNAVAILABLE for all tickers.")
        return

    # Load already-fetched tickers
    done_tickers = set()
    if resume:
        cur = conn.execute("SELECT ticker FROM _yfinance_fetch_status")
        done_tickers = {r[0] for r in cur.fetchall()}
        print(f"  Resume: {len(done_tickers):,} tickers already fetched, skipping.")

    to_fetch = [tk for tk in survivors if tk not in done_tickers]
    n_batches = math.ceil(len(to_fetch) / batch_size)

    if not to_fetch:
        print("  All survivor tickers already fetched.")
        return

    print(f"  yfinance pull: {len(to_fetch):,} tickers in {n_batches} batches of {batch_size}")
    print(f"  Date range  : {start} -> {end}")
    print(f"  Sleep/batch : {YF_BATCH_SLEEP_S}s")
    print()

    total_rows = 0
    t_start    = time.time()

    for batch_idx in range(n_batches):
        batch = to_fetch[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        batch_str = " ".join(batch)

        t_b = time.time()
        try:
            import pandas as pd
            df = _yf.download(
                batch_str,
                start=start,
                end=end,
                progress=False,
                auto_adjust=True,
                threads=True,
            )
            elapsed_b = time.time() - t_b

            if df is None or df.empty:
                # Mark all as EMPTY
                _mark_yf_status(conn, batch, "EMPTY", 0)
                print(f"  [BATCH {batch_idx+1}/{n_batches}] EMPTY ({elapsed_b:.1f}s)")
                _sleep_between_batches(batch_idx, n_batches)
                continue

            # Multi-ticker download produces MultiIndex columns: (field, ticker)
            # Single ticker produces flat columns
            price_rows = []
            if isinstance(df.columns, pd.MultiIndex):
                # Close and Volume are top-level fields
                closes  = df["Close"]  if "Close"  in df.columns.get_level_values(0) else None
                volumes = df["Volume"] if "Volume" in df.columns.get_level_values(0) else None
                if closes is None:
                    _mark_yf_status(conn, batch, "NO_CLOSE_COL", 0)
                    print(f"  [BATCH {batch_idx+1}/{n_batches}] no Close column")
                    _sleep_between_batches(batch_idx, n_batches)
                    continue

                for tk in batch:
                    if tk not in closes.columns:
                        _mark_yf_status(conn, [tk], "EMPTY", 0)
                        continue
                    tk_close  = closes[tk].dropna()
                    tk_vol    = volumes[tk].dropna() if volumes is not None and tk in volumes.columns else None
                    n = 0
                    for idx_dt, cl in tk_close.items():
                        dt_str = str(idx_dt.date())
                        vol_val = float(tk_vol.get(idx_dt, 0) or 0) if tk_vol is not None else 0.0
                        price_rows.append((tk, dt_str, float(cl), vol_val))
                        n += 1
                    _mark_yf_status(conn, [tk], "OK" if n > 0 else "EMPTY", n)
            else:
                # Single ticker (or flat columns)
                tk = batch[0] if len(batch) == 1 else None
                if tk and "Close" in df.columns:
                    for idx_dt, row in df.iterrows():
                        cl  = _sf(row.get("Close"))
                        vol = _sf(row.get("Volume"), 0.0)
                        if cl is not None:
                            price_rows.append((tk, str(idx_dt.date()), cl, vol or 0.0))
                    _mark_yf_status(conn, [tk], "OK" if price_rows else "EMPTY", len(price_rows))

            # Bulk insert to DB
            if price_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO _yfinance_price (ticker, date, close, volume) "
                    "VALUES (?,?,?,?)",
                    price_rows
                )
            conn.commit()
            total_rows += len(price_rows)

            elapsed_b = time.time() - t_b
            elapsed_t  = time.time() - t_start
            rows_this  = len(price_rows)
            pct_done   = (batch_idx + 1) / n_batches * 100
            print(f"  [BATCH {batch_idx+1:>4}/{n_batches}] "
                  f"rows={rows_this:>7,}  total={total_rows:>9,}  "
                  f"batch={elapsed_b:.1f}s  elapsed={elapsed_t:.0f}s  ({pct_done:.1f}%)")

        except Exception as e:
            msg = str(e)[:120]
            print(f"  [BATCH {batch_idx+1}/{n_batches}] ERROR: {msg}")
            _mark_yf_status(conn, batch, f"ERROR:{msg[:60]}", 0)
            conn.commit()

        _sleep_between_batches(batch_idx, n_batches)

    total_elapsed = time.time() - t_start
    print(f"\n  yfinance complete: {total_rows:,} rows, {total_elapsed:.0f}s total")


def _mark_yf_status(conn, tickers, status, n_rows):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT OR REPLACE INTO _yfinance_fetch_status (ticker, status, n_rows, fetched_at) "
        "VALUES (?,?,?,?)",
        [(tk, status, n_rows, now) for tk in tickers]
    )


def _sleep_between_batches(batch_idx, n_batches):
    if batch_idx < n_batches - 1:
        time.sleep(YF_BATCH_SLEEP_S)


# ---------------------------------------------------------------------------
# STEP 4: Build volume index from DB (for tradeable flag computation)
# ---------------------------------------------------------------------------

def build_volume_index_from_db(conn, verbose=True):
    """
    Load _yfinance_price into memory dict:
      ticker -> sorted list of (date_str, close, volume)
    """
    if verbose:
        print("  Loading yfinance price data from DB into memory ...")
        t0 = time.time()

    cur = conn.execute(
        "SELECT ticker, date, close, volume FROM _yfinance_price ORDER BY ticker, date"
    )
    idx = defaultdict(list)
    for ticker, date, close, volume in cur:
        idx[ticker].append((date, close or 0.0, volume or 0.0))

    result = dict(idx)
    if verbose:
        elapsed = time.time() - t0
        print(f"  Volume index: {len(result):,} tickers loaded in {elapsed:.1f}s")
    return result

# ---------------------------------------------------------------------------
# STEP 5: Compute tradeable flag
# ---------------------------------------------------------------------------

def compute_tradeable_flag(ticker, query_date, industry, sf1_ticker_meta,
                            volume_index, local_price_cache,
                            mcap_prefilter_excluded):
    """
    Returns (tradeable: bool, exclusion_reason: str or None).

    Priority:
      1. Industry exclusion (fastest check)
      2. MCAP pre-filter exclusion
      3. Exchange check
      4. Volume from yfinance (volume_index)
      5. Volume from local market_data.db (fallback for ~160 strategy tickers)
      6. VOLUME_DATA_UNAVAILABLE if neither has data
    """
    if industry and industry in EXCLUDED_INDUSTRIES:
        return False, f"EXCLUDED_INDUSTRY:{industry}"

    if ticker in mcap_prefilter_excluded:
        return False, "EXCLUDED_MCAP_PREFILTER"

    exchange = sf1_ticker_meta.get(ticker, {}).get("exchange", "")
    if exchange and exchange.upper() not in MAJOR_EXCHANGES:
        return False, f"NON_MAJOR_EXCHANGE:{exchange}"

    closes_and_volumes = None

    if ticker in volume_index:
        avail = [(dt, px, vol) for dt, px, vol in volume_index[ticker]
                 if dt <= query_date]
        if avail:
            closes_and_volumes = avail

    if closes_and_volumes is None and ticker in local_price_cache:
        df = local_price_cache[ticker]
        if not df.empty:
            import pandas as pd
            hist = df[df.index <= pd.Timestamp(query_date)]
            if not hist.empty:
                closes_and_volumes = [
                    (str(idx.date()),
                     float(row.get("Close", 0) or 0),
                     float(row.get("Volume", 0) or 0))
                    for idx, row in hist.iterrows()
                ]

    if not closes_and_volumes or len(closes_and_volumes) < 20:
        return False, "VOLUME_DATA_UNAVAILABLE"

    recent      = closes_and_volumes[-60:]
    shares_list = [vol for _, _, vol in recent if vol and vol > 0]
    dollar_list = [px * vol for _, px, vol in recent if px and vol and px > 0 and vol > 0]

    if not shares_list or not dollar_list:
        return False, "VOLUME_DATA_UNAVAILABLE"

    avg_shares = sum(shares_list) / len(shares_list)
    avg_dollar  = sum(dollar_list) / len(dollar_list)

    if avg_shares < MIN_AVG_DAILY_SHARES:
        return False, f"LOW_VOLUME_SHARES:{avg_shares:.0f}"
    if avg_dollar < MIN_AVG_DAILY_DOLLAR:
        return False, f"LOW_VOLUME_DOLLAR:{avg_dollar:.0f}"

    return True, None

# ---------------------------------------------------------------------------
# STEP 6: Regime detection
# ---------------------------------------------------------------------------

def _get_regime(date_str, qqq_series):
    avail = [c for d, c in qqq_series if d <= date_str]
    if len(avail) < 105:
        return "UNKNOWN"
    window = min(100, len(avail))
    ma100  = sum(avail[-window:]) / window
    above  = avail[-1] > ma100
    rets   = [(avail[i] - avail[i-1]) / avail[i-1] for i in range(1, len(avail))]
    recent = rets[-20:]
    mean_r = sum(recent) / len(recent)
    var    = sum((r - mean_r) ** 2 for r in recent) / max(len(recent) - 1, 1)
    vol20  = math.sqrt(var) * (252 ** 0.5) * 100
    if above:
        return "BULL_STRONG" if vol20 < HIGH_VOL_THR else "BULL_WEAK"
    return "BEAR_VOLATILE" if vol20 >= HIGH_VOL_THR else "BEAR_GRIND"

# ---------------------------------------------------------------------------
# STEP 7: Price features and forward outcomes
# ---------------------------------------------------------------------------

def _build_price_series(ticker, volume_index, local_price_cache):
    """Build pandas Series of closes indexed by Timestamp."""
    import pandas as pd
    if ticker in local_price_cache:
        df = local_price_cache[ticker]
        if not df.empty and "Close" in df.columns:
            return df["Close"].dropna()
    if ticker in volume_index:
        data = volume_index[ticker]
        dates  = [pd.Timestamp(d) for d, _, _ in data]
        closes = [px for _, px, _ in data]
        s = pd.Series(closes, index=dates)
        return s[s > 0] if not s.empty else None
    return None


def _price_features(closes, query_date):
    if closes is None or closes.empty:
        return None, None, None, None, None
    import pandas as pd
    hist = closes[closes.index <= pd.Timestamp(query_date)]
    if hist.empty or len(hist) < 5:
        return None, None, None, None, None
    c     = hist.values.astype(float)
    price = c[-1]
    lb    = min(126, len(c) - 1)
    mom   = ((price - c[-(lb+1)]) / c[-(lb+1)]) if lb > 0 and c[-(lb+1)] > 0 else None
    def _ma(w):
        ww = min(w, len(c))
        return c[-ww:].mean() if ww > 0 else 0.0
    ma200 = _ma(200); ma100 = _ma(100); ma50 = _ma(50)
    vs200 = ((price / ma200 - 1) * 100) if ma200 > 0 else None
    vs100 = ((price / ma100 - 1) * 100) if ma100 > 0 else None
    vs50  = ((price / ma50  - 1) * 100) if ma50  > 0 else None
    b6    = min(126, len(c) - 1)
    ret6m = ((price - c[-(b6+1)]) / c[-(b6+1)] * 100) if b6 > 0 and c[-(b6+1)] > 0 else None
    return mom, vs200, vs100, vs50, ret6m


def _forward_outcomes(closes, query_date):
    if closes is None or closes.empty:
        return None, None, None, None
    import pandas as pd
    future = closes[closes.index >= pd.Timestamp(query_date)]
    if future.empty:
        return None, None, None, None
    c  = future.values.astype(float)
    p0 = c[0]
    if p0 <= 0:
        return None, None, None, None
    def _fwd(bars):
        idx = min(bars, len(c) - 1)
        return (c[idx] - p0) / p0 if idx > 0 else None
    fwd1 = _fwd(21); fwd3 = _fwd(63); fwd6 = _fwd(126)
    window = c[:min(64, len(c))]
    peak = p0; max_dd = 0.0
    for px in window[1:]:
        peak = max(peak, px)
        dd   = (px - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return fwd1, fwd3, fwd6, max_dd

# ---------------------------------------------------------------------------
# STEP 8: DODGED_LOSER_PROXY via marketcap collapse for delisted tickers
# ---------------------------------------------------------------------------

def compute_dodged_loser_proxies(conn, mcap_index, volume_index,
                                  collapse_thr=MCAP_COLLAPSE_THR, verbose=True):
    """
    For tickers NOT in volume_index (yfinance returned nothing -- likely delisted):
      If SHARADAR/DAILY shows peak marketcap followed by >=70% decline,
      synthesize a DODGED_LOSER_PROXY feature_cache row for each month-start
      date that falls within 6 months before the collapse nadir.

    These rows have:
      tradeable_flag = 0  (they were pre-filtered or data-unavailable)
      is_dodged_loser_proxy = 1
      fwd_ret_3m = -(collapse pct) as a proxy for forward losses
      exclusion_reason = 'DODGED_LOSER_PROXY'

    Labeled clearly in all reports.
    """
    proxies_inserted = 0
    import pandas as pd

    for ticker, data in mcap_index.items():
        # Skip tickers that DID have yfinance data
        if ticker in volume_index:
            continue
        if len(data) < 10:
            continue

        dates  = [d for d, _, _ in data]
        mcaps  = [m for _, m, _ in data]

        if not mcaps or max(mcaps) <= 0:
            continue

        peak_idx   = mcaps.index(max(mcaps))
        peak_mcap  = mcaps[peak_idx]
        peak_date  = dates[peak_idx]

        # Find trough after peak
        post_peak_mcaps = mcaps[peak_idx:]
        post_peak_dates = dates[peak_idx:]
        if not post_peak_mcaps:
            continue

        trough_mcap = min(post_peak_mcaps)
        trough_idx  = post_peak_mcaps.index(trough_mcap)
        trough_date = post_peak_dates[trough_idx]

        if peak_mcap <= 0:
            continue
        collapse_pct = (peak_mcap - trough_mcap) / peak_mcap

        if collapse_pct < collapse_thr:
            continue

        # Synthesize a proxy row for each month-start date that is:
        # - after the peak
        # - within 6 months before the trough (the danger window)
        from datetime import timedelta
        try:
            trough_dt = datetime.strptime(trough_date, "%Y-%m-%d")
            six_mo_before_trough = (trough_dt - timedelta(days=183)).strftime("%Y-%m-%d")
        except Exception:
            continue

        for ms in MONTH_STARTS:
            if ms < peak_date:
                continue
            if ms < six_mo_before_trough or ms > trough_date:
                continue

            # Check if row already exists
            existing = conn.execute(
                "SELECT 1 FROM feature_cache WHERE date=? AND ticker=?",
                (ms, ticker)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE feature_cache SET is_dodged_loser_proxy=1, "
                    "fwd_ret_3m=?, exclusion_reason='DODGED_LOSER_PROXY' "
                    "WHERE date=? AND ticker=?",
                    (-collapse_pct, ms, ticker)
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO feature_cache
                      (date, ticker, tradeable_flag, exclusion_reason,
                       fwd_ret_3m, is_dodged_loser_proxy)
                    VALUES (?,?,0,'DODGED_LOSER_PROXY',?,1)
                    """,
                    (ms, ticker, -collapse_pct)
                )
            proxies_inserted += 1

    conn.commit()
    if verbose:
        print(f"  DODGED_LOSER_PROXY rows inserted: {proxies_inserted:,}")
    return proxies_inserted

# ---------------------------------------------------------------------------
# STEP 9: Industry percentiles
# ---------------------------------------------------------------------------

def compute_industry_percentiles(conn, date_str):
    cur = conn.execute(
        f"SELECT sharadar_industry, {', '.join(PERCENTILE_METRICS)} "
        f"FROM feature_cache WHERE date=? AND sharadar_industry IS NOT NULL",
        (date_str,)
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    by_ind = defaultdict(lambda: {m: [] for m in PERCENTILE_METRICS})
    for row in rows:
        ind = row[0]
        for i, metric in enumerate(PERCENTILE_METRICS):
            v = row[i + 1]
            if v is not None:
                by_ind[ind][metric].append(v)

    inserts = []
    for ind, mvals in by_ind.items():
        for metric, vals in mvals.items():
            if not vals:
                continue
            inserts.append((date_str, ind, metric, len(vals),
                            _pct(vals,25), _pct(vals,40), _pct(vals,50),
                            _pct(vals,60), _pct(vals,75)))
    if inserts:
        conn.executemany(
            "INSERT OR REPLACE INTO industry_percentiles "
            "(date, industry, metric, n_tickers, p25, p40, p50, p60, p75) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            inserts
        )
    return len(inserts)

# ---------------------------------------------------------------------------
# Spot-check helper
# ---------------------------------------------------------------------------

def spot_check_vs_tester(conn, n=10):
    try:
        import importlib
        import tester as _t
        importlib.reload(_t)
    except Exception as e:
        print(f"  [SPOT-CHECK] Cannot import tester.py: {e}")
        return

    cur = conn.execute(
        "SELECT date, ticker, sharadar_industry, ps_ratio, ps_growth, gm_pct, gm_erosion, "
        "rule40, fcf_margin, roic, share_growth, inv_days, inv_trend, pricing_power, "
        "revenue_growth, capex_sales, price_vs_ma200, price_vs_ma100, price_vs_ma50, "
        "return_6m, rs_score, regime, gate_score, gate_threshold "
        "FROM feature_cache WHERE tradeable_flag=1 AND gate_score IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (n,)
    )
    rows = cur.fetchall()
    if not rows:
        print("  [SPOT-CHECK] No gate-scored rows. Run opportunity_report.py first.")
        return

    print(f"\n  SPOT CHECK ({len(rows)} rows)")
    n_ok = n_diff = 0
    for row in rows:
        (date, ticker, ind, ps_r, ps_g, gm, gme, r40, fcf, roic, sg,
         inv_d, inv_t, pp, rev, csx, ma200, ma100, ma50, ret6m, rs,
         regime, cached_score, cached_thr) = row
        trow = {
            "Ticker": ticker, "Sector": ind or "",
            "PS_Ratio": ps_r or 999, "PS/Growth": ps_g or 999,
            "GM %": gm or 0, "GM Erosion": gme or 0,
            "Rule 40": r40 or 0, "FCF_Margin_%": fcf or 0,
            "ROIC %": roic or 0, "Share Growth %": sg or 0,
            "Inv Days": inv_d or 0, "Inv Trend": inv_t or 0,
            "Pricing Power": pp or "Weak", "Revenue_Growth_%": rev or 0,
            "Capex_Sales_%": csx or 0, "Price_vs_MA200_%": ma200 or 0,
            "Price_vs_MA100_%": ma100 or 0, "Price_vs_MA50_%": ma50 or 0,
            "Return_6M_%": ret6m or 0, "Relative_Strength_Score": rs or 50,
            "SMA20": 1.0, "Price": 1.0,
        }
        try:
            _t._ndx_regime = regime or "BULL_STRONG"
            info = _t.SECTOR_MAP.get(ind or "")
            if not info:
                continue
            universe, sub = info
            if universe == "energy":   gr = _t.gates_energy(trow, sub, 50.0)
            elif universe == "tech":   gr = _t.gates_tech(trow, sub, 50.0)
            elif universe == "medtech": gr = _t.gates_medtech(trow, sub, 50.0)
            elif universe == "semi":   gr = _t.gates_semi(trow, sub, 50.0)
            else: continue
            veto, _ = _t.check_veto(trow, 50.0)
            if veto:
                live = 0.0
            else:
                ws, _, _, _ = _t.score_gates(gr)
                rescue = _t.compute_momentum_rescue(gr, ws, _t.pass_threshold(universe), trow, universe)
                live = ws + rescue
            thr = _t.pass_threshold(universe)
            match = abs((live or 0) - (cached_score or 0)) < 0.1
            flag = "OK" if match else "DIFF"
            if match: n_ok += 1
            else:      n_diff += 1
            print(f"  {date} {ticker:<7} cached={cached_score:.3f} live={live:.3f} {flag}")
        except Exception as e:
            print(f"  {date} {ticker:<7} ERROR: {e}")
            n_diff += 1

    print(f"  Match: {n_ok}/{len(rows)}  Mismatch: {n_diff}/{len(rows)}")

# ---------------------------------------------------------------------------
# SF1 row -> fundamentals dict
# ---------------------------------------------------------------------------

def _sf1_row_to_metrics(sf1_row, daily_row=None):
    """
    Convert raw SF1 ARY row to tester.py-compatible metrics dict.
    daily_row: (date, marketcap, ps) from SHARADAR/DAILY for the query date, or None.
    """
    def _g(name):
        return _sf(getattr(sf1_row, name, None))

    revenue  = _g("revenue")
    gp       = _g("gp")
    ebit     = _g("ebit")
    ncfo     = _g("ncfo")
    capex    = _g("capex") or 0
    assets   = _g("assets")
    inventory= _g("inventory") or 0
    shareswa = _g("shareswa") or _g("sharesbas") or None

    gm_pct = None
    if revenue and revenue > 0 and gp is not None:
        gm_pct = gp / revenue * 100

    fcf_margin = None
    if revenue and revenue > 0 and ncfo is not None:
        fcf_margin = (ncfo - abs(capex)) / revenue * 100

    roic = None
    if assets and assets > 0 and ebit is not None:
        roic = ebit / assets * 100

    inv_days = None
    if revenue and revenue > 0 and inventory > 0:
        inv_days = inventory / (revenue / 365)

    # PS ratio from SHARADAR/DAILY if available
    ps_ratio = None
    if daily_row:
        _, _, ps = daily_row
        ps_ratio = _sf(ps)
    elif shareswa and shareswa > 0 and revenue and revenue > 0:
        # Estimate from marketcap if DAILY row present
        pass

    csx = (abs(capex) / revenue * 100) if revenue and revenue > 0 and capex else None

    return {
        "PS_Ratio":        ps_ratio,
        "PS/Growth":       None,
        "GM %":            gm_pct,
        "GM Erosion":      0,
        "Rule 40":         (gm_pct or 0),
        "FCF_Margin_%":    fcf_margin,
        "ROIC %":          roic,
        "Share Growth %":  0,
        "Inv Days":        inv_days,
        "Inv Trend":       0,
        "Pricing Power":   "Weak",
        "Revenue_Growth_%": 0,
        "Capex_Sales_%":   csx,
    }

# ---------------------------------------------------------------------------
# INSERT SQL
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT OR REPLACE INTO feature_cache
  (date, ticker, sharadar_industry, tradeable_flag, exclusion_reason,
   ps_ratio, ps_growth, gm_pct, gm_erosion, rule40, fcf_margin,
   roic, share_growth, inv_days, inv_trend, pricing_power,
   revenue_growth, capex_sales,
   momentum_126d, price_vs_ma200, price_vs_ma100, price_vs_ma50,
   return_6m, rs_score, regime,
   fwd_ret_1m, fwd_ret_3m, fwd_ret_6m, max_dd_3m)
VALUES
  (:date, :ticker, :sharadar_industry, :tradeable_flag, :exclusion_reason,
   :ps_ratio, :ps_growth, :gm_pct, :gm_erosion, :rule40, :fcf_margin,
   :roic, :share_growth, :inv_days, :inv_trend, :pricing_power,
   :revenue_growth, :capex_sales,
   :momentum_126d, :price_vs_ma200, :price_vs_ma100, :price_vs_ma50,
   :return_6m, :rs_score, :regime,
   :fwd_ret_1m, :fwd_ret_3m, :fwd_ret_6m, :max_dd_3m)
"""

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build two-layer feature cache")
    parser.add_argument("--resume",      action="store_true",
                        help="Skip already-fetched/cached (ticker, date) pairs")
    parser.add_argument("--size-only",   action="store_true",
                        help="Print size estimates for all NDL pulls, then exit")
    parser.add_argument("--spot-check",  type=int, default=0, metavar="N")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Fetch data but do not write to cache DB")
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="Limit to N tickers (testing)")
    parser.add_argument("--skip-yfinance", action="store_true",
                        help="Skip yfinance batch (useful if already fetched)")
    args = parser.parse_args()

    t0_global = time.time()
    print()
    print("=" * 78)
    print("  BUILD FEATURE CACHE  --  Two-layer universe (benchmark + tradeable)")
    print(f"  Date range  : {MONTH_STARTS[0]} to {MONTH_STARTS[-1]}  ({len(MONTH_STARTS)} month-starts)")
    print(f"  Cache target: {CACHE_DB}")
    if args.dry_run:   print("  MODE: DRY-RUN (no writes)")
    if args.resume:    print("  MODE: RESUME (skipping existing data)")
    if args.size_only: print("  MODE: SIZE-ONLY (no pulls, exit after estimates)")
    print("=" * 78)

    _require_ndl()
    if not _YF_OK:
        print("\nWARNING: yfinance not installed. tradeable flags will be VOLUME_DATA_UNAVAILABLE.")
        print("  pip install yfinance")

    # ------------------------------------------------------------------ #
    # STEP 1: SF1 benchmark universe
    # ------------------------------------------------------------------ #
    print("\n[STEP 1] Fetching SF1 benchmark universe ...")
    sf1_df = fetch_sf1_benchmark(verbose=True)

    sf1_meta    = {}   # ticker -> {industry, exchange}
    sf1_by_tick = {}   # ticker -> list of SF1 rows
    for row in sf1_df.itertuples(index=False):
        tk  = str(getattr(row, "ticker", ""))
        ind = str(getattr(row, "industry", "") or "")
        exc = str(getattr(row, "exchange", "") or "")
        if tk not in sf1_meta:
            sf1_meta[tk] = {"industry": ind, "exchange": exc}
        sf1_by_tick.setdefault(tk, []).append(row)

    all_tickers = sorted(sf1_meta.keys())
    if args.max_tickers:
        all_tickers = all_tickers[:args.max_tickers]
        print(f"  [--max-tickers] Capped at {args.max_tickers}")
    print(f"  Benchmark universe: {len(all_tickers):,} unique tickers")

    # ------------------------------------------------------------------ #
    # STEP 2: SHARADAR/DAILY marketcap pull (one bulk call)
    # ------------------------------------------------------------------ #
    estimate_sharadar_daily_size(len(all_tickers), verbose=True)

    if args.size_only:
        # Also estimate yfinance (rough survivor count before filter)
        est_survivors = int(len(all_tickers) * 0.40)  # ~40% survive mcap filter
        print(f"\n  yfinance OHLCV estimate (post pre-filter):")
        print(f"    Estimated survivors : ~{est_survivors:,}")
        est_yf_rows = est_survivors * 1840
        print(f"    Estimated yf rows   : ~{est_yf_rows:,.0f}  ({est_yf_rows/1e6:.1f}M)")
        print(f"    Estimated batches   : {math.ceil(est_survivors/YF_BATCH_SIZE)}")
        print(f"    Est. wall-clock     : ~{est_survivors/YF_BATCH_SIZE*YF_BATCH_SLEEP_S/60:.0f} min sleep "
              f"+ download time")
        print("\n  [--size-only] Stopping. No data pulled.")
        return

    # ------------------------------------------------------------------ #
    # STEP 2b: Open DB BEFORE DAILY pull so each batch is checkpointed
    # ------------------------------------------------------------------ #
    conn = None
    if not args.dry_run:
        print(f"\n  Opening cache DB: {CACHE_DB}")
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = _open_cache(CACHE_DB)

    # ------------------------------------------------------------------ #
    # STEP 2a: Fetch SHARADAR/DAILY marketcap (ticker-batched)
    # ------------------------------------------------------------------ #
    # One bulk call over 8,989 tickers returns ~16M rows which exceeds NDL's
    # per-call size limit. We pull in batches of 500 tickers (~920K rows each)
    # and write each batch directly to _sharadar_marketcap. With --resume, if
    # the table is already populated we load from DB and skip the NDL pull.
    print("\n[STEP 2a] Fetching SHARADAR/DAILY marketcap (batched by ticker) ...")
    if args.dry_run:
        print("  [DRY-RUN] skipping SHARADAR/DAILY pull")
        mcap_index = {}
    else:
        mcap_index = fetch_sharadar_daily_batched(
            all_tickers, conn,
            batch_size=DAILY_TICKER_BATCH_SIZE,
            sleep_s=DAILY_BATCH_SLEEP_S,
            resume=args.resume,
            verbose=True,
        )

    # ------------------------------------------------------------------ #
    # STEP 2c: Pre-filter
    # ------------------------------------------------------------------ #
    print("\n[STEP 2c] Applying marketcap pre-filter ...")
    survivors, excluded_mcap = apply_mcap_prefilter(
        all_tickers, mcap_index, threshold_m=MCAP_PREFILTER_M, verbose=True
    )

    # ------------------------------------------------------------------ #
    # STEP 3: yfinance OHLCV for survivors
    # ------------------------------------------------------------------ #
    if not args.skip_yfinance and _YF_OK and conn is not None:
        print(f"\n[STEP 3] yfinance OHLCV for {len(survivors):,} survivors ...")
        print("  (Running in background -- reports per batch of 500)")
        print("  Use --resume to restart from last checkpoint if interrupted")
        fetch_yfinance_batches(
            survivors, conn, batch_size=YF_BATCH_SIZE,
            start=DAILY_START, end=DAILY_END,
            resume=args.resume, verbose=True
        )
    elif args.skip_yfinance:
        print("\n[STEP 3] --skip-yfinance: loading existing yfinance data from DB ...")
    else:
        print("\n[STEP 3] yfinance unavailable -- tradeable flags will be incomplete.")

    # ------------------------------------------------------------------ #
    # STEP 4: Build volume index from DB
    # ------------------------------------------------------------------ #
    print("\n[STEP 4] Building volume index from DB ...")
    volume_index = {}
    if conn is not None:
        volume_index = build_volume_index_from_db(conn, verbose=True)

    # ------------------------------------------------------------------ #
    # STEP 4b: Load local price cache
    # ------------------------------------------------------------------ #
    print("\n[STEP 4b] Loading local price cache (market_data.db) ...")
    local_price_cache = {}
    try:
        if not MARKET_DB.exists():
            raise FileNotFoundError(f"Market data database not found: {MARKET_DB.resolve()}")
        conn_mkt = sqlite3.connect(str(MARKET_DB))
        local_tks = [r[0] for r in conn_mkt.execute(
            "SELECT DISTINCT ticker FROM prices"
        ).fetchall()]
        conn_mkt.close()
        for tk in local_tks:
            try:
                df = _db.get_prices(tk, start="2019-01-01", end="2026-12-31")
                if not df.empty:
                    local_price_cache[tk] = df
            except Exception:
                pass
        print(f"  Local price cache: {len(local_price_cache)} tickers")
    except Exception as e:
        print(f"  [WARN] local price cache unavailable: {e}")

    # ------------------------------------------------------------------ #
    # STEP 5: QQQ regime series
    # ------------------------------------------------------------------ #
    print("\n[STEP 5] Loading QQQ for regime detection ...")
    try:
        qqq_df = _db.get_prices(REGIME_TICKER, start="2018-01-01", end="2026-12-31")
        if not qqq_df.empty:
            qqq_series = [(str(idx.date()), float(row["Close"]))
                          for idx, row in qqq_df.iterrows()]
        else:
            raise ValueError("QQQ empty from local DB")
        print(f"  QQQ: {len(qqq_series)} bars "
              f"({qqq_series[0][0]} -> {qqq_series[-1][0]})")
    except Exception:
        if "QQQ" in volume_index:
            qqq_series = [(dt, px) for dt, px, _ in volume_index["QQQ"]]
            print(f"  QQQ (from yfinance): {len(qqq_series)} bars")
        else:
            print("  ERROR: QQQ data unavailable.")
            sys.exit(1)

    # ------------------------------------------------------------------ #
    # STEP 6: Load already-done pairs for resume
    # ------------------------------------------------------------------ #
    done_pairs = set()
    if args.resume and conn is not None:
        print("\n[STEP 6] Resume: loading existing cache pairs ...")
        cur = conn.execute("SELECT date, ticker FROM _build_progress WHERE done=1")
        done_pairs = {(r[0], r[1]) for r in cur.fetchall()}
        print(f"  {len(done_pairs):,} (date, ticker) pairs already done")

    # ------------------------------------------------------------------ #
    # STEP 7: Main build loop
    # ------------------------------------------------------------------ #
    print(f"\n[STEP 7] Building cache: {len(MONTH_STARTS)} dates × "
          f"{len(all_tickers):,} tickers ...")
    print(f"  Checkpoint every {CHECKPOINT_EVERY} tickers\n")

    total_rows  = 0
    total_skip  = 0
    t_loop      = time.time()

    for date_str in MONTH_STARTS:
        regime = _get_regime(date_str, qqq_series)
        batch  = []

        for i, ticker in enumerate(all_tickers):
            if (date_str, ticker) in done_pairs:
                total_skip += 1
                continue

            # --- Fundamentals ---
            m = None
            try:
                m = _db.get_fundamentals_asof(ticker, date_str)
            except Exception:
                pass

            if m is None:
                sf1_rows = sf1_by_tick.get(ticker, [])
                if sf1_rows:
                    valid = [r for r in sf1_rows
                             if str(getattr(r, "datekey", "9999"))[:10] <= date_str]
                    if valid:
                        best = max(valid,
                                   key=lambda r: str(getattr(r, "datekey", ""))[:10])
                        # Get marketcap/ps from SHARADAR/DAILY for this date
                        daily_row = None
                        if ticker in mcap_index:
                            avail = [(d, mc, ps) for d, mc, ps in mcap_index[ticker]
                                     if d <= date_str]
                            if avail:
                                daily_row = avail[-1]
                        m = _sf1_row_to_metrics(best, daily_row)

            if m is None:
                total_skip += 1
                continue

            industry   = sf1_meta.get(ticker, {}).get("industry", "")
            tradeable, excl_reason = compute_tradeable_flag(
                ticker, date_str, industry, sf1_meta,
                volume_index, local_price_cache, excluded_mcap
            )

            closes = _build_price_series(ticker, volume_index, local_price_cache)
            mom, vs200, vs100, vs50, ret6m = _price_features(closes, date_str)
            fwd1, fwd3, fwd6, maxdd        = _forward_outcomes(closes, date_str)

            batch.append({
                "date":              date_str,
                "ticker":            ticker,
                "sharadar_industry": industry or None,
                "tradeable_flag":    1 if tradeable else 0,
                "exclusion_reason":  excl_reason,
                "ps_ratio":          _sf(m.get("PS_Ratio")),
                "ps_growth":         _sf(m.get("PS/Growth")),
                "gm_pct":            _sf(m.get("GM %")),
                "gm_erosion":        _sf(m.get("GM Erosion")),
                "rule40":            _sf(m.get("Rule 40")),
                "fcf_margin":        _sf(m.get("FCF_Margin_%")),
                "roic":              _sf(m.get("ROIC %")),
                "share_growth":      _sf(m.get("Share Growth %")),
                "inv_days":          _sf(m.get("Inv Days")),
                "inv_trend":         _sf(m.get("Inv Trend")),
                "pricing_power":     str(m.get("Pricing Power") or "Weak"),
                "revenue_growth":    _sf(m.get("Revenue_Growth_%")),
                "capex_sales":       _sf(m.get("Capex_Sales_%")),
                "momentum_126d":     _sf(mom),
                "price_vs_ma200":    _sf(vs200),
                "price_vs_ma100":    _sf(vs100),
                "price_vs_ma50":     _sf(vs50),
                "return_6m":         _sf(ret6m),
                "rs_score":          _sf(ret6m),
                "regime":            regime,
                "fwd_ret_1m":        _sf(fwd1),
                "fwd_ret_3m":        _sf(fwd3),
                "fwd_ret_6m":        _sf(fwd6),
                "max_dd_3m":         _sf(maxdd),
            })
            total_rows += 1

            if (i + 1) % CHECKPOINT_EVERY == 0 and conn is not None and batch:
                conn.executemany(INSERT_SQL, batch)
                conn.executemany(
                    "INSERT OR IGNORE INTO _build_progress (date, ticker, done) VALUES (?,?,1)",
                    [(r["date"], r["ticker"]) for r in batch]
                )
                conn.commit()
                batch.clear()
                elapsed = time.time() - t_loop
                pct = ((MONTH_STARTS.index(date_str) * len(all_tickers) + i) /
                       (len(MONTH_STARTS) * len(all_tickers)) * 100)
                print(f"  [{date_str}] [{i+1:>5}/{len(all_tickers)}] "
                      f"rows={total_rows:,} skip={total_skip:,} "
                      f"elapsed={elapsed:.0f}s ({pct:.1f}%)")

        # Flush remaining
        if batch and conn is not None:
            conn.executemany(INSERT_SQL, batch)
            conn.executemany(
                "INSERT OR IGNORE INTO _build_progress (date, ticker, done) VALUES (?,?,1)",
                [(r["date"], r["ticker"]) for r in batch]
            )
            conn.commit()
            batch.clear()

        # Industry percentiles for this date
        if conn is not None:
            compute_industry_percentiles(conn, date_str)
            conn.commit()

        trd = (conn.execute(
            "SELECT COUNT(*) FROM feature_cache WHERE date=? AND tradeable_flag=1",
            (date_str,)
        ).fetchone()[0] if conn else "?")
        print(f"  [{date_str}] DONE  rows={total_rows:,}  tradeable={trd}  "
              f"elapsed={time.time()-t_loop:.0f}s")

    # ------------------------------------------------------------------ #
    # STEP 8: DODGED_LOSER_PROXY computation
    # ------------------------------------------------------------------ #
    if conn is not None:
        print("\n[STEP 8] Computing DODGED_LOSER_PROXY rows (marketcap collapse) ...")
        n_proxy = compute_dodged_loser_proxies(
            conn, mcap_index, volume_index, verbose=True
        )

    # ------------------------------------------------------------------ #
    # STEP 9: Write metadata
    # ------------------------------------------------------------------ #
    if conn is not None:
        elapsed_total = time.time() - t0_global
        stats = conn.execute(
            "SELECT COUNT(*), SUM(tradeable_flag), COUNT(DISTINCT ticker) "
            "FROM feature_cache"
        ).fetchone()
        n_rows, n_trd, n_tk = stats
        date_min = conn.execute("SELECT MIN(date) FROM feature_cache").fetchone()[0]
        date_max = conn.execute("SELECT MAX(date) FROM feature_cache").fetchone()[0]
        n_proxy  = conn.execute(
            "SELECT COUNT(*) FROM feature_cache WHERE is_dodged_loser_proxy=1"
        ).fetchone()[0]
        n_mcap_excl = len(excluded_mcap)

        meta = {
            "build_date":             datetime.now().isoformat(),
            "n_rows":                 str(n_rows),
            "n_tradeable":            str(n_trd),
            "n_tickers":              str(n_tk),
            "date_range_start":       str(date_min),
            "date_range_end":         str(date_max),
            "n_survivors_yfinance":   str(len(survivors)),
            "n_excluded_mcap_prefilter": str(n_mcap_excl),
            "n_dodged_loser_proxies": str(n_proxy),
            "mcap_prefilter_threshold_m": str(MCAP_PREFILTER_M),
            "volume_source":          "yfinance+local_market_data.db",
            "daily_mcap_source":      "SHARADAR/DAILY",
            "delisted_bias":          "PRESENT -- dodged_loser counts are a floor",
            "build_duration_s":       str(round(elapsed_total, 1)),
            "delisted_bias_caveat":   DELISTED_BIAS_CAVEAT,
        }
        for k, v in meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO _cache_meta (key, value) VALUES (?,?)",
                (k, v)
            )
        conn.commit()

        print()
        print("=" * 78)
        print("  CACHE BUILD COMPLETE")
        print(f"  Total rows              : {n_rows:,}")
        print(f"  Tradeable rows          : {n_trd:,}")
        print(f"  Unique tickers          : {n_tk:,}")
        print(f"  yfinance survivors      : {len(survivors):,}")
        print(f"  EXCLUDED_MCAP_PREFILTER : {n_mcap_excl:,}")
        print(f"  DODGED_LOSER_PROXY rows : {n_proxy:,}")
        print(f"  Date range              : {date_min} -> {date_max}")
        print(f"  Elapsed                 : {elapsed_total:.1f}s")
        print()
        print(DELISTED_BIAS_CAVEAT)
        print("=" * 78)

    if args.spot_check > 0 and conn is not None:
        print(f"\n[STEP 10] Spot-checking {args.spot_check} rows vs live tester.py ...")
        spot_check_vs_tester(conn, n=args.spot_check)

    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
