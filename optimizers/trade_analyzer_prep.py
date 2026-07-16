#!/usr/bin/env python3
"""
trade_analyzer_prep.py  --  Stratified trade sampler for the two-agent optimizer
=================================================================================
Reads portfolio_report.json, draws a stratified 30-trade sample (balanced across
outcome × exit_reason cells), enriches each trade with its price history during
the hold period, and writes reports/sampled_trades.json for the Trade Analyst.

Usage:
  python scripts/trade_analyzer_prep.py
  python scripts/trade_analyzer_prep.py --n 30 --seed 42
"""

import argparse, json, math, random, sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed"); sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
try:
    import tester as _tester
    _TESTER_OK = True
except Exception:
    _TESTER_OK = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON  = PROJECT_ROOT / "reports" / "portfolio_report.json"
OUTPUT_JSON  = PROJECT_ROOT / "reports" / "sampled_trades.json"

# Exit-reason stratified sampling targets (must sum to 30)
EXIT_SAMPLE_TARGETS = {
    "BELOW_MA_DECLINING": 8,
    "TRAIL_STOP":         6,
    "MA50_CROSS":         4,
    "TAKE_PROFIT":        3,
    "MA100_BREAKDOWN":    3,
    "OTHER":              6,
}

_REGIME_DESC = {
    "BULL_STRONG":   "NDX above MA100, low volatility",
    "BULL_WEAK":     "NDX above MA100, high volatility",
    "BEAR_GRIND":    "NDX below MA100, low volatility",
    "BEAR_VOLATILE": "NDX below MA100, high volatility",
}

_qqq_data = None  # list of (date_str, close) sorted by date, loaded once


def _ensure_qqq_loaded():
    global _qqq_data
    if _qqq_data is not None:
        return
    try:
        df = yf.download("QQQ", start="2019-07-01", end="2025-06-01",
                         progress=False, auto_adjust=True)
        if df.empty:
            _qqq_data = []
            return
        closes = df["Close"].squeeze()
        _qqq_data = [(str(d.date()), float(v)) for d, v in closes.items()]
    except Exception:
        _qqq_data = []


def get_ndx_regime(date_str):
    """Return 4-state NDX regime label for the given date."""
    _ensure_qqq_loaded()
    HIGH_VOL_THR = 20.0
    avail = [c for d, c in _qqq_data if d <= date_str]
    if len(avail) < 25:
        return "BULL_STRONG"
    window = min(100, len(avail))
    ma100  = sum(avail[-window:]) / window
    above  = avail[-1] > ma100
    rets   = [(avail[i] - avail[i - 1]) / avail[i - 1] for i in range(1, len(avail))]
    if len(rets) < 5:
        return "BULL_STRONG"
    recent = rets[-20:]
    mean_r = sum(recent) / len(recent)
    var    = sum((r - mean_r) ** 2 for r in recent) / max(len(recent) - 1, 1)
    vol20  = math.sqrt(var) * (252 ** 0.5) * 100
    if above:
        return "BULL_STRONG" if vol20 < HIGH_VOL_THR else "BULL_WEAK"
    return "BEAR_VOLATILE" if vol20 >= HIGH_VOL_THR else "BEAR_GRIND"


def _regime_context(entry_regime, exit_regime):
    """Return a regime-context label for the hold period."""
    is_bull     = lambda r: r in ("BULL_STRONG", "BULL_WEAK")
    is_volatile = lambda r: r in ("BULL_WEAK", "BEAR_VOLATILE")
    if is_bull(entry_regime) != is_bull(exit_regime):
        return "REGIME_CHANGED_BEARISH" if is_bull(entry_regime) else "REGIME_CHANGED_BULLISH"
    if is_volatile(entry_regime) or is_volatile(exit_regime):
        return "VOLATILE_MIXED"
    return "STABLE_BULL" if is_bull(entry_regime) else "STABLE_BEAR"


def load_trades(report_path=None):
    path = Path(report_path) if report_path else REPORT_JSON
    with open(path) as f:
        data = json.load(f)
    return data["closed_trades"], path


