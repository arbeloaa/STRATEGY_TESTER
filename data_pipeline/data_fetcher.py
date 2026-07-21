import argparse
import yfinance as yf
import pandas as pd
import time
import os
from datetime import datetime, timedelta
from pathlib import Path

# Import local DB module (safe if not present -- warn only)
import sys as _sys
try:
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(_PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT_ROOT))
    _sys.path.insert(0, str(Path(__file__).resolve().parent))  # data_pipeline/
    import db as _db
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

# ---------------------------------------------
#  DATE RANGE SETTINGS
#  START_DATE : fundamentals pulled from the most recent fiscal year BEFORE this date
#               and Price_Change% starts here
#  END_DATE   : Price_Change% ends here (future date -> uses today's price)
# ---------------------------------------------
START_DATE = "2024-04-19"   # live signals
END_DATE   = "2026-04-19"   # today

# Earnings are reported ~90 days after fiscal year end.
# A fiscal year only qualifies if: fiscal_year_end + EARNINGS_LAG_DAYS < START_DATE
EARNINGS_LAG_DAYS = 90

# Fundamentals (financials / balance sheet / cashflow) are re-fetched when the
# most recent fiscal year in the CSV is older than this many days (~15 months).
FUNDAMENTAL_MAX_AGE_DAYS = 450

# Walk-forward mode: set via env var WF_MODE=1 (used by walk_forward.py).
# In WF_MODE, fiscal column selection uses today's date instead of START_DATE.
# This allows historical periods (e.g. 2022) to use current fundamentals,
# because yfinance only returns ~4 years of history, and older periods lack
# the 3 qualifying fiscal years required by the EARNINGS_LAG_DAYS filter.
WF_MODE = os.environ.get("WF_MODE", "") == "1"

from config.paths import PROJECT_ROOT, DATA_DIR
OUTPUT_DIR = str(DATA_DIR)

# ---------------------------------------------
#  SURVIVORSHIP BIAS TICKERS
#  Companies that failed / were delisted during walk-forward periods.
#  Include these in historical runs:
#    - If strategy said BUY (passed gates) and stock collapsed -> LOSS
#    - If strategy said AVOID (failed gates) and stock collapsed -> C-WIN (correct avoid)
#  These force the strategy to confront stocks it might not have seen post-delisting.
# ---------------------------------------------
DELISTED_TICKERS = {
    # EV startups that went bust 2021-2023
    "SPCE":  {"sector": "CleanTech / Emerging", "delisted_year": 2024, "reason": "EV/space failure"},
    "RIDE":  {"sector": "CleanTech / Emerging", "delisted_year": 2023, "reason": "EV fraud/bankruptcy"},
    "NKLA":  {"sector": "CleanTech / Emerging", "delisted_year": 2024, "reason": "EV fraud conviction"},
    "WKHS":  {"sector": "CleanTech / Emerging", "delisted_year": 2024, "reason": "EV startup collapse"},
    "GOEV":  {"sector": "CleanTech / Emerging", "delisted_year": 2024, "reason": "EV startup collapse"},
    "PTRA":  {"sector": "CleanTech / Emerging", "delisted_year": 2023, "reason": "EV startup collapse"},
    # Solar failures
    "SPWR":  {"sector": "Solar Hardware",        "delisted_year": 2024, "reason": "Bankruptcy 2024"},
    # Tech / SPAC failures
    "HYLN":  {"sector": "CleanTech / Emerging", "delisted_year": 2023, "reason": "SPAC EV failure"},
    "XPEV":  {"sector": "Enterprise SaaS & AI", "delisted_year": None, "reason": "China ADR delisting risk"},
}

# Note: Walk-forward tests should call fetch_trend_metrics() for DELISTED_TICKERS
# for the START_DATE of each period. If the ticker returns data, treat as live;
# if data_fetcher skips it (yfinance returns nothing), treat as:
#   - C-WIN if strategy would have avoided it (score < threshold)
#   - LOSS if strategy would have bought it (score >= threshold)


