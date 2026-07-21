"""
db.py  --  Local SQLite market data cache
==========================================
Permanent local database for:
  - prices       : adjusted OHLCV, upsert-ok (not PIT-sensitive)
  - fundamentals : APPEND-ONLY PIT snapshots, never updated or deleted
  - meta         : schema_version, last_fetch timestamps

All SQL is confined to this module.  Other scripts import the clean
public functions below; they never write raw SQL.

ASCII-only output (no Unicode box chars).

Schema version: 1
"""

import sqlite3
import json
import os
from datetime import date as _date, datetime as _datetime
import sys
from pathlib import Path

# Setup system path to import from config
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.paths import MARKET_DATA_DB
import pandas as pd

_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(db_path=None, check_exists=True):
    """Return a new sqlite3 connection with WAL mode and foreign-key support."""
    path = Path(db_path or MARKET_DATA_DB)
    if check_exists and not path.exists():
        raise FileNotFoundError(
            f"Market data database not found: {path.resolve()}"
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _today_str():
    return str(_date.today())


# ---------------------------------------------------------------------------
# PUBLIC: init_db
# ---------------------------------------------------------------------------

def init_db(db_path=None):
    """
    Create tables if absent and set schema_version.
    Safe to call on every run (idempotent).
    """
    try:
        conn = _connect(db_path, check_exists=False)
        c = conn.cursor()

        c.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
                ticker  TEXT NOT NULL,
                date    TEXT NOT NULL,
                close   REAL,
                high    REAL,
                low     REAL,
                volume  REAL,
                PRIMARY KEY (ticker, date)
            );

            CREATE TABLE IF NOT EXISTS fundamentals (
                ticker            TEXT NOT NULL,
                period_end        TEXT NOT NULL,
                availability_date TEXT NOT NULL,
                captured_at       TEXT NOT NULL,
                sector            TEXT,
                metrics_json      TEXT,
                PRIMARY KEY (ticker, period_end, captured_at)
            );

            CREATE INDEX IF NOT EXISTS idx_fund_ticker_avail
                ON fundamentals (ticker, availability_date);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS dead_tickers (
                ticker        TEXT PRIMARY KEY,
                first_failed  TEXT,
                last_checked  TEXT,
                fail_count    INTEGER DEFAULT 0
            );
        """)

        # Set schema_version if absent
        c.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", _SCHEMA_VERSION)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"init_db failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PUBLIC: upsert_prices
# ---------------------------------------------------------------------------

def upsert_prices(ticker, dataframe, db_path=None):
    """
    Upsert adjusted daily OHLCV from a DataFrame into the prices table.
    DataFrame index must be datetime; columns must include 'Close'.
    Optional columns: 'High', 'Low', 'Volume'.
    """
    if dataframe is None or dataframe.empty:
        return
    try:
        conn = _connect(db_path)
        rows = []
        for idx, row in dataframe.iterrows():
            d = str(pd.Timestamp(idx).date())
            close  = float(row["Close"])  if "Close"  in row and pd.notna(row["Close"])  else None
            high   = float(row["High"])   if "High"   in row and pd.notna(row["High"])   else None
            low    = float(row["Low"])    if "Low"    in row and pd.notna(row["Low"])     else None
            volume = float(row["Volume"]) if "Volume" in row and pd.notna(row["Volume"]) else None
            rows.append((ticker, d, close, high, low, volume))

        conn.executemany(
            """INSERT OR REPLACE INTO prices
               (ticker, date, close, high, low, volume)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"upsert_prices({ticker}) failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PUBLIC: get_prices
# ---------------------------------------------------------------------------