def _base_exit_reason(reason):
    """Strip dynamic detail from exit reason (everything after first '(' or space+digit)."""
    return reason.split("(")[0].strip()


def stratified_sample(trades, n, rng):
    # Stratify by base exit reason; reasons not in EXIT_SAMPLE_TARGETS go to "OTHER"
    by_reason = defaultdict(list)
    for t in trades:
        reason = _base_exit_reason(t["exit_reason"])
        by_reason[reason if reason in EXIT_SAMPLE_TARGETS else "OTHER"].append(t)

    selected   = []
    other_slots = EXIT_SAMPLE_TARGETS["OTHER"]
    for reason, target in EXIT_SAMPLE_TARGETS.items():
        if reason == "OTHER":
            continue
        group = by_reason.get(reason, [])
        take  = min(target, len(group))
        selected.extend(rng.sample(group, take))
        other_slots += target - take  # underfilled slots spill into OTHER

    other_group = by_reason.get("OTHER", [])
    take_other  = min(other_slots, len(other_group))
    selected.extend(rng.sample(other_group, take_other))
    return selected


def fetch_price_history(ticker, entry_date, exit_date):
    # Include 10 calendar days before entry for run-up context
    start = (datetime.strptime(entry_date, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
    end   = (datetime.strptime(exit_date,  "%Y-%m-%d") + timedelta(days=135)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return []
        closes = df["Close"].squeeze()
        return [{"date": str(d.date()), "close": round(float(v), 2)} for d, v in closes.items()]
    except Exception as exc:
        return [{"error": str(exc)}]


def build_narrative(t, history):
    pnl_sign = "+" if t["pnl_pct"] >= 0 else ""
    entry_dt = t["entry_date"]
    exit_dt  = t["exit_date"]
    hold     = t.get("trading_days_held", "?")

    # NDX regime context
    regime_entry = get_ndx_regime(entry_dt)
    regime_exit  = get_ndx_regime(exit_dt)
    regime_ctx   = _regime_context(regime_entry, regime_exit)

    # Find entry/exit closes from history
    entry_close = next((h["close"] for h in history if h.get("date") == entry_dt), None)
    exit_close  = next((h["close"] for h in history if h.get("date") == exit_dt),  None)

    # Gate summary: flag barely-passed gates
    gate_lines = []
    gm = t.get("gate_margins") or {}
    for gate, info in gm.items():
        barely = " *** BARELY PASSED ***" if info.get("barely") else ""
        gate_lines.append(f"    {gate}: {info['score']:.1f}/{info['max_weight']:.1f}{barely}")
    gate_text = "\n".join(gate_lines) if gate_lines else "    (no gate data)"

    # Compute per-trade effective threshold and pass margin from live tester.py constants
    if _TESTER_OK:
        try:
            _tester._ndx_regime = regime_entry
            eff_thr = _tester.pass_threshold(t.get("universe", ""))
            is_cc   = t.get("universe", "").lower() in _tester.COUNTER_CYCLICAL
            adj     = 0.0 if is_cc else _tester.REGIME_ADJUSTMENTS.get(regime_entry, 0.0)
            base    = eff_thr - adj
            margin  = float(t.get("score") or 0.0) - eff_thr
            cc_note = "  (counter-cyclical: no regime adj)" if is_cc else ""
            qual    = "MARGINAL" if margin < 0.30 else ("comfortable" if margin > 1.00 else "moderate")
            thr_line    = f"  Effective threshold : {base:.2f} + {adj:+.2f}{cc_note} = {eff_thr:.2f}"
            margin_line = f"  Pass margin         : {margin:+.2f}  [{qual}]"
            rescue = float(t.get("rescue_bonus") or 0.0)
            if rescue != 0.0:
                gm_sum = sum(v.get("score", 0) for v in (t.get("gate_margins") or {}).values())
                score_breakdown = (f"  Score breakdown     : gate_margins={gm_sum:.2f}"
                                   f" + rescue_bonus={rescue:+.2f}"
                                   f" = {float(t.get('score') or 0.0):.2f}")
            else:
                score_breakdown = ""
            thr_block = thr_line + "\n" + margin_line + (("\n" + score_breakdown) if score_breakdown else "")
        except Exception:
            thr_block = ""
    else:
        thr_block = ""

    # Price path during hold (entry through exit only)
    path_rows = [h for h in history
                 if h.get("date") and entry_dt <= h["date"] <= exit_dt]
    if len(path_rows) > 20:
        step = (len(path_rows) - 2) // 18
        keep = [path_rows[0]] + path_rows[1:-1:max(step, 1)] + [path_rows[-1]]
        path_rows = keep
    path_text = "  " + "  ".join(
        f"{h['date']} ${h['close']:.2f}" for h in path_rows
    ) if path_rows else "  (no price data)"

    # Forward path from entry (+1w, +2w, +1m, +2m, +3m)
    _fwd_offsets = [7, 14, 30, 60, 90]
    _fwd_labels  = ["+1w", "+2w", "+1m", "+2m", "+3m"]
    _entry_px    = float(t["entry_price"])
    _fwd_all     = [h for h in history if h.get("date") and h["date"] >= entry_dt]
    _fwd_milestones: list = []
    _seen: set = set()
    for _off, _lbl in zip(_fwd_offsets, _fwd_labels):
        _tgt = (datetime.strptime(entry_dt, "%Y-%m-%d") + timedelta(days=_off)).strftime("%Y-%m-%d")
        _cl  = min(_fwd_all,
                   key=lambda h, _t=_tgt: abs(
                       (datetime.strptime(h["date"], "%Y-%m-%d") -
                        datetime.strptime(_t, "%Y-%m-%d")).days),
                   default=None)
        if _cl and _cl["date"] not in _seen:
            _seen.add(_cl["date"])
            _fwd_milestones.append((_lbl, _cl))
    if _fwd_milestones:
        _last_lbl, _last_h = _fwd_milestones[-1]
        _fwd3m_pct = (_last_h["close"] - _entry_px) / _entry_px * 100
        _fwd_outcome = ("STOCK_HAD_POTENTIAL" if _fwd3m_pct > 15
                        else "STOCK_WAS_WEAK" if _fwd3m_pct < -10 else "FLAT")
        _fwd_path_text = "  " + "  ".join(
            f"{lbl} {h['date']} ${h['close']:.2f}" for lbl, h in _fwd_milestones
        )
        fwd_section = (f"  Forward path from entry (entry @ ${_entry_px:.2f}):\n"
                       f"{_fwd_path_text}\n"
                       f"  Forward 3m change from entry: {_fwd3m_pct:+.1f}%  ({_fwd_outcome})")
    else:
        fwd_section = "  Forward path from entry: (no data available)"

    # Post-exit price path (up to 120 calendar days after exit)
    post_rows = [h for h in history if h.get("date") and h["date"] > exit_dt]
    if post_rows:
        exit_price_val = t["exit_price"]
        last_date = post_rows[-1]["date"]
        days_covered = (datetime.strptime(last_date, "%Y-%m-%d") -
                        datetime.strptime(exit_dt, "%Y-%m-%d")).days

        # Price closest to the 120-calendar-day mark (for the verdict line)
        target_120d = (datetime.strptime(exit_dt, "%Y-%m-%d") + timedelta(days=120)).strftime("%Y-%m-%d")
        ref_row = min(post_rows, key=lambda h: abs(
            (datetime.strptime(h["date"], "%Y-%m-%d") -
             datetime.strptime(target_120d, "%Y-%m-%d")).days
        ))
        pct_change = (ref_row["close"] - exit_price_val) / exit_price_val * 100
        if pct_change > 8:
            verdict = "(EXIT WAS PREMATURE)"
        elif pct_change < -5:
            verdict = "(STOCK CONTINUED FALLING)"
        else:
            verdict = "(FLAT)"

        # Thin to ~10 weekly snapshots when more than 20 post-exit trading days
        display_rows = post_rows
        if len(post_rows) > 20:
            step = max(1, (len(post_rows) - 2) // 8)
            display_rows = [post_rows[0]] + post_rows[1:-1:step] + [post_rows[-1]]

        post_path_text = "  " + "  ".join(
            f"{h['date']} ${h['close']:.2f}" for h in display_rows
        )
        window_label = ("up to 120 days after sell" if days_covered >= 115
                        else f"{days_covered} calendar days after sell")
        post_section = (f"  Post-exit price path ({window_label}):\n"
                        f"{post_path_text}\n"
                        f"  120-day change from exit: {pct_change:+.1f}%  {verdict}")
    else:
        post_section = "  Post-exit price path: (no data available)"

    narrative = f"""TRADE #{t['trade_id']}  {t['ticker']}  [{t['sector']} / {t.get('universe', '')}]
  Entry : {entry_dt} @ ${t['entry_price']:.2f}   Exit : {exit_dt} @ ${t['exit_price']:.2f}
  Peak  : ${t.get('peak_price') or 0:.2f}   Hold : {hold} trading days
  PnL   : {pnl_sign}{t['pnl_pct']:.1f}%  (${pnl_sign}{t['pnl_dollars']:.0f})
  Exit reason   : {t['exit_reason']}
  Regime at entry : {regime_entry}  ({_REGIME_DESC.get(regime_entry, '')})
  Regime at exit  : {regime_exit}  ({_REGIME_DESC.get(regime_exit, '')})
  Regime context  : {regime_ctx} during hold
  Conviction    : {t.get('conviction', '?')}   Score : {t.get('score', '?')}
{thr_block}
  Gate scores at entry:
{gate_text}
  Price path (entry -> exit):
{path_text}
{fwd_section}
{post_section}"""
    return narrative


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n",               type=int, default=30, help="number of trades to sample")
    ap.add_argument("--seed",            type=int, default=42, help="random seed")
    ap.add_argument("--analysis-report", default=None,
                    help="Override source portfolio_report.json (default: reports/portfolio_report.json)")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    trades, report_used = load_trades(args.analysis_report)
    print(f"Loaded {len(trades)} closed trades from {report_used.name}")

    sampled = stratified_sample(trades, args.n, rng)
    exit_counts = Counter(
        r if r in EXIT_SAMPLE_TARGETS else "OTHER"
        for t in sampled
        for r in [_base_exit_reason(t["exit_reason"])]
    )
    parts = [f"{r}={exit_counts.get(r, 0)}" for r in EXIT_SAMPLE_TARGETS]
    print(f"Sampled {len(sampled)} trades (by exit reason): " + "  ".join(parts))

    enriched = []
    for i, t in enumerate(sampled, 1):
        print(f"  [{i:02d}/{len(sampled)}] {t['ticker']:6s}  {t['entry_date']} -> {t['exit_date']}  {'+' if t['pnl_pct']>=0 else ''}{t['pnl_pct']:.1f}%  {t['exit_reason']}")
        history = fetch_price_history(t["ticker"], t["entry_date"], t["exit_date"])

        record = {
            "trade_id": i,
            "ticker":             t["ticker"],
            "sector":             t["sector"],
            "universe":           t.get("universe", ""),
            "entry_date":         t["entry_date"],
            "exit_date":          t["exit_date"],
            "entry_price":        t["entry_price"],
            "exit_price":         t["exit_price"],
            "peak_price":         t.get("peak_price"),
            "pnl_pct":            t["pnl_pct"],
            "pnl_dollars":        t["pnl_dollars"],
            "exit_reason":        t["exit_reason"],
            "confidence":         t.get("confidence"),
            "conviction":         t.get("conviction"),
            "score":              t.get("score"),
            "threshold":          t.get("threshold"),
            "rescue_bonus":       t.get("rescue_bonus", 0.0),
            "trading_days_held":  t.get("trading_days_held"),
            "gate_margins":       t.get("gate_margins", {}),
            "price_history":      history,
            "narrative":          build_narrative({**t, "trade_id": i}, history),
        }
        enriched.append(record)

    output = {
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_report":  str(report_used),
        "n_total_trades": len(trades),
        "n_sampled":      len(sampled),
        "trades":         enriched,
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(enriched)} enriched trades -> {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