# ---------------------------------------------
#  SECTORS & SUB-INDUSTRIES
# ---------------------------------------------
SECTORS = {
    # -- 1. Software, Cyber & AI ------------------------------------------
    "Enterprise SaaS & AI": [
        "MSFT", "PLTR", "ORCL", "CRM", "PATH", "AI", "GTLB", "APPN",
        "WIX", "GDDY", "YEXT", "ZETA", "BOX", "DBX", "ASAN", "S", "TUYA"
    ],
    "Cybersecurity": [
        "CRWD", "PANW", "FTNT", "ZS", "OKTA", "TENB", "VRNS", "CHKP",
        "QLYS", "RDWR", "NET", "BB", "RPD"
    ],
    "FinTech & Payments": [
        "PYPL", "XYZ", "STNE", "PAGS", "DLO", "CPAY", "WEX", "FLYW",
        "TOST", "FOUR", "PAYO", "MQ", "PSFE", "GCT", "EVTC"
    ],
    "Data & Infrastructure": [
        "MDB", "SNOW", "DDOG", "NTNX", "NTAP", "RXT", "IOT",
        "DOX", "TDC", "AVPT", "VRSN"
    ],
    "Communications & Ops": [
        "TWLO", "BAND", "RAMP", "BLZE", "OSPN", "CSGS", "NTSK"
    ],

    # -- 2. Renewable Energy -----------------------------------------------
    "Solar Hardware": [
        "ENPH", "SEDG", "FSLR", "CSIQ", "JKS", "SHLS", "NXT", "SPWR"
    ],
    "Solar Installation": [
        "RUN", "ARRY", "SUUN"
    ],
    "Renewable Utilities": [
        "BEPC", "BEP", "CWEN", "ORA", "NRGV", "FLNC"
    ],
    "CleanTech / Emerging": [
        "TURB", "ASTI", "SMXT", "BEEM", "BNRG"
    ],

    # -- 3. MedTech --------------------------------------------------------
    "Medical Devices (Heavy)": [
        "MDT", "SYK", "ZBH", "ISRG", "BSX", "EW", "GEHC", "PHG",
        "STE", "GMED", "HAE"
    ],
    "Monitoring & Specialized": [
        "DXCM", "PODD", "TNDM", "MASI", "INSP", "GKOS", "IRTC",
        "SENS", "OSIX"
    ],
    "Diagnostics & Lab Tech": [
        "ABT", "CODX", "BRKR", "PACB", "QDEL", "NEOG", "BFLY",
        "QSI", "CTKB"
    ],
    "Emerging & Biotech Med": [
        "LMRI", "LAB", "MDAI", "ATEC", "CERS", "STIM", "TMCI", "LUNG"
    ],

    # -- 4. Semiconductors -------------------------------------------------
    "Major Processors":   ["NVDA", "AMD", "INTC", "AVGO", "ARM"],
    "Memory & Storage":   ["MU", "RMBS", "WOLF"],
    "Foundries":          ["TSM", "GFS", "UMC", "TSEM", "ASX"],
    "Connectivity":       ["QCOM", "MRVL", "ALAB", "CRDO", "QRVO", "SWKS", "SYNA"],
    "Analog & Power":     ["TXN", "ADI", "NXPI", "MCHP", "ON", "STM", "VSH", "ALGM", "POWI"],
    "Emerging/Small Cap": ["NVTS", "POET", "LAES", "INDI", "LSCC", "MXL", "SKYT", "HIMX", "MTSI"],
}


# ---------------------------------------------
#  HELPERS
# ---------------------------------------------
def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def get_price_on_date(ticker_obj, target_date: datetime) -> float | None:
    today = datetime.today()
    if target_date >= today:
        try:
            return float(ticker_obj.fast_info["last_price"])
        except Exception:
            return None
    start = target_date
    end   = target_date + timedelta(days=7)
    hist  = ticker_obj.history(start=start.strftime("%Y-%m-%d"),
                               end=end.strftime("%Y-%m-%d"))
    if hist.empty:
        return None
    return float(hist["Close"].iloc[0])


def pick_fiscal_columns(df_columns, before_date: datetime, ticker: str = ""):
    """
    Return the 3 most recent fiscal year columns where:
        fiscal_year_end + EARNINGS_LAG_DAYS < before_date
    This prevents look-ahead bias (earnings are reported ~90 days after year-end).
    """
    lag = timedelta(days=EARNINGS_LAG_DAYS)
    cutoff = pd.Timestamp(before_date)
    all_dates = sorted(
        [c for c in df_columns if isinstance(c, pd.Timestamp) or
         (isinstance(c, str) and c[:4].isdigit())],
        reverse=True
    )
    qualifying = []
    disqualified = []
    for c in all_dates:
        ts = pd.Timestamp(c)
        if ts + lag < cutoff:
            qualifying.append(c)
        else:
            disqualified.append((ts.date(), (ts + lag).date()))
    if disqualified and ticker:
        for fy_end, report_date in disqualified:
            print(f"  [LAG] {ticker}: FY {fy_end} excluded -- "
                  f"report date {report_date} >= START {before_date.date()}")
    if len(qualifying) < 3:
        return None
    return qualifying[0], qualifying[1], qualifying[2]


