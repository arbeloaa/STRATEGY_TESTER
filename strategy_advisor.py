#!/usr/bin/env python3
"""
strategy_advisor.py -- Capital Markets Expert strategic review
===============================================================
Single-agent analysis (Claude Opus) of the complete strategy state.
Reads all available evidence and produces a structured strategic report.

Not part of the optimizer loop. Run manually:
  python strategy_advisor.py                 # full analysis
  python strategy_advisor.py --output-only   # print last report without re-running
  python strategy_advisor.py --fresh-walkforward  # run walk-forward first, then analyze
Cost: ~$0.50-1.50 per run depending on history size.
Output: reports/strategy_advisor_report.txt (plus timestamped copies in reports/advisor_history/)
"""

import argparse, ast, json, re, shutil, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    import anthropic as _anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False
    _anthropic = None

# ============================================================================
#  PATHS + CONSTANTS
# ============================================================================
PROJECT_ROOT   = Path(__file__).resolve().parent
PARAMS_JSON    = PROJECT_ROOT / "config" / "strategy_params.json"
BEST_JSON      = PROJECT_ROOT / "config" / "current_best_params.json"
HISTORY_JSON   = PROJECT_ROOT / "logs" / "params_history.json"
CHANGE_LOG     = PROJECT_ROOT / "logs" / "change_log.txt"
REPORT_JSON    = PROJECT_ROOT / "reports" / "portfolio_report.json"
REPORT_TXT     = PROJECT_ROOT / "reports" / "portfolio_report.txt"
SAMPLED_JSON   = PROJECT_ROOT / "reports" / "sampled_trades.json"
ADVISOR_REPORT = PROJECT_ROOT / "reports" / "strategy_advisor_report.txt"
ADVISOR_HIST   = PROJECT_ROOT / "reports" / "advisor_history"

MODEL            = "claude-opus-4-8"
MAX_TOKENS       = 8000
MAX_INPUT_TOKENS = 150_000
COST_IN_PER_1M   = 15.00
COST_OUT_PER_1M  = 75.00


# ============================================================================
#  ARCHITECTURE (hardcoded, ~1K tokens)
# ============================================================================

