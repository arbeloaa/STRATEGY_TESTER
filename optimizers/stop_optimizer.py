"""
stop_optimizer.py -- Stop-Loss Parameter Optimizer
====================================================
Fetches daily price data ONCE for all current BUY signals, then tests
N_COMBINATIONS random parameter sets entirely in-process (no re-fetching).
Each combination takes <1ms; total runtime is dominated by the initial fetch.

Parameters tested:
  HIGH_STOP, MED_STOP, LOW_STOP  -- hard stop thresholds
  TRAIL_ACTIVATE, TRAIL_PCT      -- trailing stop
  MA_DAYS                        -- gate-deterioration proxy MA period
  TIME_STOP_DAYS                 -- time stop

Scoring: avg_return * 0.5 + win_rate * 0.3 - return_stdev * 0.2

Output:
  stop_optimizer_results.csv   -- all combinations, sorted by score
  stop_optimizer_results.json  -- same + top-10 detail + best params

Usage:
  python stop_optimizer.py
  python stop_optimizer.py --json path/to/gate_report.json --combinations 100
"""

import json, random, time, statistics, csv, sys, argparse
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = PROJECT_ROOT / "reports" / "gate_report_latest.json"
OUTPUT_CSV   = PROJECT_ROOT / "reports" / "stop_optimizer_results.csv"
OUTPUT_JSON  = PROJECT_ROOT / "reports" / "stop_optimizer_results.json"
FETCH_DELAY  = 0.25
N_COMBINATIONS = 50

# Parameter ranges to test (all negative values are fractions, e.g. -0.15 = -15%)
PARAM_RANGES = {
    "HIGH_STOP":      [-0.12, -0.15, -0.18, -0.20],
    "MED_STOP":       [-0.08, -0.10, -0.12, -0.15],
    "LOW_STOP":       [-0.08, -0.10, -0.12],
    "TRAIL_ACTIVATE": [0.20, 0.25, 0.30, 0.40],
    "TRAIL_PCT":      [-0.08, -0.10, -0.12],
    "MA_DAYS":        [50, 75, 100, 150],
    "TIME_STOP_DAYS": [90, 120, 150, 180],
}

# Current virtual_trader.py defaults (baseline for comparison)
BASELINE_PARAMS = {
    "HIGH_STOP":      -0.15,
    "MED_STOP":       -0.10,
    "LOW_STOP":       -0.07,
    "TRAIL_ACTIVATE": 0.30,
    "TRAIL_PCT":      -0.10,
    "MA_DAYS":        50,
    "TIME_STOP_DAYS": 90,
}

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def log(msg, color=""):
    codes = {"green": GREEN, "red": RED, "yellow": YELLOW,
             "cyan": CYAN, "bold": BOLD}
    c = codes.get(color, "")
    rs = RESET if color else ""
    print(f"{c}{msg}{rs}", flush=True)

# ---------------------------------------------------------------------------
#  PRICE FETCH (once per ticker)
# ---------------------------------------------------------------------------
def fetch_all_prices(tickers: list, entry_date: str, end_date: str) -> dict:
    """Fetch daily Close data for all tickers. Returns {ticker: pd.DataFrame}."""
    cache = {}
    for i, ticker in enumerate(tickers):
        print(f"  [{i+1:>2}/{len(tickers)}] {ticker:<8} ...", end=" ", flush=True)
        try:
            t  = yf.Ticker(ticker)
            df = t.history(start=entry_date, end=end_date,
                           interval="1d", auto_adjust=True)
            if df.empty:
                print("no data")
                cache[ticker] = pd.DataFrame()
            else:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                df = df[["Close", "High", "Low", "Volume"]].copy()
                print(f"{len(df)} rows")
                cache[ticker] = df
        except Exception as e:
            print(f"ERROR: {e}")
            cache[ticker] = pd.DataFrame()
        time.sleep(FETCH_DELAY)
    return cache