def get_momentum_data(ticker_obj) -> dict:
    today = datetime.today()
    start = today - timedelta(days=290)
    hist  = ticker_obj.history(
                start=start.strftime("%Y-%m-%d"),
                end=today.strftime("%Y-%m-%d")
            )
    result = {"price_vs_ma200_pct": None, "price_above_ma100": None,
              "price_vs_ma100_pct": None, "return_6m_pct": None,
              "price_vs_ma50_pct": None}
    if hist.empty or len(hist) < 20:
        return result

    closes        = hist["Close"]
    current_price = float(closes.iloc[-1])

    window200 = min(200, len(closes))
    ma200     = float(closes.iloc[-window200:].mean())
    result["price_vs_ma200_pct"] = round((current_price / ma200 - 1) * 100, 1)

    window100 = min(100, len(closes))
    ma100     = float(closes.iloc[-window100:].mean())
    result["price_vs_ma100_pct"] = round((current_price / ma100 - 1) * 100, 1)
    result["price_above_ma100"]  = bool(current_price > ma100)

    window50 = min(50, len(closes))
    ma50     = float(closes.iloc[-window50:].mean())
    result["price_vs_ma50_pct"] = round((current_price / ma50 - 1) * 100, 1)

    bars_6m = min(126, len(closes) - 1)
    if bars_6m > 0:
        price_6m_ago = float(closes.iloc[-(bars_6m + 1)])
        result["return_6m_pct"] = round((current_price / price_6m_ago - 1) * 100, 1)
    return result


def get_ndx_regime() -> str:
    """
    Returns 4-state MARKET_REGIME using NDX price vs MA100 and 20-day realized vol
    as a VIX proxy (annualized % std dev of daily returns).

    States:
      BULL_STRONG  -- above MA100, low vol  (< HIGH_VOL_THR)
      BULL_WEAK    -- above MA100, high vol (>= HIGH_VOL_THR)
      BEAR_GRIND   -- below MA100, low vol
      BEAR_VOLATILE-- below MA100, high vol
    """
    HIGH_VOL_THR = 20.0   # annualized %; above this = volatile/stressed market
    try:
        ndx = yf.Ticker("^NDX")
        # Use END_DATE as reference so walk-forward historical periods get the
        # correct regime (not today's).  Cap at today so future dates don't error.
        ref_dt = min(datetime.strptime(END_DATE, "%Y-%m-%d"), datetime.today())
        hist   = ndx.history(
                    start=(ref_dt - timedelta(days=200)).strftime("%Y-%m-%d"),
                    end=ref_dt.strftime("%Y-%m-%d")
                )
        if hist.empty or len(hist) < 25:
            return "BULL_STRONG"
        closes = hist["Close"]
        window = min(100, len(closes))
        ma100  = float(closes.iloc[-window:].mean())
        above_ma100 = float(closes.iloc[-1]) > ma100

        # 20-day realized volatility (annualized)
        daily_rets = closes.pct_change().dropna()
        vol_20d = float(daily_rets.iloc[-20:].std()) * (252 ** 0.5) * 100

        if above_ma100:
            regime = "BULL_STRONG" if vol_20d < HIGH_VOL_THR else "BULL_WEAK"
        else:
            regime = "BEAR_VOLATILE" if vol_20d >= HIGH_VOL_THR else "BEAR_GRIND"
        print(f"NDX: price={closes.iloc[-1]:.0f}  MA100={ma100:.0f}  "
              f"above={above_ma100}  vol20d={vol_20d:.1f}%  => {regime}")
        return regime
    except Exception as e:
        print(f"  [WARN] NDX regime fetch failed: {e} -- defaulting to BULL_STRONG")
        return "BULL_STRONG"


def get_ma200_and_return_6m(ticker_obj) -> tuple:
    m = get_momentum_data(ticker_obj)
    return m["price_vs_ma200_pct"], m["return_6m_pct"]


def needs_fundamental_refresh(row: "pd.Series", today: datetime) -> bool:
    """
    True when a full fundamental re-fetch is needed for this ticker:
      - Fiscal Year date is absent or older than FUNDAMENTAL_MAX_AGE_DAYS
      - Any critical fundamental column is missing / NA
    """
    try:
        fy_str = str(row.get("Fiscal Year", ""))
        if not fy_str or fy_str in ("nan", "None", ""):
            return True
        fy_date = datetime.strptime(fy_str[:10], "%Y-%m-%d")
        if (today - fy_date).days > FUNDAMENTAL_MAX_AGE_DAYS:
            return True
    except Exception:
        return True
    for col in ("GM %", "ROIC %", "Rule 40", "Revenue_Growth_%"):
        val = row.get(col)
        if val is None or str(val) in ("nan", "<NA>", "None", ""):
            return True
    return False


