#!/usr/bin/env python3
"""
opportunity_report.py  --  First consumer of the two-layer feature cache.
=========================================================================
Reads data/feature_cache.db and produces logs/opportunity_report.txt.

MISSED WINNERS: every (ticker, date) where fwd_ret_3m > +30%.
  Classified into four mutually exclusive categories:

  EXCLUDED_LIQUIDITY  -- tradeable_flag=0 for volume reasons
                         (policy filter, not a gate finding)
  EXCLUDED_BIOTECH    -- tradeable_flag=0 for industry exclusion
                         (policy filter, not a gate finding)
  NOT_IN_OLD_UNIVERSE -- tradeable but tester.py's SECTOR_MAP has no entry
                         (universe curation gap, not a gate finding)
  GATE_REJECTED       -- in tradeable universe, scored by tester.py, failed.
                         This is the ONLY actual gate finding.
                         Records: which gate failed, at what margin,
                         and the foregone 3m move.

  For GATE_REJECTED: also records what fixed or relative threshold change
  would have admitted the ticker.

DODGED LOSERS: same for fwd_ret_3m < -25%.
  Records which gates correctly rejected them (defense record).
  This prevents loosening proposals that would have admitted them.

SUMMARY TABLES:
  Per window (2020-2024 / 2025-2026):
    - Count and total foregone-% per category
    - Per gate: winners rejected vs losers rejected, margin distributions
    - Net expected effect of loosening each gate

GATE REPLAY:
  - Only tradeable tickers on each date (not illiquid names we'd never buy)
  - Fundamentals preloaded in batch from feature_cache.db
  - Progress reported every 500 rows
  - Degradation labeled if cache is incomplete

DEGRADATION POLICY:
  If the cache was built without volume data, all EXCLUDED_LIQUIDITY /
  NOT_IN_OLD_UNIVERSE / GATE_REJECTED counts are labeled UNDER-COUNTED
  in the output.

Usage:
  python scripts/opportunity_report.py [--cache PATH] [--output PATH]
  python scripts/opportunity_report.py --window IS   # IS period only
  python scripts/opportunity_report.py --window OOS  # OOS period only
"""

import sys
import sqlite3
import json
import math
import time
import argparse
from datetime import datetime
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "data_pipeline"))

from config.paths import FEATURE_CACHE_DB, LOGS_DIR

CACHE_DB   = FEATURE_CACHE_DB
OUTPUT_TXT = LOGS_DIR  / "opportunity_report.txt"

# Import delisted-bias caveat from source of truth
try:
    from build_feature_cache import DELISTED_BIAS_CAVEAT
except Exception:
    DELISTED_BIAS_CAVEAT = (
        "*** STANDING CAVEAT -- DELISTED TICKER BIAS ***\n"
        "dodged-loser counts are a floor, not a total -- "
        "delisted coverage incomplete via yfinance.\n"
        "DODGED_LOSER_PROXY rows (marketcap collapse >70%) partially compensate."
    )

# Window definitions
WINDOWS = {
    "IS":  ("2020-01-01", "2024-12-31"),
    "OOS": ("2025-01-01", "2026-06-30"),
}

WINNER_THRESHOLD = 0.30   # fwd_ret_3m > +30%
LOSER_THRESHOLD  = -0.25  # fwd_ret_3m < -25%

# Category labels
CAT_LIQUIDITY   = "EXCLUDED_LIQUIDITY"
CAT_BIOTECH     = "EXCLUDED_BIOTECH"
CAT_NOT_IN_UNIV = "NOT_IN_OLD_UNIVERSE"
CAT_GATE_REJ    = "GATE_REJECTED"
CAT_DODGED      = "GATE_DEFENDED"
CAT_PROXY       = "DODGED_LOSER_PROXY"   # marketcap-collapse proxy for delisted losers

# ---------------------------------------------------------------------------
# Gate replay imports
# ---------------------------------------------------------------------------

def _load_tester():
    try:
        import importlib
        import tester as _t
        importlib.reload(_t)
        return _t
    except Exception as e:
        print(f"  [WARN] Cannot import tester.py: {e}")
        return None