# ---------------------------------------------------------------------------
#  IN-PROCESS SIMULATION
# ---------------------------------------------------------------------------
def simulate_one(stock: dict, df: pd.DataFrame, params: dict) -> dict | None:
    """
    Run a single position simulation using pre-fetched daily Close data.
    Returns position result dict or None if no data.
    """
    if df is None or df.empty:
        return None

    confidence = stock.get("confidence", "MED")
    stop_pct_map = {
        "HIGH": params["HIGH_STOP"],
        "MED":  params["MED_STOP"],
        "LOW":  params["LOW_STOP"],
    }
    stop_pct   = stop_pct_map.get(confidence, params["MED_STOP"])
    entry_px   = float(df["Close"].iloc[0])
    stop_price = entry_px * (1 + stop_pct)
    peak_price = entry_px
    trail_active = False
    exit_price = exit_date = exit_reason = None
    entry_day  = df.index[0]

    ma_days = params["MA_DAYS"]
    if len(df) >= ma_days:
        ma_series = df["Close"].rolling(ma_days).mean()
    else:
        ma_series = pd.Series([float("nan")] * len(df), index=df.index)

    for i, (dt, row) in enumerate(df.iterrows()):
        price    = float(row["Close"])
        gain_pct = (price - entry_px) / entry_px

        if price > peak_price:
            peak_price = price

        if not trail_active and gain_pct >= params["TRAIL_ACTIVATE"]:
            trail_active = True

        if trail_active:
            trail_stop = peak_price * (1 + params["TRAIL_PCT"])
            if price <= trail_stop:
                exit_price  = price
                exit_date   = dt
                exit_reason = "TRAIL_STOP"
                break

        if price <= stop_price:
            exit_price  = price
            exit_date   = dt
            exit_reason = f"HARD_STOP_{confidence}"
            break

        ma_val = ma_series.iloc[i]
        if pd.notna(ma_val) and price < float(ma_val) and i > ma_days:
            exit_price  = price
            exit_date   = dt
            exit_reason = "MA_CROSS"
            break

        days_held = (dt - entry_day).days
        if days_held >= params["TIME_STOP_DAYS"] and abs(gain_pct) < 0.05:
            exit_price  = price
            exit_date   = dt
            exit_reason = "TIME_STOP"
            break

    if exit_price is None:
        exit_price  = float(df["Close"].iloc[-1])
        exit_date   = df.index[-1]
        exit_reason = "END_OF_PERIOD"

    pnl_pct   = (exit_price - entry_px) / entry_px * 100
    days_held = (exit_date - entry_day).days
    return {
        "ticker":     stock["ticker"],
        "confidence": confidence,
        "pnl_pct":    round(pnl_pct, 2),
        "exit_reason": exit_reason,
        "days_held":  days_held,
    }

def run_combination(buys: list, price_cache: dict, params: dict) -> dict:
    """
    Simulate all positions with given params. Returns portfolio-level stats.
    """
    results = []
    for stock in buys:
        ticker = stock["ticker"]
        df     = price_cache.get(ticker)
        r      = simulate_one(stock, df, params)
        if r is not None:
            results.append(r)

    if not results:
        return {"score": -999, "params": params, "n": 0}

    pnls     = [r["pnl_pct"] for r in results]
    wins     = [p for p in pnls if p >= 0]
    losses   = [p for p in pnls if p < 0]
    avg_ret  = statistics.mean(pnls)
    win_rate = len(wins) / len(pnls) * 100
    stdev    = statistics.stdev(pnls) if len(pnls) > 1 else 0
    score    = avg_ret * 0.5 + win_rate * 0.3 - stdev * 0.2

    # Exit reason breakdown
    reason_counts = {}
    for r in results:
        k = r["exit_reason"].split("_")[0] if r["exit_reason"].startswith("HARD") else r["exit_reason"]
        reason_counts[k] = reason_counts.get(k, 0) + 1

    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    )

    return {
        "score":          round(score, 4),
        "avg_return_pct": round(avg_ret, 2),
        "win_rate_pct":   round(win_rate, 1),
        "stdev":          round(stdev, 2),
        "profit_factor":  round(profit_factor, 2),
        "avg_hold_days":  round(statistics.mean(r["days_held"] for r in results), 1),
        "pct_stopped":    round(sum(1 for r in results if "STOP" in r["exit_reason"]) / len(results) * 100, 1),
        "max_loss":       round(min(pnls), 1),
        "n":              len(results),
        "params":         params,
        "exit_reasons":   reason_counts,
    }

# ---------------------------------------------------------------------------
#  PARAM SAMPLING  (ensure baseline + N-1 random)
# ---------------------------------------------------------------------------
def sample_params(n: int) -> list:
    """Return n parameter dicts, first one is always the baseline."""
    combos = [dict(BASELINE_PARAMS)]  # baseline first
    seen   = set()
    seen.add(str(BASELINE_PARAMS))
    attempts = 0
    while len(combos) < n and attempts < n * 20:
        attempts += 1
        p = {k: random.choice(v) for k, v in PARAM_RANGES.items()}
        key = str(sorted(p.items()))
        if key not in seen:
            seen.add(key)
            combos.append(p)
    return combos