def fetch_price_only_metrics(ticker: str, sector: str,
                              start_date: datetime, end_date: datetime,
                              ndx_regime: str) -> "dict | None":
    """
    Fast incremental update: skips financials / balance_sheet / cashflow.
    Only refreshes price momentum and price-change columns.
    Merges into an existing CSV row; does NOT replace fundamental columns.
    """
    try:
        s   = yf.Ticker(ticker)
        mom = get_momentum_data(s)

        price_start      = get_price_on_date(s, start_date)
        price_end        = get_price_on_date(s, end_date)
        price_change_pct = None
        if price_start and price_end and price_start != 0:
            price_change_pct = round(((price_end - price_start) / price_start) * 100, 1)

        end_label = "today" if end_date >= datetime.today() else END_DATE

        def fmt(val, decimals=1):
            return round(float(val), decimals) if val is not None else pd.NA

        return {
            "MARKET_REGIME":     ndx_regime,
            "Price_Above_MA100": mom["price_above_ma100"],
            "Price_vs_MA100_%":  fmt(mom["price_vs_ma100_pct"], 1),
            "Price_vs_MA200_%":  fmt(mom["price_vs_ma200_pct"], 1),
            "Price_vs_MA50_%":   fmt(mom["price_vs_ma50_pct"],  1),
            "Return_6M_%":       fmt(mom["return_6m_pct"],      1),
            "RS_Score_Raw":      mom["return_6m_pct"],          # for re-ranking
            "Price Start":       fmt(price_start, 2),
            "Price End":         fmt(price_end,   2),
            f"Price_Change% ({START_DATE} -> {end_label})": price_change_pct,
        }
    except Exception as e:
        print(f"  [ERROR] {ticker} (price-only): {e}")
        return None