_ARCHITECTURE = """\
STRATEGY ARCHITECTURE
=====================
Universe   : ~200 US tech, growth, and clean-energy stocks
Window     : 2020-01-01 to 2024-12-31 (5 years, includes covid crash, 2022 bear, recovery, AI bull)
Capital    : $100,000 starting; commission $0.01/share min $2.50/trade
Data       : Point-in-time fundamentals from SQLite (no look-ahead bias on earnings releases)
Reporting lag: 90 days (fundamentals available with 90-day delay from fiscal quarter end)

ENTRY GATES  (all G1-G8 evaluated at each rebalance; stock must pass combined score threshold)
  G1  Valuation     P/S <= 25 OR P/S / rev_growth <= 3.5
  G2  Gross Margin  Two-tier system, sector-specific thresholds:
                    >= gm_tops  -> full score (1.0x weight)
                    >= gm_mids  -> partial score (0.7x weight)
                    <  gm_mids  -> G2 FAILS, entry blocked entirely
                    Tech sectors use [gm_tops, gm_mids] pair in gm_configs
                    Solar/renewable use separate gm_tops / gm_mids dicts
  G3  Rule of 40    revenue_growth% + op_margin% >= 25
  G4  NRR           Net Revenue Retention above threshold (weight 0.9)
  G5  Op Leverage   YoY operating margin improvement > 0
  G6  Dilution      Share count YoY growth < 3.0%
  G7  Platform Power Composite proxy: switching costs + network effects signals
  G8  Momentum      126-day price return vs SPY >= 5%

Conviction (HIGH/MED/LOW) assigned from total weighted gate score.
  HIGH -> 1.5x position size mult   MED -> 1.0x   LOW -> 0.4x

REGIME-BASED GATE ADJUSTMENTS (applied to score thresholds, currently at defaults, UNEXPLORED):
  BULL_STRONG: -0.5 (easier entry)   BULL_WEAK: -0.2
  BEAR_GRIND: +0.3 (harder entry)    BEAR_VOLATILE: +0.6

POSITION SIZING (ALL parameters in this family are UNEXPLORED by the optimizer):
  Base:    per_buy_fraction (0.15) of available cash x conviction_mult
  Regime:  BULL_STRONG 1.0x / BULL_WEAK 0.8x / BEAR_GRIND 0.5x / BEAR_VOLATILE 0.35x
  Max pos: BULL_STRONG 30 / BULL_WEAK 25 / BEAR_GRIND 15 / BEAR_VOLATILE 10
  Hard cap: no single position > 10% of equity
  Pyramiding: up to 2 adds per position when gain > 10% since last add

EXIT LOGIC (checked in this priority order each trading day)
  TAKE_PROFIT      gain from avg_cost > 75%  -> exit immediately
  TRAIL_STOP       activates when gain > 10%; exits if price falls > 14% from peak
  MA50_CROSS       4 consecutive days below MA50
  BELOW_MA_DECLINING price below both MA50+MA100, AND 20d return < -8.5%
  MA100_BREAKDOWN  10 consecutive days below MA100, confirmed when below by >=3%
  GM_EROSION_VETO  gross margin drops > 20% cyclical / 12% non-cyclical from entry
  MAX_HOLD         250 trading days regardless of price

NDX REGIME DETECTION (daily, using QQQ as Nasdaq-100 proxy)
  Inputs: QQQ vs its 100-day MA, plus 20-day realized annualized volatility
  BULL_STRONG: QQQ > MA100 AND vol < 20%
  BULL_WEAK:   QQQ > MA100 AND vol >= 20%
  BEAR_GRIND:  QQQ < MA100 AND vol < 20%
  BEAR_VOLATILE: QQQ < MA100 AND vol >= 20%

FITNESS FORMULA: total_return_pct * 0.5 + (sharpe_ratio * 20) * 0.5
  (Sharpe scaled by 20 so that 0.5 Sharpe contributes 10 fitness points, comparable to 10% return)

GUARDRAILS (any violation causes the change to be reverted immediately):
  max_drawdown > -45%
  n_closed_trades >= 60% of baseline (prevents going to cash)
  avg_cash_pct <= 40%

OPTIMIZATION STATUS:
  47 total iterations, 8 kept, 39 reverted
  Exhausted families: MA_exit_sensitivity, profit_capture (all members tested)
  Unexplored families: position_sizing (9 members, zero attempts), regime_gate_adjustments (6 members)
  Current best fitness: 63.33 (confirmed reproducible; earlier 70.19 figure is unverifiable)
"""


# ============================================================================
#  DATA GATHERING
# ============================================================================

def _safe_load_json(path, label):
    p = Path(path)
    if not p.exists():
        return None, f"FILE NOT FOUND: {label} ({p})"
    try:
        with open(p) as f:
            return json.load(f), None
    except Exception as e:
        return None, f"LOAD ERROR {label}: {e}"


def gather_params():
    lines = ["CURRENT PARAMETER STATE", "=" * 40]
    for path, label in [(PARAMS_JSON, "strategy_params.json (live scratchpad)"),
                        (BEST_JSON,   "current_best_params.json (confirmed best)")]:
        data, err = _safe_load_json(path, label)
        if err:
            lines.append(err)
        else:
            lines.append(f"\n{label}:")
            lines.append(json.dumps(data, indent=2))
    return "\n".join(lines)


def gather_history():
    data, err = _safe_load_json(HISTORY_JSON, "params_history.json")
    if err:
        return err, []
    lines = [f"COMPLETE PARAMETER CHANGE HISTORY ({len(data)} entries)",
             "=" * 40]
    for e in data:
        outcome = "KEPT" if e.get("kept") else "REVERTED"
        lines.append(
            f"  [{e.get('ts','')}] iter={e.get('iter','?')}  {outcome}  "
            f"{e.get('param_path','?')}: {e.get('old_value')} -> {e.get('new_value')}  "
            f"fitness {e.get('fitness_before','?')} -> {e.get('fitness_after','?')}  "
            f"delta={_delta_str(e)}  reason: {str(e.get('rationale',''))[:120]}"
        )
    return "\n".join(lines), data