# ---------------------------------------------------------------------------
#  APPLY BEST PARAMS TO virtual_trader.py
# ---------------------------------------------------------------------------
def apply_params_to_vt(params: dict, vt_path: Path) -> bool:
    """
    Patch STOP_LOSS dict and other constants in virtual_trader.py in-place.
    Returns True on success.
    """
    if not vt_path.exists():
        log(f"  virtual_trader.py not found: {vt_path}", "red")
        return False
    import re
    code = vt_path.read_text(encoding="utf-8")

    replacements = [
        # STOP_LOSS dict entries
        (r'("HIGH"\s*:\s*)-[\d.]+',  rf'\g<1>{params["HIGH_STOP"]}'),
        (r'("MED"\s*:\s*)-[\d.]+',   rf'\g<1>{params["MED_STOP"]}'),
        (r'("LOW"\s*:\s*)-[\d.]+',   rf'\g<1>{params["LOW_STOP"]}'),
        # Trailing stop
        (r'(TRAIL_ACTIVATE_PCT\s*=\s*)[\d.]+', rf'\g<1>{params["TRAIL_ACTIVATE"]}'),
        (r'(TRAIL_STOP_PCT\s*=\s*)-[\d.]+',    rf'\g<1>{params["TRAIL_PCT"]}'),
        # MA and time stop
        (r'(MA_DETERIORATION_DAYS\s*=\s*)\d+', rf'\g<1>{params["MA_DAYS"]}'),
        (r'(TIME_STOP_DAYS\s*=\s*)\d+',        rf'\g<1>{params["TIME_STOP_DAYS"]}'),
    ]
    patched = code
    for pattern, repl in replacements:
        patched = re.sub(pattern, repl, patched)

    if patched == code:
        log("  [WARN] No constants changed -- patterns may not have matched", "yellow")
        return False

    vt_path.write_text(patched, encoding="utf-8")
    return True