def get_prices(ticker, start=None, end=None, db_path=None):
    """
    Return a DataFrame with columns [Close, High, Low, Volume] indexed by
    Timestamp (tz-naive) for the given ticker.
    start and end are optional date strings 'YYYY-MM-DD'.
    Returns an empty DataFrame if no data exists.
    """
    try:
        conn = _connect(db_path)
        params = [ticker]
        where  = "ticker = ?"
        if start:
            where += " AND date >= ?"
            params.append(str(start)[:10])
        if end:
            where += " AND date <= ?"
            params.append(str(end)[:10])

        rows = conn.execute(
            f"SELECT date, close, high, low, volume FROM prices WHERE {where} ORDER BY date",
            params
        ).fetchall()
        conn.close()

        if not rows:
            return pd.DataFrame(columns=["Close", "High", "Low", "Volume"])

        df = pd.DataFrame(rows, columns=["date", "Close", "High", "Low", "Volume"])
        df.index = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df.index.name = None
        df = df.drop(columns=["date"])
        return df

    except Exception as exc:
        raise RuntimeError(f"get_prices({ticker}) failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PUBLIC: append_fundamentals
# ---------------------------------------------------------------------------

def append_fundamentals(ticker, period_end, availability_date, sector,
                         metrics_dict, captured_at=None, db_path=None):
    """
    APPEND-ONLY write of a PIT fundamental snapshot.

    Rules:
    - If a row exists for (ticker, period_end) with the same metrics_json,
      skip (no duplicate identical rows).
    - If metrics changed (e.g. restatement), INSERT a new row with today's
      captured_at (or the caller-supplied captured_at).
    - Never UPDATE or DELETE existing rows.

    metrics_dict : dict of fundamental column name -> value (may include
                   pd.NA / None which are stored as JSON null).
    captured_at  : override the timestamp (use for migration; default = today).
    """
    pe_str   = str(period_end)[:10]
    av_str   = str(availability_date)[:10]
    cap_str  = str(captured_at)[:10] if captured_at else _today_str()

    # Sanitise metrics: convert pd.NA / float NaN to None for JSON
    clean = {}
    for k, v in metrics_dict.items():
        if v is None:
            clean[k] = None
        else:
            try:
                fv = float(v)
                import math
                clean[k] = None if math.isnan(fv) else fv
            except (TypeError, ValueError):
                # Non-numeric (e.g. "Strong", "Weak")
                clean[k] = str(v) if pd.notna(v) else None

    new_json = json.dumps(clean, sort_keys=True)

    try:
        conn = _connect(db_path)
        # Check if any row for this (ticker, period_end) has identical metrics
        existing = conn.execute(
            """SELECT metrics_json FROM fundamentals
               WHERE ticker = ? AND period_end = ?
               ORDER BY captured_at""",
            (ticker, pe_str)
        ).fetchall()

        for (ex_json,) in existing:
            if ex_json == new_json:
                conn.close()
                return  # Identical row already exists -- skip

        # Either no row yet, or metrics differ -> insert new row
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals
               (ticker, period_end, availability_date, captured_at, sector, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, pe_str, av_str, cap_str, str(sector), new_json)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"append_fundamentals({ticker}, {pe_str}) failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# PUBLIC: get_fundamentals_asof
# ---------------------------------------------------------------------------

def get_fundamentals_asof(ticker, query_date, db_path=None):
    """
    Return the metrics dict for the PIT snapshot that was KNOWN as of query_date.

    Selection logic:
      1. Keep only rows where availability_date <= query_date
         (the report was already publicly available by that date).
      2. Among those, keep only rows where captured_at <= query_date
         (we only recorded this row on or before query_date -- no future look-ahead).
      3. Among the surviving rows, pick the one with the latest availability_date.
      4. If there are ties on availability_date, pick the latest captured_at.

    Returns the metrics dict (from metrics_json), or None if no row qualifies.
    """
    d_str = str(pd.Timestamp(query_date).date())
    try:
        conn = _connect(db_path)
        row = conn.execute(
            """SELECT metrics_json, sector, availability_date, captured_at
               FROM fundamentals
               WHERE ticker = ?
                 AND availability_date <= ?
                 AND captured_at      <= ?
               ORDER BY availability_date DESC, captured_at DESC
               LIMIT 1""",
            (ticker, d_str, d_str)
        ).fetchone()
        conn.close()

        if row is None:
            return None

        metrics_json, sector, avail_date, cap_at = row
        try:
            metrics = json.loads(metrics_json) if metrics_json else {}
        except Exception:
            metrics = {}

        # Inject envelope columns so scoring code can read Sector etc.
        metrics.setdefault("Ticker", ticker)
        metrics.setdefault("Sector", sector)
        metrics.setdefault("availability_date", avail_date)
        return metrics

    except Exception as exc:
        raise RuntimeError(
            f"get_fundamentals_asof({ticker}, {d_str}) failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# PUBLIC: list_pit_coverage
# ---------------------------------------------------------------------------

def list_pit_coverage(db_path=None):
    """
    Return a list of dicts summarising PIT coverage per ticker:
      {ticker, n_snapshots, first_avail, last_avail}
    """
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            """SELECT ticker,
                      COUNT(*) as n_snapshots,
                      MIN(availability_date) as first_avail,
                      MAX(availability_date) as last_avail
               FROM fundamentals
               GROUP BY ticker
               ORDER BY ticker"""
        ).fetchall()
        conn.close()
        return [
            {"ticker": r[0], "n_snapshots": r[1],
             "first_avail": r[2], "last_avail": r[3]}
            for r in rows
        ]
    except Exception as exc:
        raise RuntimeError(f"list_pit_coverage failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PUBLIC: update_meta
# ---------------------------------------------------------------------------

def update_meta(key, value, db_path=None):
    """Store or update a key-value pair in the meta table."""
    try:
        conn = _connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (str(key), str(value))
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"update_meta({key}) failed: {exc}") from exc


def get_meta(key, db_path=None):
    """Read a value from meta table; returns None if absent."""
    try:
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        raise RuntimeError(f"get_meta({key}) failed: {exc}") from exc


# ---------------------------------------------------------------------------
# PUBLIC: dead_tickers  --  persistent cache of persistently-empty fetches
# ---------------------------------------------------------------------------

def mark_dead(ticker, db_path=None):
    """
    Record one failed price fetch for ticker (fail_count += 1).
    is_dead() only returns True at fail_count >= 2, so a single transient
    failure does not permanently banish a live ticker.
    """
    today = _today_str()
    try:
        conn = _connect(db_path)
        row  = conn.execute(
            "SELECT fail_count FROM dead_tickers WHERE ticker=?", (ticker,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO dead_tickers (ticker, first_failed, last_checked, fail_count) "
                "VALUES (?,?,?,1)",
                (ticker, today, today)
            )
        else:
            conn.execute(
                "UPDATE dead_tickers SET last_checked=?, fail_count=fail_count+1 "
                "WHERE ticker=?",
                (today, ticker)
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"mark_dead({ticker}) failed: {exc}") from exc


def is_dead(ticker, db_path=None):
    """
    Return True if ticker has fail_count >= 2 in dead_tickers.
    fail_count=1 is a transient failure; only >=2 skips the network.
    Returns False on any DB error so a broken table never blocks a live ticker.
    """
    try:
        conn = _connect(db_path)
        row  = conn.execute(
            "SELECT fail_count FROM dead_tickers WHERE ticker=?", (ticker,)
        ).fetchone()
        conn.close()
        return (row is not None) and (row[0] >= 2)
    except Exception:
        return False


def clear_dead(ticker, db_path=None):
    """Remove ticker from dead_tickers (call when yfinance returns data again)."""
    try:
        conn = _connect(db_path)
        conn.execute("DELETE FROM dead_tickers WHERE ticker=?", (ticker,))
        conn.commit()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"clear_dead({ticker}) failed: {exc}") from exc


def list_dead(db_path=None):
    """Return list of dicts for all rows in dead_tickers, ordered by ticker."""
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            "SELECT ticker, first_failed, last_checked, fail_count "
            "FROM dead_tickers ORDER BY ticker"
        ).fetchall()
        conn.close()
        return [
            {"ticker": r[0], "first_failed": r[1],
             "last_checked": r[2], "fail_count": r[3]}
            for r in rows
        ]
    except Exception as exc:
        raise RuntimeError(f"list_dead failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Quick self-test (run as a script)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "test.db")
        init_db(p)
        print("init_db OK")

        # Prices round-trip
        idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        df = pd.DataFrame({"Close": [100.0, 101.5], "High": [102.0, 103.0],
                            "Low": [99.0, 100.5], "Volume": [1e6, 1.2e6]},
                           index=idx)
        upsert_prices("TEST", df, p)
        out = get_prices("TEST", db_path=p)
        assert len(out) == 2, f"Expected 2 rows, got {len(out)}"
        print("upsert_prices / get_prices OK")

        # Fundamentals round-trip
        m = {"GM %": 60.0, "Rule 40": 45.0, "Pricing Power": "Strong"}
        append_fundamentals("TEST", "2024-01-31", "2024-04-30",
                            "TestSector", m, captured_at="2024-04-30", db_path=p)
        res = get_fundamentals_asof("TEST", "2024-05-01", db_path=p)
        assert res is not None and res["GM %"] == 60.0
        print("append_fundamentals / get_fundamentals_asof OK")

        # Dedup: same metrics -> no new row
        append_fundamentals("TEST", "2024-01-31", "2024-04-30",
                            "TestSector", m, captured_at="2024-05-01", db_path=p)
        conn2 = sqlite3.connect(p)
        n = conn2.execute("SELECT COUNT(*) FROM fundamentals WHERE ticker='TEST'").fetchone()[0]
        conn2.close()
        assert n == 1, f"Expected 1 row after dedup, got {n}"
        print("dedup OK")

        cov = list_pit_coverage(p)
        assert cov[0]["n_snapshots"] == 1
        print("list_pit_coverage OK")

    print("All db.py self-tests PASSED")