def _delta_str(e):
    fb = e.get("fitness_before")
    fa = e.get("fitness_after")
    if fb is not None and fa is not None:
        try:
            return f"{float(fa) - float(fb):+.4f}"
        except (TypeError, ValueError):
            pass
    return "?"


def gather_portfolio_metrics():
    data, err = _safe_load_json(REPORT_JSON, "portfolio_report.json")
    if err:
        return err

    lines = ["PORTFOLIO PERFORMANCE (2020-2024 full window)", "=" * 40]

    # Summary
    s = data.get("summary", {})
    lines += [
        "\nSUMMARY METRICS:",
        f"  Total return         : {s.get('total_return_pct', '?'):+.2f}%",
        f"  CAGR                 : {s.get('cagr', '?'):+.2f}%",
        f"  Sharpe ratio         : {s.get('sharpe', '?'):.3f}",
        f"  Max drawdown         : {s.get('max_drawdown', '?'):+.2f}%",
        f"  Win rate             : {s.get('win_rate', '?'):.1f}%",
        f"  Avg win / avg loss   : {s.get('avg_win', '?'):+.2f}% / {s.get('avg_loss', '?'):+.2f}%",
        f"  Profit factor        : {s.get('profit_factor', '?'):.3f}",
        f"  SPY return (same window): {s.get('spy_return', '?'):+.2f}%",
        f"  Alpha vs SPY         : {s.get('alpha', '?'):+.2f}%",
        f"  SPY Sharpe           : {s.get('spy_sharpe', '?'):.3f}",
        f"  Avg cash %           : {s.get('avg_cash_pct', '?'):.1f}%",
        f"  Total commissions    : ${s.get('total_commissions', 0):,.2f}",
        f"  Trades closed        : {s.get('n_closed', '?')}",
        f"  Trades open (end)    : {s.get('n_open', '?')}",
        f"  Fitness              : {s.get('total_return_pct', 0) * 0.5 + s.get('sharpe', 0) * 20 * 0.5:.4f}",
    ]

    # Concentration
    c = data.get("concentration", {})
    lines += [
        "\nCONCENTRATION RISK:",
        f"  Top 1 position: {c.get('top1_name','?')} = {c.get('top1_pct','?')}% of portfolio P&L",
        f"  Top 3 positions: {c.get('top3_names','?')} = {c.get('top3_pct','?')}% of P&L",
        f"  Top 5: {c.get('top5_pct','?')}% of P&L",
        f"  Excl. top 2 names: profit_factor={c.get('excl_top2_pf','?'):.2f}  ({c.get('excl_top2_trades','?')} trades)",
    ]

    # Sector breakdown
    ss = data.get("sector_stats", {})
    lines.append("\nSECTOR BREAKDOWN (sorted by P&L):")
    lines.append(f"  {'Sector':<35} {'N':>5} {'Wins':>5} {'WR%':>6} {'P&L $':>10} {'Avg%':>7}")
    lines.append("  " + "-" * 72)
    for sector, stats in sorted(ss.items(), key=lambda x: -x[1].get("pnl_dollars", 0)):
        wr = stats["wins"] / stats["n"] * 100 if stats["n"] > 0 else 0
        lines.append(
            f"  {sector:<35} {stats['n']:>5} {stats['wins']:>5} {wr:>6.1f}%"
            f" {stats['pnl_dollars']:>10,.0f} {stats['avg_ret_pct']:>+7.1f}%"
        )

    # Exit reason distribution (computed from closed_trades)
    trades = data.get("closed_trades", [])
    exit_counts = Counter(t["exit_reason"].split("(")[0].strip() for t in trades)
    exit_pnl    = defaultdict(float)
    exit_wins   = defaultdict(int)
    for t in trades:
        r = t["exit_reason"].split("(")[0].strip()
        exit_pnl[r]  += t["pnl_dollars"]
        if t["pnl_pct"] > 0:
            exit_wins[r] += 1

    lines.append("\nEXIT REASON DISTRIBUTION:")
    lines.append(f"  {'Exit Reason':<28} {'Count':>6} {'Wins':>6} {'WR%':>6} {'P&L $':>11}")
    lines.append("  " + "-" * 62)
    for reason, cnt in exit_counts.most_common():
        wr = exit_wins[reason] / cnt * 100 if cnt > 0 else 0
        lines.append(
            f"  {reason:<28} {cnt:>6} {exit_wins[reason]:>6} {wr:>6.1f}%"
            f" {exit_pnl[reason]:>11,.0f}"
        )

    # Diagnostics
    diag = data.get("diagnostics", {})
    sig1 = diag.get("signal1_bad_entries", {})
    sig2 = diag.get("signal2_premature_exits", {})
    sig3 = diag.get("signal3_exit_quality", {})
    lines += [
        "\nDIAGNOSTIC SIGNALS:",
        f"  Bad entries (losers that fell below entry within 15 days):",
        f"    {sig1.get('n_bad_entries','?')} of {sig1.get('n_total_losers','?')} losers"
        f"  (${sig1.get('total_pnl_dollars',0):,.0f} total loss)",
        f"    Top gate barely-passed: {sig1.get('top_barely_gates',[])}",
        f"  Premature exits (price recovered >8% within 60 days of sell):",
        f"    {sig2.get('n_premature','?')} of {sig2.get('n_total_closed','?')} exits"
        f"  (${sig2.get('total_dollars_missed',0):,.0f} gain left on table)",
        f"    By exit reason: {json.dumps(sig2.get('by_exit_reason', {}))}",
        f"  Exit quality breakdown:",
        f"    Good exits: {sig3.get('n_good_exits','?')} ({sig3.get('pct_good','?'):.1f}%)",
        f"    Premature:  {sig3.get('n_premature','?')} ({sig3.get('pct_premature','?'):.1f}%)",
        f"    Rode down:  {sig3.get('n_rode_down','?')} ({sig3.get('pct_rode_down','?'):.1f}%)",
    ]

    # Top 10 winners / losers
    sorted_trades = sorted(trades, key=lambda t: t["pnl_dollars"], reverse=True)
    lines.append("\nTOP 10 WINNING TRADES:")
    lines.append(f"  {'Ticker':>6}  {'Entry':>10}  {'Exit':>10}  {'PnL%':>7}  {'PnL$':>8}  Exit Reason")
    for t in sorted_trades[:10]:
        lines.append(
            f"  {t['ticker']:>6}  {t['entry_date']:>10}  {t['exit_date']:>10}"
            f"  {t['pnl_pct']:>+7.1f}%  {t['pnl_dollars']:>8,.0f}"
            f"  {t['exit_reason'].split('(')[0].strip()}"
        )
    lines.append("\nTOP 10 LOSING TRADES:")
    lines.append(f"  {'Ticker':>6}  {'Entry':>10}  {'Exit':>10}  {'PnL%':>7}  {'PnL$':>8}  Exit Reason")
    for t in sorted_trades[-10:][::-1]:
        lines.append(
            f"  {t['ticker']:>6}  {t['entry_date']:>10}  {t['exit_date']:>10}"
            f"  {t['pnl_pct']:>+7.1f}%  {t['pnl_dollars']:>8,.0f}"
            f"  {t['exit_reason'].split('(')[0].strip()}"
        )

    return "\n".join(lines)