# ---------------------------------------------
#  POINT-IN-TIME FUNDAMENTAL COMPUTATION
#  Callable from both fetch_trend_metrics (live)
#  and pit_fundamentals.py (historical PIT build).
#  PROBLEM 2 FIX: price_at_anchor replaces fast_info["last_price"]
#  so P/S never leaks a future price into historical scoring.
# ---------------------------------------------
def compute_fundamentals_at(income, balance, cash, q_income, q_cash,
                             anchor_period_end, price_at_anchor,
                             ticker="", sector=""):
    """
    Compute all derived fundamental metrics anchored to anchor_period_end (T0).
    price_at_anchor: stock price on availability_date (T0 + reporting lag).
    Returns dict of computed metrics keyed to the CSV column names, or None.
    T1 and T2 are the two prior fiscal-year columns found in income.columns.
    """
    def _fmt(val, decimals=1):
        if val is None:
            return pd.NA
        return round(float(val), decimals)

    def gv(df, keys, date):
        for k in keys:
            if k in df.index:
                v = df.loc[k][date]
                return float(v) if pd.notna(v) else None
        return None

    def safe(val, fallback=0.0):
        return val if val is not None else fallback

    def gvq(df, keys, col):
        for k in keys:
            if k in df.index and col in df.columns:
                v = df.loc[k][col]
                return float(v) if pd.notna(v) else None
        return None

    T0 = pd.Timestamp(anchor_period_end)

    # Find T1, T2 as the two prior fiscal years in income.columns
    all_ann = sorted([c for c in income.columns if isinstance(c, pd.Timestamp)], reverse=True)
    t0_idx  = next((i for i, c in enumerate(all_ann) if abs((c - T0).days) <= 5), None)
    if t0_idx is None or t0_idx + 2 >= len(all_ann):
        return None
    T1 = all_ann[t0_idx + 1]
    T2 = all_ann[t0_idx + 2]

    if T0 not in balance.columns or T0 not in cash.columns:
        return None

    rev    = [gv(income,  ["Total Revenue"],                             d) for d in [T0, T1, T2]]
    gp     = [gv(income,  ["Gross Profit"],                              d) for d in [T0, T1, T2]]
    ebit   =  gv(income,  ["EBIT", "Operating Income"],                 T0)
    tax    =  gv(income,  ["Tax Provision"],                            T0)
    pretax =  gv(income,  ["Pretax Income"],                            T0)
    inv    = [gv(balance, ["Inventory"],                                 d) for d in [T0, T1]]
    assets =  gv(balance, ["Total Assets"],                             T0)
    liab   =  gv(balance, ["Current Liabilities"],                      T0)
    shares = [gv(balance, ["Ordinary Shares Number", "Share Issued"],   d) for d in [T0, T1]]
    fcf_op  = gv(cash, ["Operating Cash Flow"],                         T0)
    fcf_cap = gv(cash, ["Capital Expenditure"],                         T0)
    fcf     = (safe(fcf_op) - abs(safe(fcf_cap))) if (fcf_op is not None or fcf_cap is not None) else None

    capex_sales = None
    if fcf_cap is not None and rev[0] not in (None, 0.0):
        capex_sales = round(abs(safe(fcf_cap)) / safe(rev[0]) * 100, 1)

    net_income   = gv(income, ["Net Income"], T0)
    fcf_ni_ratio = None
    if fcf is not None and net_income is not None and net_income > 0:
        fcf_ni_ratio = round(fcf / net_income * 100, 1)

    q_cols = sorted(q_income.columns, reverse=True) if not q_income.empty else []
    Q0, Q1 = (q_cols[0], q_cols[1]) if len(q_cols) >= 2 else (None, None)

    # 1. PS/Growth  (PROBLEM 2 FIX: price_at_anchor, not today's price)
    mkt_cap    = price_at_anchor * safe(shares[0])
    ps_ratio   = (mkt_cap / safe(rev[0])) if rev[0] else None
    growth_yoy = (((safe(rev[0]) - safe(rev[1])) / safe(rev[1])) * 100
                  if rev[0] is not None and rev[1] not in (None, 0.0) else None)
    ps_growth_ratio = ((ps_ratio / growth_yoy)
                       if ps_ratio is not None and growth_yoy and growth_yoy > 0 else None)

    # 2. Gross Margin
    gm_curr    = ((safe(gp[0]) / safe(rev[0])) * 100 if rev[0] else None)
    gm_prev    = ((safe(gp[1]) / safe(rev[1])) * 100 if rev[1] else None)
    gm_erosion = ((gm_prev - gm_curr) if gm_curr is not None and gm_prev is not None else None)

    # 3. ROIC
    if ebit is not None and tax is not None and pretax and assets and liab:
        nopat = ebit * (1 - (tax / pretax))
        roic  = (nopat / (assets - liab)) * 100 if (assets - liab) else None
    else:
        roic = None

    # 4. Inventory Days
    cogs          = (safe(rev[0]) - safe(gp[0])) if rev[0] and gp[0] is not None else None
    cogs_prev     = (safe(rev[1]) - safe(gp[1])) if rev[1] and gp[1] is not None else None
    inv_days_curr = ((safe(inv[0]) / cogs) * 365 if inv[0] is not None and cogs and cogs > 0 else None)
    inv_days_prev = ((safe(inv[1]) / cogs_prev) * 365
                     if inv[1] is not None and cogs_prev and cogs_prev > 0 else None)
    inv_trend     = ((inv_days_curr - inv_days_prev)
                     if inv_days_curr is not None and inv_days_prev is not None else None)

    # 5. Rule of 40
    fcf_margin = ((safe(fcf) / safe(rev[0])) * 100 if fcf is not None and rev[0] else None)
    r40        = ((growth_yoy + fcf_margin)
                  if growth_yoy is not None and fcf_margin is not None else None)

    # 6. Share dilution
    share_growth = (((safe(shares[0]) - safe(shares[1])) / safe(shares[1])) * 100
                    if shares[0] is not None and shares[1] not in (None, 0.0) else None)

    # 7. Pricing Power
    if gm_erosion is not None and growth_yoy is not None:
        pricing_power = "Strong" if (gm_erosion < 1 and growth_yoy > 15) else "Weak"
    else:
        pricing_power = "N/A"

    # GM_Change_QoQ
    if Q0 and Q1:
        q_rev0 = gvq(q_income, ["Total Revenue"], Q0)
        q_gp0  = gvq(q_income, ["Gross Profit"],  Q0)
        q_rev1 = gvq(q_income, ["Total Revenue"], Q1)
        q_gp1  = gvq(q_income, ["Gross Profit"],  Q1)
        q_gm0  = ((q_gp0 / q_rev0) * 100 if q_gp0 is not None and q_rev0 else None)
        q_gm1  = ((q_gp1 / q_rev1) * 100 if q_gp1 is not None and q_rev1 else None)
        gm_change_qoq = ((q_gm0 - q_gm1) if q_gm0 is not None and q_gm1 is not None else None)
    else:
        gm_change_qoq = None

    # R40_Trend
    growth_t1_t2 = (((safe(rev[1]) - safe(rev[2])) / safe(rev[2])) * 100
                    if rev[1] is not None and rev[2] not in (None, 0.0) else None)
    fcf_t1 = (gv(cash, ["Operating Cash Flow"], T1)
              if T1 in cash.columns else None) if not cash.empty else None
    capex_t1 = (gv(cash, ["Capital Expenditure"], T1)
                if T1 in cash.columns else None) if not cash.empty else None
    fcf_val_t1 = ((safe(fcf_t1) - abs(safe(capex_t1)))
                  if fcf_t1 is not None or capex_t1 is not None else None)
    fcf_margin_t1 = ((safe(fcf_val_t1) / safe(rev[1])) * 100
                     if fcf_val_t1 is not None and rev[1] else None)
    r40_t1    = ((growth_t1_t2 + fcf_margin_t1)
                 if growth_t1_t2 is not None and fcf_margin_t1 is not None else None)
    r40_trend = ((r40 - r40_t1) if r40 is not None and r40_t1 is not None else None)

    return {
        "PS/Growth":        _fmt(ps_growth_ratio, 2),
        "PS_Ratio":         _fmt(ps_ratio,         2),
        "GM %":             _fmt(gm_curr,           1),
        "GM Erosion":       _fmt(gm_erosion,        1),
        "GM_Change_QoQ":    _fmt(gm_change_qoq,     1),
        "ROIC %":           _fmt(roic,              1),
        "Inv Days":         _fmt(inv_days_curr,     0),
        "Inv Trend":        _fmt(inv_trend,         1),
        "Rule 40":          _fmt(r40,               1),
        "R40_Trend":        _fmt(r40_trend,         1),
        "Share Growth %":   _fmt(share_growth,      2),
        "Pricing Power":    pricing_power,
        "Revenue_Growth_%": _fmt(growth_yoy,        1),
        "Capex_Sales_%":    _fmt(capex_sales,       1),
        "FCF_NI_Ratio_%":   _fmt(fcf_ni_ratio,      1),
        "FCF_Margin_%":     _fmt(fcf_margin,        1),
    }