def _score_row(trow, _tester, regime="BULL_STRONG"):
    """
    Run tester.py gate scoring on a row dict.
    Returns (score, threshold, gate_results, veto, veto_reason).
    gate_results is a dict {gate_name: (passed, weight, note, proxy, score_override)}.
    """
    if _tester is None:
        return None, None, {}, False, ""

    sector = str(trow.get("Sector", ""))
    univ_map = _tester.SECTOR_MAP
    univ_info = univ_map.get(sector)
    if univ_info is None:
        return None, None, {}, False, "NO_SECTOR_MAP"

    universe, sub = univ_info
    sector_pct_rank = 50.0

    try:
        _tester._ndx_regime = regime or "BULL_STRONG"

        if universe == "energy":
            gate_results = _tester.gates_energy(trow, sub, sector_pct_rank)
        elif universe == "tech":
            gate_results = _tester.gates_tech(trow, sub, sector_pct_rank)
        elif universe == "medtech":
            gate_results = _tester.gates_medtech(trow, sub, sector_pct_rank)
        elif universe == "semi":
            gate_results = _tester.gates_semi(trow, sub, sector_pct_rank)
        else:
            return None, None, {}, False, f"UNKNOWN_UNIVERSE:{universe}"

        veto, veto_reason = _tester.check_veto(trow, sector_pct_rank)
        if veto:
            thr = _tester.pass_threshold(universe)
            return 0.0, thr, gate_results, True, veto_reason

        w_score, max_pos, nd, np_ = _tester.score_gates(gate_results)
        rescue = _tester.compute_momentum_rescue(
            gate_results, w_score, _tester.pass_threshold(universe), trow, universe
        )
        total_score = w_score + rescue
        thr = _tester.pass_threshold(universe)
        return total_score, thr, gate_results, False, ""

    except Exception as e:
        return None, None, {}, False, f"SCORING_ERROR:{e}"


def _find_failing_gate(gate_results, score, threshold, veto, veto_reason):
    """
    Identify the single gate most responsible for rejection.
    Returns (gate_name, margin_below_pass, note).
    """
    if veto:
        return "VETO", 0.0, veto_reason

    if score is None:
        return "UNKNOWN", 0.0, "scoring failed"

    gap = threshold - score  # positive = below threshold
    if gap <= 0:
        return None, 0.0, "actually passed"

    # Find gates that failed and contributed most to the gap
    failed_gates = []
    for gname, (passed, weight, note, is_proxy, score_override) in gate_results.items():
        if weight == 0:
            continue
        actual_score = score_override if score_override is not None else (weight if passed else 0.0)
        contribution = weight - actual_score   # how much it's costing us
        if contribution > 0.01:
            failed_gates.append((gname, contribution, weight, note))

    if not failed_gates:
        return "SCORE_GAP", gap, f"score={score:.2f} thr={threshold:.2f}"

    # Sort by contribution
    failed_gates.sort(key=lambda x: -x[1])
    top_gate, top_contrib, top_weight, top_note = failed_gates[0]
    margin = top_weight - (threshold - score)   # how much the threshold would need to drop
    return top_gate, top_contrib, top_note


def _threshold_that_would_admit(score, threshold):
    """
    Return the threshold value that would have just admitted this ticker
    (score - 0.05 for a small margin above the new threshold).
    """
    if score is None:
        return None
    return round(score - 0.05, 3)


