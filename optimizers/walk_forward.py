#!/usr/bin/env python3
"""
walk_forward.py  --  walk-forward robustness check for the portfolio backtest
===============================================================================
Runs scripts/portfolio_simulator.py over several distinct historical windows
using its real --start/--end CLI flags (no file-patching), and scores each
period with auto_optimizer.compute_fitness() so the fitness formula used here
can never drift out of sync with the one the optimizer keep/rollback gate uses.

This is a fresh, simpler replacement for the concept behind the old
scripts/walk_forward.py (which patched data_fetcher.py's START_DATE/END_DATE
constants and re-fetched a CSV per period -- a different, now-obsolete,
CSV-based architecture). That file is not used or imported here.

Usage:
  python walk_forward.py                          # full run, all periods
  python walk_forward.py --periods 2022_bear 2024_mixed   # quick subset
"""

import sys, json, subprocess, argparse, time
from pathlib import Path

# Add project root to sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # optimizers/

from config.paths import PROJECT_ROOT, REPORTS_DIR
from auto_optimizer import compute_fitness

SIMULATOR    = PROJECT_ROOT / "engine" / "portfolio_simulator.py"
REPORT_JSON  = REPORTS_DIR / "portfolio_report.json"

# Distinct market regimes within the project's usable data range (most tickers
# have reliable price/fundamentals coverage from ~2020 onward -- see
# scripts/chack_coverage.py).
PERIODS = [
    {"label": "2020-2021_covid_bull", "start": "2020-01-01", "end": "2021-12-31", "type": "BULL"},
    {"label": "2022_bear",            "start": "2022-01-01", "end": "2022-12-31", "type": "BEAR"},
    {"label": "2023_recovery",        "start": "2023-01-01", "end": "2023-12-31", "type": "RECOVERY"},
    {"label": "2024_mixed",           "start": "2024-01-01", "end": "2024-12-31", "type": "MIXED"},
]

OVERFIT_SPREAD_THRESHOLD = 25.0   # max-min fitness across periods -> OVERFIT
BEAR_WEAK_GAP            = 15.0   # bull-avg minus bear fitness -> BEAR_WEAK
MIN_PERIODS_FOR_VERDICT  = 2      # spread is meaningless with fewer than this many periods


# ==============================================================================
#  PER-PERIOD RUN
# ==============================================================================

def run_period(period, timeout=1800):
    """
    Run portfolio_simulator.py for one period via subprocess (same pattern as
    auto_optimizer.run_simulator()), then load reports/portfolio_report.json.
    Returns a metrics dict on success, or None on failure.
    """
    label = period["label"]
    print(f"  [WF] Running {label}  ({period['start']} -> {period['end']}) ...", flush=True)
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(SIMULATOR), "--start", period["start"], "--end", period["end"]],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            encoding="ascii", errors="replace",
        )
        elapsed = time.time() - t0
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-400:]
            print(f"  [WF] {label} FAILED  exit={result.returncode}  ({elapsed:.0f}s)")
            print(f"  [WF] tail:\n{tail}")
            return None
        print(f"  [WF] {label} done in {elapsed:.0f}s", flush=True)
    except subprocess.TimeoutExpired:
        print(f"  [WF] {label} TIMEOUT")
        return None
    except Exception as exc:
        print(f"  [WF] {label} EXCEPTION: {exc}")
        return None

    try:
        with open(REPORT_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"  [WF] {label}: cannot load {REPORT_JSON}: {exc}")
        return None

    summary = data.get("summary", {})
    fitness = compute_fitness(summary)
    return {
        "label":            label,
        "type":             period["type"],
        "start":            period["start"],
        "end":              period["end"],
        "fitness":          fitness,
        "total_return_pct": summary.get("total_return_pct"),
        "sharpe":           summary.get("sharpe"),
        "max_drawdown":     summary.get("max_drawdown"),
        "win_rate":         summary.get("win_rate"),
        "n_closed":         summary.get("n_closed"),
    }


# ==============================================================================
#  VERDICT
# ==============================================================================