# ---------------------------------------------------------------------------
#  MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Stop-Loss Parameter Optimizer")
    parser.add_argument("--json",         type=Path, default=DEFAULT_JSON)
    parser.add_argument("--combinations", type=int,  default=N_COMBINATIONS)
    parser.add_argument("--apply-best",   action="store_true",
                        help="Apply best params to virtual_trader.py after optimizing")
    parser.add_argument("--seed",         type=int,  default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    log("=" * 65, "bold")
    log("  STOP-LOSS PARAMETER OPTIMIZER", "bold")
    log(f"  Combinations: {args.combinations}  |  Seed: {args.seed}", "bold")
    log("=" * 65, "bold")

    # Load gate report
    if not args.json.exists():
        log(f"ERROR: {args.json} not found -- run tester.py first", "red")
        sys.exit(1)

    with open(args.json, encoding="utf-8") as f:
        report = json.load(f)

    meta   = report.get("meta", {})
    stocks = report.get("stocks", [])
    buys   = [s for s in stocks if s.get("strategy_passed") and not s.get("veto")]
    log(f"\n  BUY signals: {len(buys)}  |  Market regime: {meta.get('nasdaq_regime', '?')}")

    if not buys:
        log("ERROR: No BUY signals found.", "red")
        sys.exit(1)

    # Date range
    raw_start  = meta.get("start_date", "") or ""
    import re
    m          = re.search(r"(\d{4}-\d{2}-\d{2})", raw_start)
    entry_date = m.group(1) if m else datetime.today().strftime("%Y-%m-%d")
    end_date   = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"  Period: {entry_date} -> {end_date[:10]}\n")

    # Fetch price data ONCE
    log("[1/3] Fetching daily price data ...", "yellow")
    tickers     = [s["ticker"] for s in buys]
    price_cache = fetch_all_prices(tickers, entry_date, end_date)
    fetched     = sum(1 for df in price_cache.values() if not df.empty)
    log(f"\n  Fetched {fetched}/{len(tickers)} tickers successfully\n", "green")

    # Sample parameter combinations
    log("[2/3] Running parameter combinations ...", "yellow")
    combos   = sample_params(args.combinations)
    all_results = []

    for i, params in enumerate(combos):
        tag = "BASELINE" if i == 0 else f"combo_{i}"
        r   = run_combination(buys, price_cache, params)
        r["combo_id"] = tag
        all_results.append(r)
        marker = f"{GREEN}BASELINE{RESET}" if i == 0 else f"  {i:>3}"
        print(f"  {marker}  score={r['score']:>7.3f}  "
              f"ret={r['avg_return_pct']:>+6.1f}%  "
              f"wr={r['win_rate_pct']:>4.0f}%  "
              f"pf={r['profit_factor']:>5.2f}x  "
              f"stop%={r['pct_stopped']:>4.0f}%  "
              f"H={params['HIGH_STOP']*100:.0f}%/"
              f"M={params['MED_STOP']*100:.0f}%/"
              f"L={params['LOW_STOP']*100:.0f}%  "
              f"trail={params['TRAIL_ACTIVATE']*100:.0f}%/{abs(params['TRAIL_PCT'])*100:.0f}%  "
              f"ma={params['MA_DAYS']}d  "
              f"t={params['TIME_STOP_DAYS']}d",
              flush=True)

    # Sort by score
    all_results.sort(key=lambda x: -x["score"])
    baseline_r = next(r for r in all_results if r["combo_id"] == "BASELINE")

    log(f"\n[3/3] Results summary", "bold")
    log("=" * 65, "bold")
    log(f"\n  BASELINE:  score={baseline_r['score']:.3f}  "
        f"ret={baseline_r['avg_return_pct']:+.1f}%  "
        f"wr={baseline_r['win_rate_pct']:.0f}%  "
        f"pf={baseline_r['profit_factor']:.2f}x", "cyan")
    log(f"\n  TOP 10 COMBINATIONS:", "green")
    header = (f"  {'Rank':>4}  {'Score':>7}  {'Ret%':>6}  {'WR%':>5}  "
              f"{'PF':>5}  {'H%':>4}  {'M%':>4}  {'L%':>4}  "
              f"{'Trail':>9}  {'MA':>5}  {'Time':>5}")
    log(header)
    log("  " + "-" * 75)
    for rank, r in enumerate(all_results[:10], 1):
        p    = r["params"]
        star = " *" if r["combo_id"] == "BASELINE" else "  "
        col  = "green" if r["score"] > baseline_r["score"] else "yellow"
        log(f"  {rank:>4}{star}  {r['score']:>7.3f}  "
            f"{r['avg_return_pct']:>+5.1f}%  "
            f"{r['win_rate_pct']:>4.0f}%  "
            f"{r['profit_factor']:>5.2f}x  "
            f"{p['HIGH_STOP']*100:>3.0f}%  "
            f"{p['MED_STOP']*100:>3.0f}%  "
            f"{p['LOW_STOP']*100:>3.0f}%  "
            f"{p['TRAIL_ACTIVATE']*100:.0f}%/{abs(p['TRAIL_PCT'])*100:.0f}%  "
            f"{p['MA_DAYS']:>4}d  "
            f"{p['TIME_STOP_DAYS']:>4}d", col)

    best     = all_results[0]
    best_p   = best["params"]
    improved = best["score"] > baseline_r["score"]
    log(f"\n  BEST PARAMS (score={best['score']:.3f}, "
        f"{'BETTER' if improved else 'NO improvement over'} baseline):", "bold")
    for k, v in best_p.items():
        baseline_v = BASELINE_PARAMS[k]
        changed    = v != baseline_v
        mark       = f" <- was {baseline_v}" if changed else ""
        col        = "green" if changed else ""
        log(f"    {k:<20} = {v}{mark}", col)

    # Write CSV
    csv_rows = []
    for r in all_results:
        row = {"combo_id": r["combo_id"], "score": r["score"],
               "avg_return_pct": r["avg_return_pct"],
               "win_rate_pct": r["win_rate_pct"],
               "stdev": r["stdev"],
               "profit_factor": r["profit_factor"],
               "avg_hold_days": r["avg_hold_days"],
               "pct_stopped": r["pct_stopped"],
               "max_loss": r["max_loss"],
               "n": r["n"]}
        row.update({k: v for k, v in r["params"].items()})
        csv_rows.append(row)

    fieldnames = ["combo_id", "score", "avg_return_pct", "win_rate_pct", "stdev",
                  "profit_factor", "avg_hold_days", "pct_stopped", "max_loss", "n"] + list(PARAM_RANGES)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    # Write JSON
    out = {
        "generated":     datetime.now().isoformat(),
        "n_combinations": len(all_results),
        "n_positions":   len(buys),
        "entry_date":    entry_date,
        "baseline":      {k: baseline_r[k] for k in
                          ["score", "avg_return_pct", "win_rate_pct", "profit_factor"]},
        "best_params":   best_p,
        "best_score":    best["score"],
        "improved":      improved,
        "top_10":        all_results[:10],
        "all_results":   all_results,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    log(f"\n  CSV  -> {OUTPUT_CSV}", "cyan")
    log(f"  JSON -> {OUTPUT_JSON}", "cyan")

    if args.apply_best and improved:
        vt_path = PROJECT_ROOT / "scripts" / "virtual_trader.py"
        log(f"\n  Applying best params to {vt_path.name} ...", "yellow")
        if apply_params_to_vt(best_p, vt_path):
            log("  virtual_trader.py patched successfully", "green")
        else:
            log("  Patch failed -- manual edit required", "red")
    elif args.apply_best and not improved:
        log("\n  --apply-best: no improvement over baseline -- NOT patching virtual_trader.py", "yellow")

    log("\n" + "=" * 65, "bold")
    return best_p if improved else None


if __name__ == "__main__":
    main()