def gather_walk_forward_from_log():
    """Parse the most recent WALK_FORWARD_CHECK entry from change_log.txt."""
    if not CHANGE_LOG.exists():
        return "WALK-FORWARD: change_log.txt not found -- no historical walk-forward data available."

    text = CHANGE_LOG.read_text(encoding="utf-8", errors="replace")
    wf_lines = [l for l in text.splitlines()
                if "WALK_FORWARD" in l and "per_period=" in l]
    if not wf_lines:
        return "WALK-FORWARD: no WALK_FORWARD_CHECK entries found in change_log.txt."

    last = wf_lines[-1]
    ts_m      = re.search(r"\[([^\]]+)\]", last)
    verdict_m = re.search(r"verdict=(\w+)", last)
    spread_m  = re.search(r"spread=([\d.]+)", last)
    avg_m     = re.search(r"avg_fitness=([\d.]+)", last)
    period_m  = re.search(r"per_period=(\{.*\})", last)

    ts         = ts_m.group(1) if ts_m else "unknown"
    verdict    = verdict_m.group(1) if verdict_m else "UNKNOWN"
    spread     = spread_m.group(1) if spread_m else "?"
    avg_fit    = avg_m.group(1) if avg_m else "?"
    per_period = {}
    if period_m:
        try:
            per_period = ast.literal_eval(period_m.group(1))
        except Exception:
            pass

    note = (
        "\nNOTE: This walk-forward was run with an earlier parameter configuration "
        "(before several optimization iterations). A fresh walk-forward with current "
        "params would give more accurate regime-by-regime numbers. Use --fresh-walkforward "
        "to re-run."
    )

    lines = [
        "WALK-FORWARD ROBUSTNESS (most recent run from change_log.txt)",
        "=" * 50,
        f"  Run timestamp  : {ts}",
        f"  Verdict        : {verdict}",
        f"  Spread (max-min): {spread}",
        f"  Avg fitness    : {avg_fit}",
        "",
        "  Per-period fitness:",
    ]
    for period_label, fitness in per_period.items():
        lines.append(f"    {period_label:<28}: {fitness:>8.4f}")
    lines.append(note)
    return "\n".join(lines)


