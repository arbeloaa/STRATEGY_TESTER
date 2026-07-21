import sys
import sqlite3
import numpy as np
from pathlib import Path

# Add project root and engine to sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "engine"))
sys.path.insert(0, str(_PROJECT_ROOT / "data_pipeline"))

import tester as _tester
from data_fetcher import SECTORS, DELISTED_TICKERS

# Build ticker-to-sector lookup map
ticker_to_sector = {}
for sec_name, tks in SECTORS.items():
    for t in tks:
        ticker_to_sector[t] = sec_name
for t, info in DELISTED_TICKERS.items():
    ticker_to_sector[t] = info["sector"]

def sf(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def get_percentile_by_date(conn):
    """
    Compute daily percentiles and medians per sector over all tradeable candidate rows.
    """
    cur = conn.execute(
        """
        SELECT date, ticker, gm_pct, ps_ratio, rule40, revenue_growth
        FROM feature_cache
        WHERE tradeable_flag = 1
        """
    )
    rows = cur.fetchall()
    
    # Group by (date, sector)
    by_date_sector = {}
    for r in rows:
        dt = r[0]
        ticker = r[1]
        sector = ticker_to_sector.get(ticker)
        if not sector:
            continue
            
        key = (dt, sector)
        if key not in by_date_sector:
            by_date_sector[key] = {'gm': [], 'ps': [], 'r40': [], 'rev': []}
        if r[2] is not None: by_date_sector[key]['gm'].append(r[2])
        if r[3] is not None: by_date_sector[key]['ps'].append(r[3])
        if r[4] is not None: by_date_sector[key]['r40'].append(r[4])
        if r[5] is not None: by_date_sector[key]['rev'].append(r[5])
        
    stats_by_date_sector = {}
    for key, data in by_date_sector.items():
        stats_by_date_sector[key] = {
            'gm_p40': np.percentile(data['gm'], 40) if data['gm'] else 0.0,
            'ps_median': np.median(data['ps']) if data['ps'] else 0.0,
            'rule40_p40': np.percentile(data['r40'], 40) if data['r40'] else 0.0,
            'rev_p25': np.percentile(data['rev'], 25) if data['rev'] else 0.0
        }
    return stats_by_date_sector

def _cache_row_to_tester_row(cache_row):
    ticker = cache_row.get("ticker", "")
    sector = ticker_to_sector.get(ticker, cache_row.get("sharadar_industry", ""))
    return {
        "Ticker":               ticker,
        "Sector":               sector,
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
        "SMA20":                1.0,
        "Price":                1.0,
    }

def get_original_rejection_reason(gate_results, score, threshold, veto, veto_reason):
    if veto:
        return "VETO", veto_reason
    if score >= threshold:
        return "PASS", ""
    # Find the gate that caused the failure
    min_margin = 999
    worst_gate = "UNKNOWN"
    for gname, (passed, weight, note, is_proxy, score_override) in gate_results.items():
        if not passed:
            margin = weight if score_override is None else weight - score_override
            if margin < min_margin:
                min_margin = margin
                worst_gate = gname
    return "GATE_REJ", worst_gate

def evaluate_variant(row, stats, variant_name, original_gates, original_score, threshold, veto, veto_reason):
    dt = row["date"]
    trow = _cache_row_to_tester_row(row)
    sector = trow["Sector"]
    sec_stats = stats.get((dt, sector))
    if sec_stats is None:
        sec_stats = {'gm_p40': 0.0, 'ps_median': 0.0, 'rule40_p40': 0.0, 'rev_p25': 0.0}
    
    # Copy original gate results structure
    modified_gates = {}
    for gname, val in original_gates.items():
        modified_gates[gname] = list(val) # [passed, weight, note, is_proxy, score_override]
        
    # Variant definitions
    if variant_name == "GM≥p40":
        # Modify G2
        g2_key = "G2 Gross Margin"
        if g2_key in modified_gates:
            val = sf(row["gm_pct"])
            passed = val >= sf(sec_stats["gm_p40"])
            weight = modified_gates[g2_key][1]
            modified_gates[g2_key][0] = passed
            modified_gates[g2_key][4] = weight if passed else 0.0
            
    elif variant_name == "P/S≤median×1.5":
        # Modify G1
        g1_key = "G1 Valuation"
        if g1_key in modified_gates:
            val = row["ps_ratio"]
            if val is None or float(val) == 999.0:
                passed = False
            else:
                passed = float(val) <= (sf(sec_stats["ps_median"]) * 1.5)
            weight = modified_gates[g1_key][1]
            modified_gates[g1_key][0] = passed
            modified_gates[g1_key][4] = weight if passed else 0.0
            
    elif variant_name == "R40≥p40":
        # Modify G3
        g3_key = None
        for k in modified_gates.keys():
            if k.startswith("G3"):
                g3_key = k
                break
        if g3_key:
            val = sf(row["rule40"])
            passed = val >= sf(sec_stats["rule40_p40"])
            weight = modified_gates[g3_key][1]
            modified_gates[g3_key][0] = passed
            modified_gates[g3_key][4] = weight if passed else 0.0
            
    elif variant_name == "RevGrowth≥p25":
        # Modify G5
        g5_key = None
        for k in modified_gates.keys():
            if k.startswith("G5"):
                g5_key = k
                break
        if g5_key:
            val = sf(row["revenue_growth"])
            passed = val >= sf(sec_stats["rev_p25"])
            weight = modified_gates[g5_key][1]
            modified_gates[g5_key][0] = passed
            modified_gates[g5_key][4] = weight if passed else 0.0
            
    # Re-calculate score
    w_score, max_pos, nd, np_ = _tester.score_gates(modified_gates)
    
    trow = _cache_row_to_tester_row(row)
    sector = trow["Sector"]
    univ_map = _tester.SECTOR_MAP
    univ_info = univ_map.get(sector)
    universe, sub = univ_info
    
    rescue = _tester.compute_momentum_rescue(
        modified_gates, w_score, threshold, trow, universe
    )
    total_score = w_score + rescue
    passed_all = (total_score >= threshold) and not veto
    
    # Specific gate pass status
    specific_passed = True
    if variant_name == "GM≥p40":
        specific_passed = modified_gates.get("G2 Gross Margin", (False,))[0]
    elif variant_name == "P/S≤median×1.5":
        specific_passed = modified_gates.get("G1 Valuation", (False,))[0]
    elif variant_name == "R40≥p40":
        g3_key = next((k for k in modified_gates if k.startswith("G3")), None)
        specific_passed = modified_gates[g3_key][0] if g3_key else False
    elif variant_name == "RevGrowth≥p25":
        g5_key = next((k for k in modified_gates if k.startswith("G5")), None)
        specific_passed = modified_gates[g5_key][0] if g5_key else False
        
    return passed_all, specific_passed

def main():
    db_path = "data/feature_cache_local.db"
    print(f"Connecting to {db_path}...")
    conn = sqlite3.connect(db_path)
    print("Computing stats by date...")
    stats = get_percentile_by_date(conn)
    
    # Fetch candidate rows in scope
    cur = conn.execute(
        """
        SELECT date, ticker, sharadar_industry, tradeable_flag,
               ps_ratio, ps_growth, gm_pct, gm_erosion, rule40, fcf_margin,
               roic, share_growth, inv_days, inv_trend, pricing_power,
               revenue_growth, capex_sales,
               price_vs_ma200, price_vs_ma100, price_vs_ma50,
               return_6m, rs_score, regime,
               fwd_ret_3m
        FROM feature_cache
        WHERE fwd_ret_3m IS NOT NULL
          AND tradeable_flag = 1
        ORDER BY date, ticker
        """
    )
    cols = [d[0] for d in cur.description]
    all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    
    # Filter to only our active universe
    old_universe_tickers = set()
    for tks in SECTORS.values():
        old_universe_tickers.update(tks)
    old_universe_tickers.update(DELISTED_TICKERS.keys())
    
    candidates = [r for r in all_rows if r["ticker"] in old_universe_tickers]
    print(f"Loaded {len(candidates)} active universe candidate rows.")
    
    windows = {
        "IS":  ("2020-01-01", "2024-12-31"),
        "OOS": ("2025-01-01", "2026-06-30")
    }
    
    variants = ["GM≥p40", "P/S≤median×1.5", "R40≥p40", "RevGrowth≥p25"]
    
    for win_name, (start, end) in windows.items():
        win_rows = [r for r in candidates if r["date"] >= start and r["date"] <= end]
        print(f"\n=============================================================")
        print(f"WINDOW: {win_name} ({start} -> {end}) - N={len(win_rows)}")
        print(f"=============================================================")
        
        # Pre-compute original scoring for each row in window
        row_original_status = []
        for r in win_rows:
            trow = _cache_row_to_tester_row(r)
            sector = trow["Sector"]
            univ_map = _tester.SECTOR_MAP
            univ_info = univ_map.get(sector)
            if univ_info is None:
                continue
            universe, sub = univ_info
            
            _tester._ndx_regime = r["regime"] or "BULL_STRONG"
            sector_pct_rank = 50.0
            
            if universe == "energy":
                gate_results = _tester.gates_energy(trow, sub, sector_pct_rank)
            elif universe == "tech":
                gate_results = _tester.gates_tech(trow, sub, sector_pct_rank)
            elif universe == "medtech":
                gate_results = _tester.gates_medtech(trow, sub, sector_pct_rank)
            elif universe == "semi":
                gate_results = _tester.gates_semi(trow, sub, sector_pct_rank)
                
            veto, veto_reason = _tester.check_veto(trow, sector_pct_rank)
            w_score, max_pos, nd, np_ = _tester.score_gates(gate_results)
            rescue = _tester.compute_momentum_rescue(
                gate_results, w_score, _tester.pass_threshold(universe), trow, universe
            )
            score = w_score + rescue
            thr = _tester.pass_threshold(universe)
            orig_pass_all = (score >= thr) and not veto
            
            # Find specific gate status
            # We map specific gates for comparison
            # G2 Gross Margin
            orig_pass_g2 = gate_results.get("G2 Gross Margin", (False,))[0]
            # G1 Valuation
            orig_pass_g1 = gate_results.get("G1 Valuation", (False,))[0]
            # G3 Capital Eff / Rule of 40
            g3_key = next((k for k in gate_results if k.startswith("G3")), None)
            orig_pass_g3 = gate_results[g3_key][0] if g3_key else False
            # G5 Leverage / Op Leverage
            g5_key = next((k for k in gate_results if k.startswith("G5")), None)
            orig_pass_g5 = gate_results[g5_key][0] if g5_key else False
            
            row_original_status.append({
                "row": r,
                "orig_pass_all": orig_pass_all,
                "orig_pass_gates": {
                    "GM≥p40": orig_pass_g2,
                    "P/S≤median×1.5": orig_pass_g1,
                    "R40≥p40": orig_pass_g3,
                    "RevGrowth≥p25": orig_pass_g5
                },
                "gate_results": gate_results,
                "score": score,
                "thr": thr,
                "veto": veto,
                "veto_reason": veto_reason
            })
            
        # Analyze each variant
        for var in variants:
            # We want to split results by PASSED_ALL (strategy-level) and PASSED_ANY (gate-level)
            
            # 1. PASSED_ALL (Strategy-level) Analysis
            gained_all = []
            lost_all = []
            
            # 2. PASSED_ANY (Gate-level) Analysis
            gained_any = []
            lost_any = []
            
            for orig in row_original_status:
                r = orig["row"]
                fwd = r["fwd_ret_3m"]
                is_winner = fwd > 0.30
                is_loser = fwd < -0.25
                
                new_pass_all, new_pass_gate = evaluate_variant(
                    r, stats, var, orig["gate_results"], orig["score"], orig["thr"], orig["veto"], orig["veto_reason"]
                )
                
                # Full strategy level change
                if not orig["orig_pass_all"] and new_pass_all:
                    gained_all.append(r)
                elif orig["orig_pass_all"] and not new_pass_all:
                    lost_all.append(r)
                    
                # Gate level change
                orig_pass_gate = orig["orig_pass_gates"][var]
                if not orig_pass_gate and new_pass_gate:
                    gained_any.append(r)
                elif orig_pass_gate and not new_pass_gate:
                    lost_any.append(r)
                    
            def get_stats_block(gained, lost):
                gained_winners = [x for x in gained if x["fwd_ret_3m"] > 0.30]
                gained_losers = [x for x in gained if x["fwd_ret_3m"] < -0.25]
                lost_winners = [x for x in lost if x["fwd_ret_3m"] > 0.30]
                lost_losers = [x for x in lost if x["fwd_ret_3m"] < -0.25]
                
                gained_rets = [x["fwd_ret_3m"] for x in gained]
                lost_rets = [x["fwd_ret_3m"] for x in lost]
                net_ret = sum(gained_rets) - sum(lost_rets)
                
                return {
                    "gained_count": len(gained),
                    "lost_count": len(lost),
                    "new_winners": len(gained_winners),
                    "new_losers": len(gained_losers),
                    "lost_winners": len(lost_winners),
                    "lost_losers": len(lost_losers),
                    "net_count": len(gained) - len(lost),
                    "net_ret_pct": round(net_ret * 100, 1)
                }
                
            stats_all = get_stats_block(gained_all, lost_all)
            stats_any = get_stats_block(gained_any, lost_any)
            
            print(f"\nVariant: {var}")
            print(f"  PASSED_ALL (Strategy Level):")
            print(f"    Gained: {stats_all['gained_count']} | Lost: {stats_all['lost_count']} | Net Change: {stats_all['net_count']:+}")
            print(f"    Missed Winners newly caught: {stats_all['new_winners']} | Losers newly admitted: {stats_all['new_losers']}")
            print(f"    Winners lost: {stats_all['lost_winners']} | Losers saved/lost: {stats_all['lost_losers']}")
            print(f"    Net Return Effect: {stats_all['net_ret_pct']:+}%")
            
            print(f"  PASSED_ANY (Gate Level):")
            print(f"    Gained: {stats_any['gained_count']} | Lost: {stats_any['lost_count']} | Net Change: {stats_any['net_count']:+}")
            print(f"    Winners newly passing gate: {stats_any['new_winners']} | Losers newly passing gate: {stats_any['new_losers']}")
            print(f"    Winners now failing gate: {stats_any['lost_winners']} | Losers now failing gate: {stats_any['lost_losers']}")
            print(f"    Net Return Effect: {stats_any['net_ret_pct']:+}%")

if __name__ == "__main__":
    main()