def robustness_verdict(period_results):
    """
    period_results: dict of label -> metrics dict (from run_period), or None
    for any period that failed to run.
    Returns {"verdict", "spread", "avg_fitness", "per_period"}.
    verdict is one of ROBUST, OVERFIT, BEAR_WEAK, or INSUFFICIENT_DATA
    (fewer than MIN_PERIODS_FOR_VERDICT periods ran -- a "spread" over 0 or 1
    points is not a meaningful robustness signal, so don't call it ROBUST).
    """
    per_period = {lbl: r["fitness"] for lbl, r in period_results.items() if r is not None}
    if len(per_period) < MIN_PERIODS_FOR_VERDICT:
        fitnesses   = list(per_period.values())
        avg_fitness = round(sum(fitnesses) / len(fitnesses), 4) if fitnesses else 0.0
        return {"verdict": "INSUFFICIENT_DATA", "spread": 0.0,
                "avg_fitness": avg_fitness, "per_period": per_period}

    fitnesses   = list(per_period.values())
    spread      = round(max(fitnesses) - min(fitnesses), 4)
    avg_fitness = round(sum(fitnesses) / len(fitnesses), 4)

    bull_fits = [per_period[lbl] for lbl, r in period_results.items()
                 if r is not None and r.get("type") == "BULL"]
    bear_fits = [per_period[lbl] for lbl, r in period_results.items()
                 if r is not None and r.get("type") == "BEAR"]

    # OVERFIT (blown-out spread across all regimes) takes priority over
    # BEAR_WEAK (a single weak regime) -- it is the stronger overfitting signal.
    if spread > OVERFIT_SPREAD_THRESHOLD:
        verdict = "OVERFIT"
    elif bull_fits and bear_fits and (sum(bull_fits) / len(bull_fits)
                                       - sum(bear_fits) / len(bear_fits)) > BEAR_WEAK_GAP:
        verdict = "BEAR_WEAK"
    else:
        verdict = "ROBUST"

    return {"verdict": verdict, "spread": spread, "avg_fitness": avg_fitness, "per_period": per_period}


# ==============================================================================
#  SUMMARY TABLE  (ASCII only)
# ==============================================================================

def print_summary(period_results, verdict_info):
    print()
    print("=" * 78)
    print("  WALK-FORWARD ROBUSTNESS SUMMARY")
    print("=" * 78)
    print(f"  {'Period':<24} {'Type':<10} {'Return%':>9} {'Sharpe':>7} {'MaxDD%':>8} {'Fitness':>9}")
    print("  " + "-" * 74)
    for lbl, r in period_results.items():
        if r is None:
            print(f"  {lbl:<24}  RUN FAILED")
            continue
        print(f"  {lbl:<24} {r['type']:<10} {r['total_return_pct']:>+9.2f} "
              f"{r['sharpe']:>7.2f} {r['max_drawdown']:>+8.2f} {r['fitness']:>9.4f}")
    print("  " + "-" * 74)
    print(f"  Spread (max-min fitness) : {verdict_info['spread']:.4f}")
    print(f"  Average fitness          : {verdict_info['avg_fitness']:.4f}")
    print(f"  VERDICT                  : {verdict_info['verdict']}")
    print("=" * 78)


# ==============================================================================
#  MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Walk-forward robustness check")
    parser.add_argument("--periods", nargs="+", default=None,
                        help="Subset of period labels to run (default: all)")
    args = parser.parse_args()

    periods_to_run = PERIODS
    if args.periods:
        wanted = set(args.periods)
        periods_to_run = [p for p in PERIODS if p["label"] in wanted]
        missing = wanted - {p["label"] for p in periods_to_run}
        if missing:
            print(f"  [WF] WARNING: unknown period label(s): {sorted(missing)}")
        if not periods_to_run:
            print("  [WF] ERROR: no matching periods to run")
            sys.exit(1)

    print("=" * 78)
    print("  WALK-FORWARD ROBUSTNESS CHECK")
    print(f"  Periods: {', '.join(p['label'] for p in periods_to_run)}")
    print("=" * 78)

    period_results = {}
    for period in periods_to_run:
        period_results[period["label"]] = run_period(period)

    verdict_info = robustness_verdict(period_results)
    print_summary(period_results, verdict_info)
    return verdict_info


if __name__ == "__main__":
    main()