def run_fresh_walk_forward():
    """Import walk_forward module and run all 4 periods. Returns formatted string."""
    try:
        import walk_forward as _wf
    except ImportError as e:
        return f"WALK-FORWARD: cannot import walk_forward module: {e}"

    print("  [WF] Running 4-period walk-forward (~12 minutes) ...", flush=True)

    # Back up and restore portfolio_report.json (each period overwrites it)
    report_bak = report_txt_bak = None
    if REPORT_JSON.exists():
        report_bak = REPORT_JSON.with_suffix(".json.wf_bak")
        shutil.copy2(REPORT_JSON, report_bak)
    if REPORT_TXT.exists():
        report_txt_bak = REPORT_TXT.with_suffix(".txt.wf_bak")
        shutil.copy2(REPORT_TXT, report_txt_bak)

    try:
        period_results = {p["label"]: _wf.run_period(p) for p in _wf.PERIODS}
    finally:
        if report_bak:
            shutil.copy2(report_bak, REPORT_JSON)
            report_bak.unlink()
        if report_txt_bak:
            shutil.copy2(report_txt_bak, REPORT_TXT)
            report_txt_bak.unlink()

    verdict_info = _wf.robustness_verdict(period_results)
    _wf.print_summary(period_results, verdict_info)

    lines = [
        "WALK-FORWARD ROBUSTNESS (FRESH -- just run with current params)",
        "=" * 50,
        f"  Verdict        : {verdict_info['verdict']}",
        f"  Spread (max-min): {verdict_info['spread']:.4f}",
        f"  Avg fitness    : {verdict_info['avg_fitness']:.4f}",
        "",
        f"  {'Period':<28} {'Type':<12} {'Return%':>9} {'Sharpe':>7} {'MaxDD%':>8} {'Fitness':>9}",
        "  " + "-" * 78,
    ]
    for lbl, r in period_results.items():
        if r is None:
            lines.append(f"  {lbl:<28} RUN FAILED")
        else:
            lines.append(
                f"  {lbl:<28} {r['type']:<12} {r['total_return_pct']:>+9.2f}"
                f" {r['sharpe']:>7.3f} {r['max_drawdown']:>+8.2f} {r['fitness']:>9.4f}"
            )
    return "\n".join(lines)