def _cache_row_to_tester_row(cache_row):
    """Convert a feature_cache row dict to tester.py input format."""
    return {
        "Ticker":               cache_row.get("ticker", ""),
        "Sector":               cache_row.get("sharadar_industry", ""),
        "PS_Ratio":             cache_row.get("ps_ratio") or 999,
        "PS/Growth":            cache_row.get("ps_growth") or 999,
        "GM %":                 cache_row.get("gm_pct") or 0,
        "GM Erosion":           cache_row.get("gm_erosion") or 0,
        "Rule 40":              cache_row.get("rule40") or 0,
        "FCF_Margin_%":         cache_row.get("fcf_margin") or 0,
        "ROIC %":               cache_row.get("roic") or 0,
        "Share Growth %":       cache_row.get("share_growth") or 0,
        "Inv Days":             cache_row.get("inv_days") or 0,
        "Inv Trend":            cache_row.get("inv_trend") or 0,
        "Pricing Power":        cache_row.get("pricing_power") or "Weak",
        "Revenue_Growth_%":     cache_row.get("revenue_growth") or 0,
        "Capex_Sales_%":        cache_row.get("capex_sales") or 0,
        "Price_vs_MA200_%":     cache_row.get("price_vs_ma200") or 0,
        "Price_vs_MA100_%":     cache_row.get("price_vs_ma100") or 0,
        "Price_vs_MA50_%":      cache_row.get("price_vs_ma50") or 0,
        "Return_6M_%":          cache_row.get("return_6m") or 0,
        "Relative_Strength_Score": cache_row.get("rs_score") or 50,
        "SMA20":                1.0,   # not in cache; use neutral value
        "Price":                1.0,   # same
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def run_analysis(conn, window_label, start_date, end_date, _tester, lines,
                 volume_data_available=True):
    """
    Run the full missed-winner / dodged-loser analysis for one date window.
    Appends results to `lines` list (written to file at end).
    Returns summary dict.
    """
    t0 = time.time()

    lines.append("")
    lines.append("=" * 80)
    lines.append(f"  WINDOW: {window_label}  ({start_date} -> {end_date})")
    lines.append("=" * 80)

    if not volume_data_available:
        lines.append("")
        lines.append("  *** DEGRADATION WARNING ***")
        lines.append("  The feature cache was built without a daily volume/price table.")
        lines.append("  tradeable_flag is incomplete for tickers outside market_data.db.")
        lines.append("  All category counts below are UNDER-COUNTED.")
        lines.append("  Re-build the cache with SHARADAR/DAILY or SEP access for full fidelity.")
        lines.append("")

    # ---- Fetch all rows in window ----------------------------------------
    cur = conn.execute(
        """
        SELECT date, ticker, sharadar_industry,
               tradeable_flag, exclusion_reason,
               ps_ratio, ps_growth, gm_pct, gm_erosion, rule40, fcf_margin,
               roic, share_growth, inv_days, inv_trend, pricing_power,
               revenue_growth, capex_sales,
               momentum_126d, price_vs_ma200, price_vs_ma100, price_vs_ma50,
               return_6m, rs_score,
               regime,
               fwd_ret_3m, fwd_ret_1m, fwd_ret_6m, max_dd_3m,
               COALESCE(is_dodged_loser_proxy, 0) AS is_proxy
        FROM feature_cache
        WHERE date >= ? AND date <= ?
          AND fwd_ret_3m IS NOT NULL
        ORDER BY date, ticker
        """,
        (start_date, end_date)
    )
    all_rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    # ---- Separate tickers in old universe from SF1-only -------------------
    try:
        from data_fetcher import SECTORS, DELISTED_TICKERS
        old_universe_tickers = set()
        for tks in SECTORS.values():
            old_universe_tickers.update(tks)
        old_universe_tickers.update(DELISTED_TICKERS.keys())
    except Exception:
        old_universe_tickers = set()

    # ---- Identify opportunities ------------------------------------------
    missed_winners = []   # fwd_ret_3m > +30%
    dodged_losers  = []   # fwd_ret_3m < -25%

    n_processed = 0
    n_gate_replayed = 0
    t_last_print = time.time()

    for row in all_rows:
        fwd3m    = row.get("fwd_ret_3m")
        if fwd3m is None:
            continue

        is_proxy = int(row.get("is_proxy", 0))
        is_winner = fwd3m > WINNER_THRESHOLD
        is_loser  = fwd3m < LOSER_THRESHOLD

        # DODGED_LOSER_PROXY rows: always count as dodged losers, labeled [PROXY]
        if is_proxy:
            record = {
                "ticker":       row["ticker"],
                "date":         row["date"],
                "industry":     row.get("sharadar_industry", ""),
                "fwd_ret_3m":   round(float(fwd3m) * 100, 2),
                "fwd_ret_1m":   0.0,
                "fwd_ret_6m":   0.0,
                "max_dd_3m":    round(float(row.get("max_dd_3m") or 0) * 100, 2),
                "category":     CAT_PROXY,
                "regime":       row.get("regime", ""),
                "gate_name":    "MCAP_COLLAPSE",
                "gate_margin":  None,
                "gate_note":    "Marketcap collapsed >70% -- proxy for delisted disaster",
                "gate_score":   None, "gate_thr": None, "admit_thr": None,
            }
            dodged_losers.append(record)
            n_processed += 1
            continue

        if not is_winner and not is_loser:
            n_processed += 1
            continue

        ticker       = row["ticker"]
        date         = row["date"]
        tradeable    = int(row.get("tradeable_flag", 0))
        excl_reason  = row.get("exclusion_reason") or ""

        # ---- Classify ----------------------------------------------------
        category = None
        gate_name   = None
        gate_margin = None
        gate_note   = None
        gate_score  = None
        gate_thr    = None
        admit_thr   = None  # threshold that would have admitted
        gate_results_json = None

        if tradeable == 0:
            # Not tradeable -- why?
            if "INDUSTRY" in excl_reason or "BIOTECH" in excl_reason:
                category = CAT_BIOTECH
            else:
                category = CAT_LIQUIDITY
        elif ticker not in old_universe_tickers:
            category = CAT_NOT_IN_UNIV
        else:
            # In tradeable universe, in old universe -- run gate scoring
            trow = _cache_row_to_tester_row(row)
            regime = row.get("regime", "BULL_STRONG")
            score, thr, gresults, veto, veto_reason = _score_row(trow, _tester, regime)
            n_gate_replayed += 1

            if score is None or thr is None:
                category = CAT_NOT_IN_UNIV  # sector not in SECTOR_MAP
            elif score >= thr:
                category = "PASSED_GATE"   # was in universe and passed -- executed trade
            else:
                category = CAT_GATE_REJ
                gname, gmargin, gnote = _find_failing_gate(gresults, score, thr, veto, veto_reason)
                gate_name   = gname
                gate_margin = round(float(gmargin), 4) if gmargin is not None else None
                gate_note   = str(gnote)[:200] if gnote else None
                gate_score  = round(float(score), 4)
                gate_thr    = round(float(thr), 4)
                admit_thr   = _threshold_that_would_admit(score, thr)
                gate_results_json = json.dumps({
                    k: {"passed": v[0], "weight": v[1], "score": v[4]}
                    for k, v in gresults.items()
                })

        if category is None:
            n_processed += 1
            continue

        record = {
            "ticker":       ticker,
            "date":         date,
            "industry":     row.get("sharadar_industry", ""),
            "fwd_ret_3m":   round(float(fwd3m) * 100, 2),
            "fwd_ret_1m":   round(float(row.get("fwd_ret_1m") or 0) * 100, 2),
            "fwd_ret_6m":   round(float(row.get("fwd_ret_6m") or 0) * 100, 2),
            "max_dd_3m":    round(float(row.get("max_dd_3m") or 0) * 100, 2),
            "category":     category,
            "regime":       row.get("regime", ""),
            "gm_pct":       row.get("gm_pct"),
            "ps_ratio":     row.get("ps_ratio"),
            "rule40":       row.get("rule40"),
            "revenue_growth": row.get("revenue_growth"),
            # Gate data (only for GATE_REJECTED)
            "gate_name":    gate_name,
            "gate_margin":  gate_margin,
            "gate_note":    gate_note,
            "gate_score":   gate_score,
            "gate_thr":     gate_thr,
            "admit_thr":    admit_thr,
        }

        if is_winner and category != "PASSED_GATE":
            missed_winners.append(record)
        elif is_loser and category in (CAT_GATE_REJ, CAT_BIOTECH, CAT_LIQUIDITY, CAT_NOT_IN_UNIV):
            # A loser correctly rejected by ANY filter is a "dodged loser"
            record["category"] = CAT_DODGED
            record["which_filter"] = category   # original classification
            record["gate_name"]    = gate_name
            dodged_losers.append(record)
        elif is_loser and category == "PASSED_GATE":
            # Passed all gates and was a loser -- these are the bad entries
            record["category"] = "PASSED_GATE_LOSER"
            dodged_losers.append(record)

        n_processed += 1

        if time.time() - t_last_print > 10:
            t_last_print = time.time()
            print(f"  [{window_label}] processed {n_processed:,} rows, "
                  f"gate_replays={n_gate_replayed:,}, "
                  f"winners={len(missed_winners)}, losers={len(dodged_losers)}")

    elapsed = time.time() - t0
    print(f"  [{window_label}] Complete: {n_processed:,} rows in {elapsed:.1f}s  "
          f"gate_replays={n_gate_replayed:,}  "
          f"missed_winners={len(missed_winners)}  dodged_losers={len(dodged_losers)}")

    # ------------------------------------------------------------------ #
    # Summary tables
    # ------------------------------------------------------------------ #
    _write_missed_winners_table(missed_winners, window_label, lines)
    _write_dodged_losers_table(dodged_losers, window_label, lines)
    _write_gate_analysis(missed_winners, dodged_losers, window_label, lines)

    return {
        "window":          window_label,
        "missed_winners":  len(missed_winners),
        "dodged_losers":   len(dodged_losers),
        "gate_replayed":   n_gate_replayed,
        "elapsed_s":       round(elapsed, 1),
        "missed_by_cat":   _count_by(missed_winners, "category"),
        "gate_findings":   [r for r in missed_winners if r["category"] == CAT_GATE_REJ],
    }


def _count_by(records, field):
    counts = defaultdict(int)
    for r in records:
        counts[r.get(field, "UNKNOWN")] += 1
    return dict(counts)


def _write_missed_winners_table(missed, window, lines):
    lines.append("")
    lines.append(f"  MISSED WINNERS (fwd_ret_3m > +{WINNER_THRESHOLD*100:.0f}%)  --  {window}")
    lines.append("  " + "-" * 78)
    lines.append("  NOTE: Only GATE_REJECTED is an actual gate finding.")
    lines.append("        EXCLUDED_* and NOT_IN_OLD_UNIVERSE price the policy filters.")

    # Aggregate by category
    by_cat = defaultdict(list)
    for r in missed:
        by_cat[r["category"]].append(r["fwd_ret_3m"])

    lines.append(f"  {'Category':<30} {'Count':>6}  {'TotalForgone%':>14}  {'AvgForgone%':>12}")
    lines.append("  " + "-" * 68)
    total_count = 0
    total_pct   = 0.0
    for cat in [CAT_LIQUIDITY, CAT_BIOTECH, CAT_NOT_IN_UNIV, CAT_GATE_REJ]:
        vals = by_cat.get(cat, [])
        if not vals:
            continue
        tot = sum(vals)
        avg = tot / len(vals)
        lines.append(f"  {cat:<30} {len(vals):>6}  {tot:>+14.1f}%  {avg:>+12.1f}%")
        total_count += len(vals)
        total_pct   += tot
    if total_count:
        lines.append("  " + "-" * 68)
        lines.append(f"  {'TOTAL':<30} {total_count:>6}  {total_pct:>+14.1f}%  "
                     f"{total_pct/total_count:>+12.1f}%")
        lines.append("")
        lines.append("  NOTE: Only GATE_REJECTED is an actual gate finding.")
        lines.append("        EXCLUDED_* and NOT_IN_OLD_UNIVERSE price the policy filters, not the gates.")

    # Top 20 GATE_REJECTED by foregone move
    gate_rej = [r for r in missed if r["category"] == CAT_GATE_REJ]
    gate_rej.sort(key=lambda x: -x["fwd_ret_3m"])
    if gate_rej:
        lines.append("")
        lines.append(f"  TOP GATE_REJECTED MISSED WINNERS (by foregone 3m move):")
        lines.append(f"  {'Date':<12} {'Ticker':<8} {'Industry':<28} {'3m%':>6}  "
                     f"{'Gate':<28} {'Margin':>8}  {'AdmitThr':>9}")
        lines.append("  " + "-" * 105)
        for r in gate_rej[:20]:
            lines.append(
                f"  {r['date']:<12} {r['ticker']:<8} {(r['industry'] or '')[:28]:<28} "
                f"{r['fwd_ret_3m']:>+6.1f}%  "
                f"{(r['gate_name'] or '')[:28]:<28} "
                f"{(r['gate_margin'] or 0):>+8.3f}  "
                f"{(r['admit_thr'] or 0):>9.3f}"
            )
        if len(gate_rej) > 20:
            lines.append(f"  ... and {len(gate_rej)-20} more GATE_REJECTED missed winners")


def _write_dodged_losers_table(losers, window, lines):
    dodged    = [r for r in losers if r["category"] == CAT_DODGED]
    proxy     = [r for r in losers if r["category"] == CAT_PROXY]
    passed_bad= [r for r in losers if r["category"] == "PASSED_GATE_LOSER"]

    lines.append("")
    lines.append(f"  DODGED LOSERS (fwd_ret_3m < {LOSER_THRESHOLD*100:.0f}%)  --  {window}")
    lines.append("  (Defense record -- correctly rejected by filters)")
    lines.append("  " + "-" * 78)
    lines.append("")
    lines.append("  *** DELISTED BIAS CAVEAT: these counts are a FLOOR, not a total.")
    lines.append("  *** The worst disasters (frauds, bankruptcies) delisted and may")
    lines.append("  *** have no yfinance data. DODGED_LOSER_PROXY rows (below) partially")
    lines.append("  *** compensate via SHARADAR/DAILY marketcap collapse detection.")
    lines.append("  *** Every loosen-vs-tighten decision must treat this table as a floor.")
    lines.append("")

    by_filter = defaultdict(list)
    for r in dodged:
        by_filter[r.get("which_filter", "UNKNOWN")].append(r["fwd_ret_3m"])
    for r in proxy:
        by_filter[CAT_PROXY].append(r["fwd_ret_3m"])

    lines.append(f"  {'Filter':<30} {'Count':>6}  {'TotalAvoided%':>14}  {'AvgDD%':>9}  {'Note':>15}")
    lines.append("  " + "-" * 80)
    for filt, vals in sorted(by_filter.items(), key=lambda x: -len(x[1])):
        tot = sum(vals)
        avg = tot / len(vals)
        note = "[PROXY -- floor]" if filt == CAT_PROXY else ""
        lines.append(f"  {filt:<30} {len(vals):>6}  {tot:>+14.1f}%  {avg:>+9.1f}%  {note:>15}")

    total_dodged = dodged + proxy
    if total_dodged:
        all_vals = [r["fwd_ret_3m"] for r in total_dodged]
        lines.append("  " + "-" * 80)
        lines.append(f"  {'TOTAL (incl. proxies)':<30} {len(all_vals):>6}  "
                     f"{sum(all_vals):>+14.1f}%  "
                     f"{sum(all_vals)/len(all_vals):>+9.1f}%  "
                     f"{'[floor]':>15}")

    if passed_bad:
        lines.append("")
        lines.append(f"  PASSED_GATE_LOSER: {len(passed_bad)} losers that cleared ALL gates")
        lines.append("  (These are bad entries -- review gate tightening opportunities)")
        pbl_sorted = sorted(passed_bad, key=lambda x: x["fwd_ret_3m"])
        for r in pbl_sorted[:10]:
            lines.append(f"    {r['date']:<12} {r['ticker']:<8} "
                         f"{(r['industry'] or '')[:30]:<30} {r['fwd_ret_3m']:>+7.1f}%")


def _write_gate_analysis(missed, dodged, window, lines):
    """
    Per-gate analysis: for each gate that appears in GATE_REJECTED records,
    report winners it rejected vs losers it would have admitted (defense record),
    margin distributions, and net expected effect of loosening.
    """
    gate_winners  = defaultdict(list)  # gate -> list of fwd_ret_3m values
    gate_losers   = defaultdict(list)  # gate -> list of fwd_ret_3m values (absolute)
    gate_margins  = defaultdict(list)  # gate -> list of gate_margins

    for r in missed:
        if r["category"] == CAT_GATE_REJ and r.get("gate_name"):
            gate_winners[r["gate_name"]].append(r["fwd_ret_3m"])
            if r.get("gate_margin") is not None:
                gate_margins[r["gate_name"]].append(r["gate_margin"])

    dodged_gate_rej = [r for r in dodged if r.get("which_filter") == CAT_GATE_REJ
                       and r.get("gate_name")]
    for r in dodged_gate_rej:
        gate_losers[r["gate_name"]].append(abs(r["fwd_ret_3m"]))

    all_gates = sorted(set(gate_winners.keys()) | set(gate_losers.keys()))
    if not all_gates:
        return

    lines.append("")
    lines.append(f"  PER-GATE ANALYSIS: Winners Rejected vs Losers Defended  --  {window}")
    lines.append("  " + "-" * 90)
    lines.append(f"  {'Gate':<30} {'W_Rej':>6}  {'W_AvgFg%':>9}  "
                 f"{'L_Def':>6}  {'L_AvgDD%':>9}  {'Net$Effect':>11}  {'Verdict':>12}")
    lines.append("  " + "-" * 90)

    for gate in all_gates:
        w_list = gate_winners.get(gate, [])
        l_list = gate_losers.get(gate, [])
        w_avg = sum(w_list) / len(w_list) if w_list else 0
        l_avg = sum(l_list) / len(l_list) if l_list else 0
        # Net effect: positive = loosening this gate would be beneficial
        # (more upside captured than downside admitted)
        net = (sum(w_list) / max(len(w_list), 1)) - (sum(l_list) / max(len(l_list), 1))
        verdict = ("LOOSEN_CANDIDATE" if net > 20 and len(w_list) >= 3
                   else "TIGHTEN_CANDIDATE" if net < -20 and len(l_list) >= 3
                   else "BALANCED")
        lines.append(
            f"  {gate[:30]:<30} {len(w_list):>6}  {w_avg:>+9.1f}%  "
            f"{len(l_list):>6}  {l_avg:>+9.1f}%  {net:>+11.1f}%  {verdict:>12}"
        )

    lines.append("")
    lines.append("  MARGIN DISTRIBUTION for top GATE_REJECTED missed winners:")
    lines.append("  (margin = how far score was below threshold for that gate)")
    for gate in sorted(gate_margins.keys(), key=lambda g: -len(gate_margins[g]))[:5]:
        margins = sorted(gate_margins[gate])
        p25 = margins[int(len(margins)*0.25)] if margins else 0
        p50 = margins[int(len(margins)*0.50)] if margins else 0
        p75 = margins[int(len(margins)*0.75)] if margins else 0
        lines.append(f"    {gate[:38]:<38} n={len(margins)}  "
                     f"p25={p25:.3f}  p50={p50:.3f}  p75={p75:.3f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Opportunity report from two-layer feature cache")
    parser.add_argument("--cache",  default=str(CACHE_DB),
                        help="Path to feature_cache.db")
    parser.add_argument("--output", default=str(OUTPUT_TXT),
                        help="Output .txt path")
    parser.add_argument("--window", choices=["IS", "OOS", "BOTH"], default="BOTH",
                        help="Which time window to analyze")
    args = parser.parse_args()

    cache_path  = Path(args.cache)
    output_path = Path(args.output)

    if not cache_path.exists():
        raise FileNotFoundError(
            f"Feature cache database not found: {cache_path.resolve()}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 70)
    print("  OPPORTUNITY REPORT")
    print(f"  Cache : {cache_path}")
    print(f"  Output: {output_path}")
    print("=" * 70)

    conn = sqlite3.connect(str(cache_path))
    conn.row_factory = sqlite3.Row

    # Check cache metadata for degradation flags
    meta_cur = conn.execute(
        "SELECT key, value FROM _cache_meta WHERE key IN "
        "('volume_data_available', 'n_rows', 'n_tradeable', 'n_tickers')"
    )
    meta = {r[0]: r[1] for r in meta_cur.fetchall()}
    volume_data_available = meta.get("volume_data_available", "True").lower() != "false"

    print(f"\n  Cache stats:")
    print(f"    Total rows    : {meta.get('n_rows', '?'):>12}")
    print(f"    Tradeable rows: {meta.get('n_tradeable', '?'):>12}")
    print(f"    Unique tickers: {meta.get('n_tickers', '?'):>12}")
    if not volume_data_available:
        print()
        print("  *** DEGRADATION: volume data unavailable -- counts are UNDER-COUNTED ***")

    # Load tester
    _tester = _load_tester()
    if _tester is None:
        print("  WARNING: tester.py unavailable -- GATE_REJECTED classification disabled")

    # Write report
    lines = [
        "OPPORTUNITY REPORT",
    ]
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Cache: {cache_path}")
    lines.append(f"Volume data: yfinance+SHARADAR/DAILY(mcap pre-filter)")
    lines.append("")
    lines.append(DELISTED_BIAS_CAVEAT)
    lines.append("")

    if not volume_data_available:
        lines.append("*** DEGRADATION WARNING: volume table inaccessible during cache build ***")
        lines.append("*** All EXCLUDED_LIQUIDITY, NOT_IN_OLD_UNIVERSE, GATE_REJECTED counts ***")
        lines.append("*** are UNDER-COUNTED. Re-build cache with SHARADAR/DAILY or SEP.    ***")
        lines.append("")

    all_summaries = []
    windows_to_run = (
        [("IS", *WINDOWS["IS"]), ("OOS", *WINDOWS["OOS"])]
        if args.window == "BOTH"
        else [(args.window, *WINDOWS[args.window])]
    )

    for window_label, start_date, end_date in windows_to_run:
        print(f"\n[{window_label}] Analyzing {start_date} -> {end_date} ...")
        summary = run_analysis(
            conn, window_label, start_date, end_date,
            _tester, lines,
            volume_data_available=volume_data_available,
        )
        all_summaries.append(summary)

    # Cross-window summary
    lines.append("")
    lines.append("=" * 80)
    lines.append("  CROSS-WINDOW SUMMARY")
    lines.append("=" * 80)
    for s in all_summaries:
        lines.append(f"  {s['window']:<6}  "
                     f"missed_winners={s['missed_winners']:>5}  "
                     f"dodged_losers={s['dodged_losers']:>5}  "
                     f"gate_replayed={s['gate_replayed']:>6}  "
                     f"elapsed={s['elapsed_s']:.1f}s")
        for cat, cnt in sorted(s.get("missed_by_cat", {}).items()):
            lines.append(f"         {cat:<28} {cnt:>5}")

    lines.append("")
    lines.append("INTERPRETATION KEY:")
    lines.append("  EXCLUDED_LIQUIDITY  -- policy filter (liquidity); not a gate finding")
    lines.append("  EXCLUDED_BIOTECH    -- policy filter (industry); not a gate finding")
    lines.append("  NOT_IN_OLD_UNIVERSE -- universe curation gap; not a gate finding")
    lines.append("  GATE_REJECTED       -- ONLY ACTUAL GATE FINDING")
    lines.append("                         These are the actionable items for gate tuning.")
    lines.append("                         See per-gate analysis above for loosening proposals.")
    lines.append("")
    if not volume_data_available:
        lines.append("*** ALL COUNTS ARE UNDER-COUNTED: volume table was inaccessible. ***")
        lines.append("*** Rebuild cache with NDL volume access for accurate numbers.    ***")

    conn.close()

    text = "\n".join(lines)
    with open(output_path, "w", encoding="ascii", errors="replace") as f:
        f.write(text)

    print(f"\n  Report written to: {output_path}")
    print(f"  {len(all_summaries[0].get('gate_findings', []))} GATE_REJECTED findings in "
          f"{all_summaries[0]['window']} window")


if __name__ == "__main__":
    main()