# ---------------------------------------------
#  MAIN FETCH FUNCTION  (live CSV path)
# ---------------------------------------------
def fetch_trend_metrics(ticker: str, sector: str,
                        start_date: datetime, end_date: datetime,
                        ndx_regime: "bool | None" = None):
    try:
        s       = yf.Ticker(ticker)
        income  = s.financials
        balance = s.balance_sheet
        cash    = s.cashflow

        # In WF_MODE, use today as the cutoff so current fundamentals are always
        # available regardless of historical START_DATE.
        fiscal_cutoff = datetime.today() if WF_MODE else start_date
        cols = pick_fiscal_columns(income.columns, fiscal_cutoff, ticker=ticker)
        if cols is None:
            print(f"  [SKIP] {ticker}: not enough fiscal years before "
                  f"{fiscal_cutoff.date()} (after {EARNINGS_LAG_DAYS}-day earnings lag)")
            return None
        T0, T1, T2 = cols

        if T0 not in balance.columns or T0 not in cash.columns:
            print(f"  [SKIP] {ticker}: balance/cashflow missing for {T0.date()}")
            return None

        q_income = s.quarterly_financials
        q_cash   = s.quarterly_cashflow

        # Live path: P/S uses today's price (correct for generating the live CSV;
        # the PIT path in pit_fundamentals.py passes price_at_anchor instead).
        curr_price = s.fast_info["last_price"]

        fund = compute_fundamentals_at(income, balance, cash, q_income, q_cash,
                                       T0, curr_price, ticker=ticker, sector=sector)
        if fund is None:
            print(f"  [SKIP] {ticker}: compute_fundamentals_at returned None for {T0.date()}")
            return None

        # Append to local DB (grow PIT history each run)
        if _DB_AVAILABLE:
            try:
                _db.init_db()
                avail_dt = T0 + timedelta(days=EARNINGS_LAG_DAYS)
                _db.append_fundamentals(
                    ticker            = ticker,
                    period_end        = str(T0.date()),
                    availability_date = str(avail_dt.date()),
                    sector            = sector,
                    metrics_dict      = fund,
                    # captured_at defaults to today in append_fundamentals
                )
            except Exception as _dbe:
                print(f"  [DB WARN] append_fundamentals {ticker}: {_dbe}")

        # Forward Guidance (live-only -- not included in PIT snapshots)
        fwd_rev_est = None
        fwd_rev_growth_est = None
        def safe(val, fallback=0.0):
            return val if val is not None else fallback
        try:
            rev_est = s.revenue_estimate
            if rev_est is not None and not rev_est.empty:
                if "+1y" in rev_est.index:
                    fwd_rev_est = rev_est.loc["+1y", "avg"] if "avg" in rev_est.columns else None
                elif "1y" in rev_est.index:
                    fwd_rev_est = rev_est.loc["1y", "avg"] if "avg" in rev_est.columns else None
                # Need raw rev[0] to compute fwd growth -- re-fetch it
                def _gv(df, keys, date):
                    for k in keys:
                        if k in df.index:
                            v = df.loc[k][date]
                            return float(v) if pd.notna(v) else None
                    return None
                raw_rev0 = _gv(income, ["Total Revenue"], T0)
                if fwd_rev_est is not None and raw_rev0 not in (None, 0.0):
                    fwd_rev_growth_est = round(
                        ((float(fwd_rev_est) - safe(raw_rev0)) / safe(raw_rev0)) * 100, 1
                    )
                    fwd_rev_est = round(float(fwd_rev_est) / 1e9, 2)
        except Exception:
            pass

        guidance_delta = None
        try:
            q_est = getattr(s, 'quarterly_revenue_estimate', None)
            if q_est is None or (hasattr(q_est, 'empty') and q_est.empty):
                q_est = s.revenue_estimate
            if q_est is not None and not q_est.empty and "avg" in q_est.columns:
                periods = [p for p in ["+1q", "0q", "+1y", "0y"] if p in q_est.index]
                if len(periods) >= 2:
                    est_now  = q_est.loc[periods[0], "avg"]
                    est_prev = q_est.loc[periods[1], "avg"]
                    if pd.notna(est_now) and pd.notna(est_prev) and est_prev != 0:
                        delta_pct = ((float(est_now) - float(est_prev))
                                     / abs(float(est_prev))) * 100
                        if delta_pct > 1:
                            guidance_delta = "Raised"
                        elif delta_pct < -1:
                            guidance_delta = "Cut"
                        else:
                            guidance_delta = "Maintained"
        except Exception:
            pass

        # Momentum
        mom               = get_momentum_data(s)
        price_vs_ma200_pct = mom["price_vs_ma200_pct"]
        price_vs_ma100_pct = mom["price_vs_ma100_pct"]
        price_above_ma100  = mom["price_above_ma100"]
        return_6m_pct      = mom["return_6m_pct"]
        price_vs_ma50_pct  = mom["price_vs_ma50_pct"]
        rs_score_raw       = return_6m_pct

        # Price Change %
        price_start = get_price_on_date(s, start_date)
        price_end   = get_price_on_date(s, end_date)
        if price_start and price_end and price_start != 0:
            price_change_pct = round(((price_end - price_start) / price_start) * 100, 1)
        else:
            price_change_pct = None

        end_label = "today" if end_date >= datetime.today() else END_DATE

        def fmt(val, decimals=1):
            if val is None:
                return pd.NA
            return round(float(val), decimals)

        try:
            lag_days = (pd.Timestamp(start_date) - pd.Timestamp(T0)).days
        except Exception:
            lag_days = None

        return {
            "Ticker":                                          ticker,
            "Sector":                                          sector,
            "Fiscal Year":                                     str(T0.date()),
            "Fiscal_Year_Lag_Days":                            lag_days,
            "MARKET_REGIME":                                   ndx_regime,
            "PS/Growth":                                       fund["PS/Growth"],
            "PS_Ratio":                                        fund["PS_Ratio"],
            "GM %":                                            fund["GM %"],
            "GM Erosion":                                      fund["GM Erosion"],
            "GM_Change_QoQ":                                   fund["GM_Change_QoQ"],
            "ROIC %":                                          fund["ROIC %"],
            "Inv Days":                                        fund["Inv Days"],
            "Inv Trend":                                       fund["Inv Trend"],
            "Rule 40":                                         fund["Rule 40"],
            "R40_Trend":                                       fund["R40_Trend"],
            "Share Growth %":                                  fund["Share Growth %"],
            "Pricing Power":                                   fund["Pricing Power"],
            "Revenue_Growth_%":                                fund["Revenue_Growth_%"],
            "Capex_Sales_%":                                   fund["Capex_Sales_%"],
            "FCF_NI_Ratio_%":                                  fund["FCF_NI_Ratio_%"],
            "FCF_Margin_%":                                    fund["FCF_Margin_%"],
            "FY_Rev_Est_$B":                                   fmt(fwd_rev_est,        2),
            "FY_Rev_Growth_Est_%":                             fmt(fwd_rev_growth_est, 1),
            "Revenue_Guidance_Delta":                          guidance_delta,
            "Price_Above_MA100":                               price_above_ma100,
            "Price_vs_MA100_%":                                fmt(price_vs_ma100_pct, 1),
            "Price_vs_MA200_%":                                fmt(price_vs_ma200_pct, 1),
            "Price_vs_MA50_%":                                 fmt(price_vs_ma50_pct,  1),
            "Return_6M_%":                                     fmt(return_6m_pct,      1),
            "RS_Score_Raw":                                    rs_score_raw,
            "Price Start":                                     fmt(price_start,        2),
            "Price End":                                       fmt(price_end,          2),
            f"Price_Change% ({START_DATE} -> {end_label})":    price_change_pct,
        }

    except Exception as e:
        print(f"  [ERROR] {ticker}: {e}")
        return None