def gather_sampled_trades():
    """Return 5 most instructive trade narratives (3 premature + 2 correctly avoided)."""
    data, err = _safe_load_json(SAMPLED_JSON, "sampled_trades.json")
    if err:
        return err

    trades_with_60d = []
    for t in data.get("trades", []):
        m = re.search(r"60-day change from exit: ([+-][\d.]+)%", t.get("narrative", ""))
        if m:
            trades_with_60d.append((float(m.group(1)), t))

    trades_with_60d.sort(key=lambda x: x[0], reverse=True)

    # 3 biggest premature exits (sold too early, stock ran higher)
    premature = [t for chg, t in trades_with_60d if chg > 8.0][:3]
    # 2 biggest correctly-avoided losses (sold well, stock kept falling)
    good_exits = [t for chg, t in reversed(trades_with_60d) if chg < -5.0][:2]

    selected = premature + good_exits
    if not selected:
        return "SAMPLED TRADES: no instructive examples available (need sampled_trades.json with 60d data)"

    lines = [
        f"SAMPLED TRADE EXAMPLES ({len(selected)} most instructive from current sample)",
        f"  ({len(premature)} premature exits with large post-exit gains;"
        f" {len(good_exits)} correctly-timed exits where stock continued falling)",
        "=" * 60,
    ]
    for t in selected:
        lines.append("")
        lines.append(t.get("narrative", f"[no narrative for trade {t.get('trade_id')}]"))
        lines.append("-" * 60)
    return "\n".join(lines)


# ============================================================================
#  CONTEXT ASSEMBLY + TRIMMING
# ============================================================================

def estimate_tokens(text):
    return max(1, len(text) // 3)


def assemble_context(walk_forward_text, history_raw):
    """Return list of (section_name, section_text) pairs."""
    return [
        ("architecture",     _ARCHITECTURE),
        ("params",           gather_params()),
        ("history",          gather_history()[0]),  # text only
        ("portfolio",        gather_portfolio_metrics()),
        ("walk_forward",     walk_forward_text),
        ("sampled_trades",   gather_sampled_trades()),
    ]


def build_user_prompt(sections):
    """Concatenate sections into a single user message."""
    divider = "\n\n" + ("=" * 72) + "\n\n"
    return divider.join(text for _, text in sections)


def trim_sections(sections, history_raw):
    """
    Reduce context if over MAX_INPUT_TOKENS. Trim priority:
    1. Drop sampled_trades
    2. Truncate history to last 50 + all KEPT entries
    Never trim architecture, portfolio, or walk_forward.
    """
    total = sum(estimate_tokens(t) for _, t in sections)
    if total <= MAX_INPUT_TOKENS:
        return sections

    # Step 1: drop sampled trades
    trimmed = [(n, t) for n, t in sections if n != "sampled_trades"]
    trimmed.append(("sampled_trades",
                    "SAMPLED TRADES: omitted to stay within context budget."))
    total = sum(estimate_tokens(t) for _, t in trimmed)
    if total <= MAX_INPUT_TOKENS:
        print(f"  [ADVISOR] Trimmed sampled trades to fit context ({total:,} est. tokens)")
        return trimmed

    # Step 2: truncate history
    kept    = [e for e in history_raw if e.get("kept")]
    recent  = history_raw[-50:] if len(history_raw) > 50 else history_raw
    combined = {id(e): e for e in kept + recent}  # deduplicate preserving order
    ordered  = [e for e in history_raw if id(e) in combined]
    lines = [f"COMPLETE PARAMETER CHANGE HISTORY (truncated: {len(ordered)} of {len(history_raw)} entries shown)"]
    for e in ordered:
        outcome = "KEPT" if e.get("kept") else "REVERTED"
        lines.append(
            f"  [{e.get('ts','')}] {outcome}  {e.get('param_path','?')}:"
            f" {e.get('old_value')} -> {e.get('new_value')}"
            f"  delta={_delta_str(e)}"
        )
    trimmed2 = [(n, t) if n != "history" else ("history", "\n".join(lines))
                for n, t in trimmed]
    total2 = sum(estimate_tokens(t) for _, t in trimmed2)
    print(f"  [ADVISOR] Trimmed history to {len(ordered)} entries ({total2:,} est. tokens)")
    return trimmed2


# ============================================================================
#  SYSTEM PROMPT
# ============================================================================

_SYSTEM = """\
You are a veteran capital markets strategist and quantitative portfolio manager with 25 years \
of experience in systematic equity strategies, with deep expertise in momentum strategies, \
growth stock selection, regime-based allocation, and the practical realities of what separates \
strategies that survive live trading from those that only work in backtests.

You are reviewing a systematic momentum strategy that trades US tech and growth stocks using \
point-in-time fundamentals (gates G1-G8) for entry selection and technical rules for exits. \
The strategy has been through extensive parameter optimization and has hit a fitness ceiling. \
Your job is to look at the complete evidence and provide strategic direction that parameter \
tuning cannot.

Your analysis must cover:

1. STRATEGY HEALTH ASSESSMENT
   - What is genuinely working? Be specific: which mechanisms are earning their place, \
backed by the evidence provided.
   - What is structurally broken or missing? Not "which numbers are wrong" but "which logic \
is wrong or absent."
   - Is the fitness ceiling real (strategy is at its potential) or artificial (something \
structural is capping it)?

2. ROOT CAUSE ANALYSIS OF THE BIGGEST WEAKNESSES
   Look at the diagnostic signals, sector breakdown, exit distribution, and walk-forward \
regime results. For each major weakness, trace it to a root cause:
   - Is the 2022 bear underperformance a regime-detection speed problem, a position-sizing \
problem, or an entry-quality problem? Use the evidence.
   - The diagnostics consistently show ~97% of losing trades fall below entry within 15 days. \
What does this actually tell us about the entry logic, and what specific change would address it?
   - Exits tagged premature keep recovering after we sell. Is this a threshold problem \
(optimizer already says no -- every threshold change reverts) or does the exit need a different \
SIGNAL entirely?

3. STRUCTURAL IMPROVEMENT PROPOSALS (3-5, ranked by expected impact)
   For each proposal:
   - What to build/change, specifically enough that a developer could implement it
   - The evidence from the provided data that motivates it
   - Expected impact and the risk/downside
   - How to validate it (what test would prove it works before trusting it)
   Be creative but grounded -- propose things a professional PM would actually consider: new gate \
types, different exit signals (e.g. relative strength vs sector, volatility-adjusted stops, \
time-decay exits), regime-detection improvements, position sizing schemes (volatility targeting, \
Kelly-fraction approaches), sector rotation overlays, correlation-based portfolio constraints.

4. WHAT TO STOP DOING
   - Which parameters/mechanisms have been over-optimized and should be frozen?
   - Which ideas in the history clearly failed and should not be revisited?

5. THE ONE THING
   If you could only make one change to this strategy, what would it be and why?

Rules:
- Ground every claim in the specific evidence provided. Quote actual numbers.
- Distinguish clearly between "the data shows X" and "in my experience Y" -- both are valuable \
but label them.
- Be honest about uncertainty. If the evidence is ambiguous, say so.
- Do not propose parameter tuning -- that avenue is exhausted. Propose structural/logic \
changes only.
- Write for a strategy owner who is technical but not a finance professional. Explain financial \
concepts briefly when you use them.
- ASCII only, no unicode symbols."""


# ============================================================================
#  API CALL
# ============================================================================

def call_advisor(user_text):
    """Call claude-opus-4-8. Returns (response_text, in_tokens, out_tokens)."""
    est = estimate_tokens(user_text)
    print(f"  [ADVISOR] Estimated input tokens: ~{est:,}  (limit: {MAX_INPUT_TOKENS:,})")
    if est > MAX_INPUT_TOKENS:
        print("  [ADVISOR] WARNING: input may exceed context limit even after trimming")

    client = _anthropic.Anthropic()
    t0 = time.time()
    print("  [ADVISOR] Calling claude-opus-4-8 (max_tokens=8000) ...", flush=True)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        print(f"  [ADVISOR] API ERROR: {e}")
        raise

    elapsed  = time.time() - t0
    text     = resp.content[0].text if resp.content else ""
    in_tok   = resp.usage.input_tokens
    out_tok  = resp.usage.output_tokens
    cost     = (in_tok * COST_IN_PER_1M + out_tok * COST_OUT_PER_1M) / 1_000_000
    print(f"  [ADVISOR] Done in {elapsed:.0f}s  in={in_tok:,}  out={out_tok:,}  cost=${cost:.4f}")
    return text, in_tok, out_tok, cost


# ============================================================================
#  OUTPUT
# ============================================================================

def save_report(text, in_tok, out_tok, cost):
    """Write report to fixed path + timestamped copy."""
    header = (
        f"STRATEGY ADVISOR REPORT\n"
        f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Model     : {MODEL}\n"
        f"Tokens    : {in_tok:,} in / {out_tok:,} out\n"
        f"Cost      : ${cost:.4f}\n"
        f"{'=' * 72}\n\n"
    )
    full = header + text

    ADVISOR_REPORT.parent.mkdir(parents=True, exist_ok=True)
    ADVISOR_REPORT.write_text(full, encoding="ascii", errors="replace")
    print(f"  [ADVISOR] Saved -> {ADVISOR_REPORT}")

    ADVISOR_HIST.mkdir(parents=True, exist_ok=True)
    ts_tag   = datetime.now().strftime("%Y%m%d_%H%M")
    hist_dst = ADVISOR_HIST / f"advisor_{ts_tag}.txt"
    hist_dst.write_text(full, encoding="ascii", errors="replace")
    print(f"  [ADVISOR] Archived -> {hist_dst}")

    return full


# ============================================================================
#  MAIN
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Run a Capital Markets Expert strategic review of the strategy"
    )
    ap.add_argument("--output-only",      action="store_true",
                    help="Print the most recent advisor report without re-running")
    ap.add_argument("--fresh-walkforward", action="store_true",
                    help="Run walk_forward.py first to get current regime numbers (~12 min extra)")
    args = ap.parse_args()

    # --output-only: just print last report
    if args.output_only:
        if not ADVISOR_REPORT.exists():
            print(f"ERROR: no report found at {ADVISOR_REPORT}")
            print("Run without --output-only to generate one.")
            sys.exit(1)
        print(ADVISOR_REPORT.read_text(encoding="utf-8", errors="replace"))
        return

    if not _ANTHROPIC_OK:
        print("ERROR: anthropic package not installed (pip install anthropic)")
        sys.exit(1)

    print(f"\n{'=' * 72}")
    print(f"  STRATEGY ADVISOR  --  {MODEL}")
    print(f"{'=' * 72}\n")

    # Walk-forward: fresh run or parse from log
    if args.fresh_walkforward:
        walk_forward_text = run_fresh_walk_forward()
    else:
        walk_forward_text = gather_walk_forward_from_log()

    # Load raw history for potential trimming
    hist_data, hist_err = _safe_load_json(HISTORY_JSON, "params_history.json")
    history_raw = hist_data if hist_data else []

    # Assemble context sections
    sections = assemble_context(walk_forward_text, history_raw)

    # Trim if over token budget
    sections = trim_sections(sections, history_raw)

    # Build user prompt
    user_text = build_user_prompt(sections)

    # Confirm before sending (this is an expensive call)
    est = estimate_tokens(user_text)
    est_cost_low  = (est * COST_IN_PER_1M + 2000 * COST_OUT_PER_1M) / 1_000_000
    est_cost_high = (est * COST_IN_PER_1M + MAX_TOKENS * COST_OUT_PER_1M) / 1_000_000
    print(f"  Context assembled: ~{est:,} estimated input tokens")
    print(f"  Estimated cost: ${est_cost_low:.2f} - ${est_cost_high:.2f}")
    print()

    # API call
    response_text, in_tok, out_tok, cost = call_advisor(user_text)

    # Force ASCII for console output
    safe_response = response_text.encode("ascii", errors="replace").decode("ascii")
    print(f"\n{'=' * 72}")
    print("  ADVISOR REPORT")
    print(f"{'=' * 72}\n")
    print(safe_response)

    # Save
    save_report(response_text, in_tok, out_tok, cost)

    print(f"\n{'=' * 72}")
    print(f"  Tokens in/out : {in_tok:,} / {out_tok:,}")
    print(f"  Total cost    : ${cost:.4f}")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