# ---------------------------------------------
#  ENTRY POINT
# ---------------------------------------------
if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="Fetch multi-sector fundamental data")
    _parser.add_argument(
        "--mode",
        choices=["full", "incremental", "auto"],
        default="auto",
        help=(
            "full        : re-fetch every ticker from scratch\n"
            "incremental : load existing CSV, only full-fetch new/stale tickers,\n"
            "              price-only update for the rest\n"
            "auto        : same as incremental (default)"
        ),
    )
    _args = _parser.parse_args()

    start_dt = parse_date(START_DATE)
    end_dt   = parse_date(END_DATE)

    print(f"Mode        : {_args.mode}")
    print(f"Date range  : {START_DATE}  ->  {END_DATE}")
    print(f"Fundamentals: most recent fiscal year before {START_DATE}")
    print(f"Output dir  : {OUTPUT_DIR}\n")
    print(f"Earnings lag: {EARNINGS_LAG_DAYS} days  |  "
          f"Fundamental max age: {FUNDAMENTAL_MAX_AGE_DAYS} days")
    print(f"Fetching NDX 4-state regime (^NDX vs MA100 + 20d realized vol)...")
    ndx_regime = get_ndx_regime()
    print(f"MARKET_REGIME = {ndx_regime}\n")

    output_path = os.path.join(OUTPUT_DIR, "multi_sector_trend_latest.csv")

    # ------------------------------------------------------------------
    #  Load existing CSV for incremental / auto mode
    # ------------------------------------------------------------------
    existing_df      = pd.DataFrame()
    existing_tickers: set = set()

    if _args.mode in ("incremental", "auto"):
        if os.path.exists(output_path):
            try:
                existing_df      = pd.read_csv(output_path)
                existing_tickers = set(existing_df["Ticker"].tolist())
                print(f"Loaded existing CSV: {len(existing_df)} rows, "
                      f"{len(existing_tickers)} tickers\n")
            except Exception as _e:
                print(f"  [WARN] Could not load existing CSV ({_e}) "
                      f"-- falling back to full fetch\n")
                existing_df      = pd.DataFrame()
                existing_tickers = set()

    # ------------------------------------------------------------------
    #  Per-ticker fetch routing
    # ------------------------------------------------------------------
    today          = datetime.today()
    results        = []
    n_full         = 0
    n_price_only   = 0
    n_kept         = 0

    for sector, tickers in SECTORS.items():
        print(f"\nAnalyzing: {sector}")
        for t in tickers:
            print(f"  {t} ...", end=" ", flush=True)

            # Determine whether a full fundamental re-fetch is needed
            existing_row = None
            if t in existing_tickers:
                rows = existing_df[existing_df["Ticker"] == t]
                if not rows.empty:
                    existing_row = rows.iloc[0]

            do_full = (
                _args.mode == "full"
                or existing_row is None
                or needs_fundamental_refresh(existing_row, today)
            )

            if do_full:
                data = fetch_trend_metrics(t, sector, start_dt, end_dt,
                                           ndx_regime=ndx_regime)
                if data:
                    results.append(data)
                    n_full += 1
                    print("OK (full)")
                else:
                    print("skipped")
                time.sleep(0.4)
            else:
                # Fast path: only refresh price / momentum columns
                updates = fetch_price_only_metrics(t, sector, start_dt, end_dt,
                                                   ndx_regime)
                if updates is not None:
                    data = existing_row.to_dict()
                    data.update(updates)
                    results.append(data)
                    n_price_only += 1
                    print("OK (price-only)")
                else:
                    # Price fetch also failed -- keep row unchanged
                    results.append(existing_row.to_dict())
                    n_kept += 1
                    print("kept (price fetch failed)")
                time.sleep(0.3)

    print(f"\nFetch summary: {n_full} full,  "
          f"{n_price_only} price-only,  {n_kept} kept unchanged")

    df = pd.DataFrame(results)

    # ------------------------------------------------------------------
    #  RS Score: always re-rank using fresh Return_6M_% values
    # ------------------------------------------------------------------
    if "RS_Score_Raw" not in df.columns and "Return_6M_%" in df.columns:
        df["RS_Score_Raw"] = pd.to_numeric(df["Return_6M_%"], errors="coerce")

    if "RS_Score_Raw" in df.columns:
        raw = pd.to_numeric(df["RS_Score_Raw"], errors="coerce")
        df["Relative_Strength_Score"] = (
            raw.rank(pct=True, na_option="keep") * 99 + 1
        ).round(0).astype("Int64")
        df.drop(columns=["RS_Score_Raw"], inplace=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"\nSaved -> {output_path}")

    print("\n" + "=" * 60)
    print("TREND-BASED STRATEGY REPORT -- Multi Sector")
    print("=" * 60)
    print(df.to_string(index=False))