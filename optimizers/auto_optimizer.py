#!/usr/bin/env python3
"""
auto_optimizer.py  --  3-agent improvement loop for the portfolio backtest system
==================================================================================
Agents may edit:
  engine/tester.py                  (gate logic, thresholds, confidence scoring)
  engine/portfolio_simulator.py     (CONFIG block ONLY -- between CONFIG/END CONFIG)

Fitness = total_return_pct * 0.5 + (sharpe * 20) * 0.5
  (* 20 puts Sharpe on a comparable scale to a return percentage.
     A Sharpe of 1.0 -> 20 pts, so the two terms contribute roughly equally
     when return is ~20% and Sharpe is ~1.0. Tune the constant if one term
     dominates -- explain in a comment.)

Guardrails (hard rollback -- trip any one -> revert, log reason):
  1. DRAWDOWN CEILING   : max_drawdown >= -45% (current 2020-2024 bear-inclusive baseline -40.84%)
  2. STAY_INVESTED FLOOR: n_closed >= 60% of baseline AND avg_cash_pct <= 40%
  3. SANITY             : n_closed > 0, total_return_pct is a real number

Usage:
  python auto_optimizer.py --baseline-only      # verify harness, no edits
  python auto_optimizer.py --once               # one full iteration
  python auto_optimizer.py --iterations 5 --budget 5.00
  python auto_optimizer.py --iterations 10 --no-gpt --budget 3.00
"""

import sys, json, os, shutil, subprocess, time, re, math, argparse, textwrap
from datetime import datetime
from pathlib import Path

# ============================================================================
#  OPTIONAL API IMPORTS
# ============================================================================
try:
    import anthropic as _anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False
    _anthropic = None

try:
    from openai import OpenAI as _OpenAI
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False
    _OpenAI = None

# ============================================================================
#  PATHS
# ============================================================================
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR    = PROJECT_ROOT / "engine" / "portfolio_simulator.py"
SIM_START_OVERRIDE = "2020-01-01"
SIM_END_OVERRIDE   = "2024-12-31"
TESTER_MAIN  = PROJECT_ROOT / "engine" / "tester.py"
REPORT_JSON  = PROJECT_ROOT / "reports" / "portfolio_report.json"
REPORT_TXT   = PROJECT_ROOT / "reports" / "portfolio_report.txt"
CHANGE_LOG   = PROJECT_ROOT / "logs"    / "change_log.txt"
FEATURE_CACHE_DB = PROJECT_ROOT / "data" / "feature_cache.db"
OPP_REPORT   = PROJECT_ROOT / "logs"    / "opportunity_report.txt"
PARAMS_HIST  = PROJECT_ROOT / "logs"    / "params_history.json"

# ============================================================================
#  MODELS AND APPROXIMATE COSTS  (USD per 1M tokens)
# ============================================================================
MODEL_ANALYST     = "claude-haiku-4-5-20251001"
MODEL_CRITIC_GPT  = "gpt-4o-mini"
MODEL_SYNTHESIZER = "claude-opus-4-8"

_COST_PER_1M = {
    "claude-haiku-4-5-20251001": {"in": 0.80,  "out": 4.00},
    "claude-opus-4-8":           {"in": 15.00, "out": 75.00},
    "gpt-4o-mini":               {"in": 0.15,  "out": 0.60},
}

_session_cost    = 0.0
_session_tokens  = {"in": 0, "out": 0}
_session_verdicts = {"APPROVE": 0, "REVISE": 0, "REJECT": 0}

_walk_forward_cache  = None   # set by run_walk_forward_check(); dict or None if never run
_kept_since_wf_check = 0      # count of fully-kept changes since the last walk-forward check


def _tally_cost(model, in_tok, out_tok):
    global _session_cost, _session_tokens
    rates = _COST_PER_1M.get(model, {"in": 5.0, "out": 20.0})
    cost  = (in_tok * rates["in"] + out_tok * rates["out"]) / 1_000_000.0
    _session_cost          += cost
    _session_tokens["in"]  += in_tok
    _session_tokens["out"] += out_tok
    print(f"  [API] {model}  in={in_tok}  out={out_tok}  "
          f"cost=${cost:.4f}  session=${_session_cost:.4f}")
    return cost


# ============================================================================
#  FITNESS FUNCTION
# ============================================================================
# fitness = total_return_pct * 0.5 + (sharpe * 20) * 0.5
# The constant 20 was chosen so Sharpe=1.0 -> 20 fitness pts, which is
# comparable to a 20% return contribution.  Both terms carry equal weight.
# If one term starts dominating (e.g. sharpe*20 >> return), halve the
# constant and explain the change here.

def compute_fitness(summary):
    ret    = float(summary.get("total_return_pct") or 0.0)
    sharpe = float(summary.get("sharpe")           or 0.0)
    if math.isnan(ret):    ret    = 0.0
    if math.isnan(sharpe): sharpe = 0.0
    return round(ret * 0.5 + (sharpe * 20.0) * 0.5, 4)


# ============================================================================
#  GUARDRAILS
# ============================================================================
DRAWDOWN_CEILING  = -45.0   # max_drawdown must stay >= this (less negative)
STAY_INVESTED_MIN = 0.60    # n_closed must stay >= 60% of baseline
CASH_IDLE_MAX     = 0.40    # avg_cash_pct must not exceed 40%

# Walk-forward robustness gate (second-stage check, run only every Nth kept
# change since walk_forward.py runs 4x the simulations of a normal iteration).
WALK_FORWARD_CHECK_EVERY       = 3     # run the full walk-forward suite every Nth kept change
WALK_FORWARD_SPREAD_TOLERANCE  = 5.0   # allowed increase in walk-forward spread before rollback


def check_guardrails(summary, baseline_n_closed):
    """
    Returns (passed: bool, reason: str).
    Checks SANITY -> DRAWDOWN -> STAY_INVESTED in order.
    """
    n_closed = int(summary.get("n_closed") or 0)
    ret      = summary.get("total_return_pct")
    mdd      = float(summary.get("max_drawdown") or 0.0)

    # 1. Sanity
    if n_closed == 0:
        return False, "SANITY: n_closed=0 (simulator produced no trades)"
    if ret is None or (isinstance(ret, float) and math.isnan(ret)):
        return False, "SANITY: total_return_pct is NaN/None"

    # 2. Drawdown ceiling
    if mdd < DRAWDOWN_CEILING:
        return False, (f"DRAWDOWN_CEILING: max_drawdown={mdd:.2f}% < "
                       f"floor {DRAWDOWN_CEILING:.0f}%")

    # 3. Stay-invested: trade count
    if baseline_n_closed and n_closed < STAY_INVESTED_MIN * baseline_n_closed:
        limit = int(STAY_INVESTED_MIN * baseline_n_closed)
        return False, (f"STAY_INVESTED_FLOOR: n_closed={n_closed} < "
                       f"{STAY_INVESTED_MIN*100:.0f}% of baseline {baseline_n_closed} "
                       f"(min={limit})")

    # 4. Stay-invested: average cash idle (only if field present in JSON)
    avg_cash = summary.get("avg_cash_pct")
    if avg_cash is not None:
        try:
            if float(avg_cash) > CASH_IDLE_MAX:
                return False, (f"CASH_IDLE: avg_cash_pct={avg_cash:.1%} > "
                               f"{CASH_IDLE_MAX*100:.0f}% limit")
        except (TypeError, ValueError):
            pass

    return True, "OK"


# ============================================================================
#  REPORT LOADING AND FORMATTING
# ============================================================================

def load_report():
    """
    Load reports/portfolio_report.json.
    Returns (summary_dict, full_data_dict) or (None, None) on error.
    """
    try:
        with open(REPORT_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("summary", {}), data
    except Exception as exc:
        print(f"  [REPORT] Cannot load {REPORT_JSON}: {exc}")
        return None, None


def _fmt_report_for_agent(summary, full_data):
    """Build a structured text block for agent prompts (ASCII, ~80 cols)."""
    con    = full_data.get("concentration", {})
    secs   = full_data.get("sector_stats", {})
    closed = full_data.get("closed_trades", [])
    cfg    = full_data.get("config", {})

    lines = [
        "=" * 66,
        "  PORTFOLIO BACKTEST SUMMARY",
        "=" * 66,
        f"  Period       : {cfg.get('SIM_START','?')} -> {cfg.get('SIM_END','?')}",
        f"  Total Return : {summary.get('total_return_pct',0):+.2f}%"
        f"   SPY Return: {summary.get('spy_return',0):+.2f}%"
        f"   Alpha: {summary.get('alpha',0):+.2f}%",
        f"  CAGR         : {summary.get('cagr',0):+.2f}%",
        f"  Sharpe       : {summary.get('sharpe',0):.2f}"
        f"   SPY Sharpe: {summary.get('spy_sharpe','N/A')}",
        f"  Max Drawdown : {summary.get('max_drawdown',0):+.2f}%"
        f"   SPY MaxDD: {summary.get('spy_max_drawdown','N/A')}%",
        f"  Win Rate     : {summary.get('win_rate',0):.1f}%"
        f"  ({summary.get('n_closed',0)} closed / {summary.get('n_open',0)} open)",
        f"  Avg Win      : {summary.get('avg_win',0):+.2f}%"
        f"   Avg Loss: {summary.get('avg_loss',0):+.2f}%",
        f"  Profit Factor: {summary.get('profit_factor',0):.2f}x",
        f"  Avg Cash Idle: {summary.get('avg_cash_pct',0):.1%}",
        "",
        "CONCENTRATION (gross P&L share by ticker):",
        f"  Top-1  ({con.get('top1_name',''):<6}): {con.get('top1_pct',0):.1f}%",
        f"  Top-3  ({','.join(con.get('top3_names',[]))}): {con.get('top3_pct',0):.1f}%",
        f"  Top-5  ({','.join(con.get('top5_names',[]))}): {con.get('top5_pct',0):.1f}%",
        f"  Excl top-2 profit factor: {con.get('excl_top2_pf','N/A')}x"
        f"  ({con.get('excl_top2_trades',0)} trades)",
    ]

    # Sector breakdown sorted by P&L
    lines += ["", "SECTOR BREAKDOWN (closed trades):"]
    lines.append(f"  {'Sector':<32} {'N':>4} {'WR%':>6} {'AvgRet%':>8} {'P&L $':>10}")
    lines.append("  " + "-" * 62)
    for sec, st in sorted(secs.items(), key=lambda x: -x[1].get("pnl_dollars", 0)):
        n   = st.get("n", 0)
        wr  = st["wins"] / n * 100 if n else 0
        avg = st.get("avg_ret_pct", 0)
        pnl = st.get("pnl_dollars", 0)
        lines.append(f"  {sec[:32]:<32} {n:>4} {wr:>6.1f} {avg:>+8.2f} {pnl:>+10,.0f}")

    if closed:
        # Exit-reason distribution
        exit_counts = {}
        for t in closed:
            r = t.get("exit_reason", "?").split("(")[0].strip()
            exit_counts[r] = exit_counts.get(r, 0) + 1
        lines += ["", "EXIT REASON BREAKDOWN:"]
        for r, cnt in sorted(exit_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {r:<35} {cnt:>4} trades")

        # Worst 10 by P&L dollars
        worst = sorted(closed, key=lambda t: t.get("pnl_dollars", 0))[:10]
        lines += ["", "TOP-10 LOSERS (by P&L $):"]
        for t in worst:
            lines.append(
                f"  {t['ticker']:<7} {t['sector'][:24]:<24} "
                f"ret={t['pnl_pct']:>+7.2f}% "
                f"pnl=${t['pnl_dollars']:>+9,.0f} "
                f"exit={t['exit_reason'].split('(')[0].strip()[:22]}")

        # Best 10 by P&L dollars
        best = sorted(closed, key=lambda t: t.get("pnl_dollars", 0), reverse=True)[:10]
        lines += ["", "TOP-10 WINNERS (by P&L $):"]
        for t in best:
            lines.append(
                f"  {t['ticker']:<7} {t['sector'][:24]:<24} "
                f"ret={t['pnl_pct']:>+7.2f}% "
                f"pnl=${t['pnl_dollars']:>+9,.0f} "
                f"exit={t['exit_reason'].split('(')[0].strip()[:22]}")

    # Diagnostic signals (precomputed by portfolio_simulator.py)
    diag = full_data.get("diagnostics", {})
    if diag:
        s1  = diag.get("signal1_bad_entries",     {})
        s2  = diag.get("signal2_premature_exits", {})
        s3  = diag.get("signal3_exit_quality",    {})
        lines += ["", "DIAGNOSTIC SIGNALS (precomputed -- act on these directly):"]
        lines.append("  " + "-" * 62)

        # Signal 1 -- bad entries -> tighten gates in tester.py
        n_bad  = s1.get("n_bad_entries",    0)
        n_loss = s1.get("n_total_losers",   0)
        bad_pl = s1.get("total_pnl_dollars", 0.0)
        n_gd   = s1.get("n_with_gate_data", 0)
        lines.append("  SIGNAL 1 -- BAD ENTRIES  (lever: tighten gates in tester.py)")
        lines.append(f"    {n_bad} of {n_loss} losing trades fell below entry price within 15d")
        lines.append(f"    Total P&L from bad entries: ${bad_pl:+,.0f}")
        gates = s1.get("top_barely_gates", [])
        if gates and n_gd > 0:
            lines.append(f"    Top barely-passed gates ({n_gd} bad-entry trades with gate data):")
            for rec in gates[:3]:
                lines.append(
                    f"      {rec.get('gate','?')[:28]:<28}"
                    f" {rec.get('count',0):>3}/{n_gd}"
                    f"  ({rec.get('pct_of_bad',0):.1f}%)")
        if s1.get("hint"):
            lines.append(f"    {s1['hint']}")

        # Signal 2 -- premature exits -> loosen exit in CONFIG
        n_prem = s2.get("n_premature",         0)
        n_cl2  = s2.get("n_total_closed",       0)
        missed = s2.get("total_dollars_missed", 0.0)
        lines.append("  SIGNAL 2 -- PREMATURE EXITS  (lever: loosen exit in CONFIG)")
        lines.append(f"    {n_prem} of {n_cl2} exits left ${missed:,.0f} on the table"
                     f"  (20d lookahead / >8% threshold)")
        for rkey, rdata in list(s2.get("by_exit_reason", {}).items())[:3]:
            lines.append(
                f"    {rkey[:24]:<24}"
                f" {rdata.get('count',0):>4} exits"
                f"  ${rdata.get('dollars_missed',0):>+9,.0f} missed")
        if s2.get("hint"):
            lines.append(f"    {s2['hint']}")

        # Signal 3 -- exit quality summary
        lines.append("  SIGNAL 3 -- EXIT QUALITY")
        lines.append(
            f"    Good: {s3.get('pct_good',0):.1f}%"
            f"  Premature: {s3.get('pct_premature',0):.1f}%"
            f"  Rode-down: {s3.get('pct_rode_down',0):.1f}%"
            f"  ({s3.get('total_closed',0)} closed)")
        lines.append("  " + "-" * 62)

    lines.append("=" * 66)
    return "\n".join(lines)


def _read_config_block():
    """Extract the CONFIG block from portfolio_simulator.py."""
    try:
        with open(SIMULATOR, "r", encoding="ascii", errors="replace") as f:
            text = f.read()
        m = re.search(r"(#\s*={10,}\s*\n#\s*CONFIG.*?#\s*END CONFIG[^\n]*\n)", text, re.DOTALL)
        return m.group(1) if m else "(CONFIG block not found -- check SIMULATOR path)"
    except Exception as exc:
        return f"(cannot read simulator: {exc})"


def _read_change_log_tail(n_lines=80):
    """Read last n_lines of change_log.txt (ASCII, errors replaced)."""
    try:
        CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not CHANGE_LOG.exists():
            return "(change_log.txt not yet created)"
        with open(CHANGE_LOG, "r", encoding="ascii", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n_lines:] if len(lines) > n_lines else lines
        return "".join(tail)
    except Exception as exc:
        return f"(cannot read change_log: {exc})"


# ============================================================================
#  SIMULATION RUNNER
# ============================================================================

def run_simulator(timeout=1800):
    """
    Execute portfolio_simulator.py as subprocess.
    Returns (success: bool, stderr_or_error: str).
    """
    print("  [SIM] Running portfolio_simulator.py ...", flush=True)
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(SIMULATOR), "--start", SIM_START_OVERRIDE, "--end", SIM_END_OVERRIDE],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            encoding="ascii", errors="replace",
        )
        elapsed = time.time() - t0
        print(f"  [SIM] Done in {elapsed:.0f}s  exit={result.returncode}", flush=True)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-600:]
            print(f"  [SIM] ERROR tail:\n{tail}", flush=True)
            return False, tail
        return True, ""
    except subprocess.TimeoutExpired:
        print("  [SIM] TIMEOUT", flush=True)
        return False, "TIMEOUT"
    except Exception as exc:
        print(f"  [SIM] EXCEPTION: {exc}", flush=True)
        return False, str(exc)


# ============================================================================
#  BACKUP / RESTORE
# ============================================================================

def make_backup(filepath, tag=""):
    """
    Copy filepath -> backups/<stem>_<timestamp><tag><ext>.
    Returns Path to backup or None on failure.
    """
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    p  = Path(filepath)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    dst = BACKUPS_DIR / f"{p.stem}_{ts}{sfx}{p.suffix}"
    try:
        shutil.copy2(str(p), str(dst))
        print(f"  [BACKUP] {p.name} -> backups/{dst.name}")
        return dst
    except Exception as exc:
        print(f"  [BACKUP] FAILED for {p.name}: {exc}")
        return None


def restore_backup(original, backup):
    """Restore original from backup. Returns True on success."""
    try:
        shutil.copy2(str(backup), str(original))
        print(f"  [RESTORE] {Path(original).name} <- {Path(backup).name}")
        return True
    except Exception as exc:
        print(f"  [RESTORE] FAILED: {exc}")
        return False


def _sync_tester_copy():
    """No-op: tester.py no longer has a secondary copy (multi_sector/ removed)."""
    pass


# ============================================================================
#  CHANGE LOG
# ============================================================================

def _append_log(entry):
    """Append an entry to change_log.txt (ASCII, newline-padded)."""
    CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = f"\n[{ts}] AUTO_OPTIMIZER: {entry}\n"
    try:
        with open(CHANGE_LOG, "a", encoding="ascii", errors="replace") as f:
            f.write(text)
    except Exception as exc:
        print(f"  [LOG] Write failed: {exc}")


def log_rollback(reason, target_file, fit_before, fit_after=None, patches=None):
    after        = f"  fitness_after={fit_after:.4f}" if fit_after is not None else ""
    change_lines = _summarize_patches(patches) if patches else []
    changes_block = ""
    if change_lines:
        inner = "\n        ".join(change_lines)
        changes_block = f"\n    CHANGES (REVERTED):\n        {inner}"
    _append_log(f"ROLLBACK  file={target_file}  "
                f"fitness_before={fit_before:.4f}{after}  reason={reason}"
                f"{changes_block}")
    print(f"  [ROLLBACK] {reason}")


def log_kept(target_file, fit_before, fit_after, evidence_short, patches=None):
    change_lines = _summarize_patches(patches) if patches else []
    changes_block = ""
    if change_lines:
        inner = "\n        ".join(change_lines)
        changes_block = f"\n    CHANGES:\n        {inner}"
    _append_log(f"KEPT  file={target_file}  "
                f"fitness {fit_before:.4f} -> {fit_after:.4f}  "
                f"delta={fit_after-fit_before:+.4f}  "
                f"evidence={evidence_short[:120]}"
                f"{changes_block}")
    print(f"  [KEPT] fitness {fit_before:.4f} -> {fit_after:.4f}"
          f"  delta={fit_after-fit_before:+.4f}")


# ============================================================================
#  WALK-FORWARD ROBUSTNESS GATE
# ============================================================================

def run_walk_forward_check(iteration_num):
    """
    Run the full walk_forward.py suite (multiple historical periods) and cache
    the result in _walk_forward_cache. Expensive -- callers must gate this to
    every Nth kept change (see WALK_FORWARD_CHECK_EVERY).

    walk_forward.run_period() overwrites reports/portfolio_report.json for each
    period it tests, so the current single-period (2020-2024) report is backed
    up beforehand and restored afterward -- otherwise the main iteration loop
    would read back the wrong period's report on its next pass.

    Returns the verdict dict from walk_forward.robustness_verdict().
    """
    import walk_forward as _wf
    global _walk_forward_cache

    report_backup_json = report_backup_txt = None
    if REPORT_JSON.exists():
        report_backup_json = REPORT_JSON.with_suffix(".json.wf_bak")
        shutil.copy2(REPORT_JSON, report_backup_json)
    if REPORT_TXT.exists():
        report_backup_txt = REPORT_TXT.with_suffix(".txt.wf_bak")
        shutil.copy2(REPORT_TXT, report_backup_txt)

    print(f"\n  [WALK-FORWARD] Running full {len(_wf.PERIODS)}-period suite "
          f"(iteration {iteration_num}) ...", flush=True)
    try:
        period_results = {p["label"]: _wf.run_period(p) for p in _wf.PERIODS}
    finally:
        if report_backup_json is not None:
            shutil.copy2(report_backup_json, REPORT_JSON)
            report_backup_json.unlink()
        if report_backup_txt is not None:
            shutil.copy2(report_backup_txt, REPORT_TXT)
            report_backup_txt.unlink()

    verdict_info = _wf.robustness_verdict(period_results)
    verdict_info["iteration"] = iteration_num
    _walk_forward_cache = verdict_info

    print(f"  [WALK-FORWARD] verdict={verdict_info['verdict']}  "
          f"spread={verdict_info['spread']:.2f}  avg_fitness={verdict_info['avg_fitness']:.2f}")
    _append_log(
        f"WALK_FORWARD_CHECK  iteration={iteration_num}  "
        f"verdict={verdict_info['verdict']}  spread={verdict_info['spread']:.4f}  "
        f"avg_fitness={verdict_info['avg_fitness']:.4f}  "
        f"per_period={verdict_info['per_period']}"
    )
    return verdict_info


def _fmt_walk_forward_block():
    """Short ASCII summary of the last cached walk-forward result, for agent prompts."""
    if _walk_forward_cache is None:
        return ""
    wf = _walk_forward_cache
    per_period_str = "  ".join(f"{lbl}={fit:.1f}" for lbl, fit in wf["per_period"].items())
    return (
        f"\nWALK-FORWARD ROBUSTNESS (last checked at iteration {wf['iteration']}):\n"
        f"  Verdict: {wf['verdict']}\n"
        f"  Per-period fitness: {per_period_str}\n"
        f"  Spread: {wf['spread']:.1f} points\n"
    )


# ============================================================================
#  PATCH EXTRACTION AND APPLICATION
# ============================================================================
# Agent 3 outputs changes in this format (may repeat multiple blocks):
#   <<<PATCH_START>>>
#   <<<OLD>>>
#   ...exact original lines...
#   <<<NEW>>>
#   ...replacement lines...
#   <<<PATCH_END>>>

_PS = "<<<PATCH_START>>>"
_PO = "<<<OLD>>>"
_PN = "<<<NEW>>>"
_PE = "<<<PATCH_END>>>"


def extract_patches(text):
    """
    Parse all PATCH blocks from Agent 3's response.
    Returns list of (old_text, new_text) tuples.
    """
    patches = []
    pos = 0
    while True:
        i_start = text.find(_PS, pos)
        if i_start < 0:
            break
        i_old = text.find(_PO, i_start)
        i_new = text.find(_PN, i_old if i_old > 0 else i_start)
        i_end = text.find(_PE, i_new if i_new > 0 else i_start)
        if i_old < 0 or i_new < 0 or i_end < 0:
            break
        old_text = text[i_old + len(_PO): i_new].strip("\n")
        new_text = text[i_new + len(_PN): i_end].strip("\n")
        patches.append((old_text, new_text))
        pos = i_end + len(_PE)
    return patches


def apply_patches(filepath, patches):
    """
    Apply (old, new) patches to a file in order.
    Returns (ok: bool, n_applied: int, error_msg: str).
    """
    try:
        with open(filepath, "r", encoding="ascii", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        return False, 0, f"Cannot read {filepath}: {exc}"

    applied = 0
    for old, new in patches:
        if old not in content:
            snippet = old[:120].replace("\n", "\\n")
            return False, applied, f"Patch old-text not found after {applied} applied:\n  {snippet}"
        content = content.replace(old, new, 1)
        applied += 1

    try:
        with open(filepath, "w", encoding="ascii", errors="replace") as f:
            f.write(content)
    except Exception as exc:
        return False, applied, f"Cannot write {filepath}: {exc}"

    return True, applied, "OK"


def _count_changed_lines(patches):
    """Count total lines touched across all patches."""
    n = 0
    for old, new in patches:
        n += max(len(old.splitlines()), len(new.splitlines()))
    return n


def _summarize_patches(patches):
    """
    Produce a list of human-readable change strings from a patch list.
    For each (old_text, new_text) pair, compare non-empty lines pairwise.
    If both sides look like   NAME = value   lines, emit "NAME: old_val -> new_val".
    Otherwise emit trimmed verbatim text (truncated to 120 chars per side).
    Returns a list of strings, one per changed line detected.
    """
    import re as _re
    # Matches:  IDENTIFIER  =  value  (optional trailing comment)
    _VAR_RE = _re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*(?:#.*)?$'
    )
    summary_lines = []
    for old_text, new_text in patches:
        old_lines = [l for l in old_text.splitlines() if l.strip()]
        new_lines = [l for l in new_text.splitlines() if l.strip()]
        max_len   = max(len(old_lines), len(new_lines))
        for i in range(max_len):
            o = old_lines[i].rstrip() if i < len(old_lines) else ""
            n = new_lines[i].rstrip() if i < len(new_lines) else ""
            if o == n:
                continue   # identical -- skip
            m_o = _VAR_RE.match(o)
            m_n = _VAR_RE.match(n)
            if m_o and m_n and m_o.group(1) == m_n.group(1):
                # Clean var = value pattern on both sides
                name    = m_o.group(1)
                old_val = m_o.group(2).split("#")[0].strip()
                new_val = m_n.group(2).split("#")[0].strip()
                summary_lines.append(f"{name}: {old_val} -> {new_val}")
            else:
                # Fall back to verbatim (truncated)
                o_short = o.strip()[:120] if o else "(empty)"
                n_short = n.strip()[:120] if n else "(empty)"
                summary_lines.append(f"OLD: {o_short}")
                summary_lines.append(f"NEW: {n_short}")
    return summary_lines if summary_lines else ["(no line-level diff extracted)"]


# ============================================================================
#  API WRAPPERS
# ============================================================================

def _call_anthropic(model, system_text, user_text, max_tokens=4096):
    """Call Anthropic API. Returns (response_text, in_tokens, out_tokens)."""
    if not _ANTHROPIC_OK:
        raise RuntimeError("anthropic package not installed  (pip install anthropic)")
    client = _anthropic.Anthropic()
    resp   = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_text,
        messages=[{"role": "user", "content": user_text}],
    )
    text    = resp.content[0].text if resp.content else ""
    in_tok  = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    _tally_cost(model, in_tok, out_tok)
    return text, in_tok, out_tok


def _call_openai(model, system_text, user_text, max_tokens=1024):
    """Call OpenAI API. Returns (response_text, in_tokens, out_tokens)."""
    if not _OPENAI_OK:
        raise RuntimeError("openai package not installed  (pip install openai)")
    client = _OpenAI()
    resp   = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user",   "content": user_text},
        ],
    )
    text    = resp.choices[0].message.content or ""
    in_tok  = resp.usage.prompt_tokens
    out_tok = resp.usage.completion_tokens
    _tally_cost(model, in_tok, out_tok)
    return text, in_tok, out_tok


# ============================================================================
#  OPPORTUNITY-CACHE HELPERS  (B1/B2 support)
# ============================================================================

import sqlite3 as _sqlite3


def _load_oos_sample(n_executed=15, n_gate_rejected=10, n_dodged=5):
    """
    Draw the Agent 1 sample from feature_cache.db and portfolio_report.json:
      - n_executed    : executed trades from portfolio_report.json (stratified)
      - n_gate_rejected: GATE_REJECTED missed winners from cache
      - n_dodged      : dodged losers (correctly rejected) from cache

    Returns a dict with keys 'executed', 'gate_rejected', 'dodged'.
    Each entry is a list of dicts with ticker, date, fwd_ret_3m, gate info, price narrative.
    Returns {} with a warning if the cache does not exist.
    """
    result = {"executed": [], "gate_rejected": [], "dodged": [], "cache_available": False}

    # --- Executed trades from portfolio_report.json ---
    try:
        with open(REPORT_JSON, "r", encoding="utf-8") as f:
            rdata = json.load(f)
        closed = rdata.get("closed_trades", [])
        if closed:
            # Stratify by outcome: mix winners and losers
            winners = sorted([t for t in closed if t.get("pnl_pct", 0) > 0],
                             key=lambda x: -abs(x.get("pnl_dollars", 0)))
            losers  = sorted([t for t in closed if t.get("pnl_pct", 0) <= 0],
                             key=lambda x: abs(x.get("pnl_dollars", 0)), reverse=True)
            # Mix: ~60% losers, ~40% winners (focus on what to fix)
            n_l = min(int(n_executed * 0.6), len(losers))
            n_w = min(n_executed - n_l, len(winners))
            sampled = losers[:n_l] + winners[:n_w]
            result["executed"] = [{
                "ticker":       t.get("ticker"),
                "sector":       t.get("sector"),
                "entry_date":   t.get("entry_date"),
                "exit_date":    t.get("exit_date"),
                "pnl_pct":      t.get("pnl_pct"),
                "pnl_dollars":  t.get("pnl_dollars"),
                "exit_reason":  t.get("exit_reason"),
                "score":        t.get("score"),
                "conviction":   t.get("conviction"),
                "gate_margins": t.get("gate_margins", {}),
                "type":         "EXECUTED_TRADE",
            } for t in sampled]
    except Exception as e:
        result["executed_warn"] = f"Could not load portfolio_report.json: {e}"

    # --- GATE_REJECTED missed winners + dodged losers from cache ---
    if not FEATURE_CACHE_DB.exists():
        result["cache_warn"] = (
            "feature_cache.db not found -- run data_pipeline/build_feature_cache.py first. "
            "Agent 1 will operate on executed trades only."
        )
        return result

    try:
        conn = _sqlite3.connect(str(FEATURE_CACHE_DB))

        # GATE_REJECTED missed winners: fwd_ret_3m > 30%, tradeable, gate_passed=0
        cur = conn.execute(
            """
            SELECT date, ticker, sharadar_industry, fwd_ret_3m, fwd_ret_1m,
                   gate_score, gate_threshold, gate_failed_name, gate_failed_margin,
                   gm_pct, ps_ratio, rule40, revenue_growth, regime
            FROM feature_cache
            WHERE tradeable_flag = 1
              AND gate_passed = 0
              AND gate_failed_name IS NOT NULL
              AND fwd_ret_3m > 0.30
            ORDER BY fwd_ret_3m DESC
            LIMIT ?
            """,
            (n_gate_rejected * 3,)  # over-fetch, then subsample
        )
        gr_rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        gr_records = [dict(zip(cols, r)) for r in gr_rows]
        # Subsample to n_gate_rejected, spread across magnitude
        step = max(1, len(gr_records) // n_gate_rejected)
        result["gate_rejected"] = [
            {**r, "type": "GATE_REJECTED_MISSED_WINNER",
             "fwd_ret_3m_pct": round(r["fwd_ret_3m"] * 100, 2)}
            for r in gr_records[::step][:n_gate_rejected]
        ]

        # Dodged losers: fwd_ret_3m < -25%, gate_passed=0 (correctly rejected)
        cur2 = conn.execute(
            """
            SELECT date, ticker, sharadar_industry, fwd_ret_3m, max_dd_3m,
                   gate_score, gate_threshold, gate_failed_name, gate_failed_margin,
                   gm_pct, ps_ratio, rule40, revenue_growth, regime
            FROM feature_cache
            WHERE tradeable_flag = 1
              AND gate_passed = 0
              AND gate_failed_name IS NOT NULL
              AND fwd_ret_3m < -0.25
            ORDER BY fwd_ret_3m ASC
            LIMIT ?
            """,
            (n_dodged * 3,)
        )
        d_rows = cur2.fetchall()
        cols2  = [d[0] for d in cur2.description]
        d_records = [dict(zip(cols2, r)) for r in d_rows]
        step2 = max(1, len(d_records) // n_dodged)
        result["dodged"] = [
            {**r, "type": "DODGED_LOSER",
             "fwd_ret_3m_pct": round(r["fwd_ret_3m"] * 100, 2)}
            for r in d_records[::step2][:n_dodged]
        ]

        result["cache_available"] = True
        conn.close()
    except Exception as e:
        result["cache_warn"] = f"Cache load error: {e}"

    return result


def _load_params_trend():
    """
    Read params_history.json and build a trend summary:
    For every parameter with >= 3 tested values, compute:
      - value -> delta series
      - explicit monotonicity read ("as X rose A->B->C, deltas improved -- trend positive until D crashed")

    Returns a formatted ASCII block for injection into agent prompts.
    Returns empty string if params_history.json not available.
    """
    try:
        if not PARAMS_HIST.exists():
            return ""
        with open(PARAMS_HIST, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return ""

    if not history:
        return ""

    # Group by parameter path
    by_param = {}
    for entry in history:
        path  = entry.get("param_path") or entry.get("param") or ""
        value = entry.get("value") or entry.get("new_value")
        delta = entry.get("fitness_delta") or entry.get("delta")
        if not path or value is None or delta is None:
            continue
        by_param.setdefault(path, []).append((float(value), float(delta)))

    if not by_param:
        return ""

    lines = ["PARAMETER TREND ANALYSIS (from params_history.json):"]
    n_with_trend = 0
    for param, pairs in sorted(by_param.items()):
        if len(pairs) < 3:
            continue
        pairs_sorted = sorted(pairs, key=lambda x: x[0])  # sort by value
        n_with_trend += 1
        value_str = "  ->  ".join(f"{v:.4g}" for v, _ in pairs_sorted)
        delta_str = "  ->  ".join(f"{d:+.4f}" for _, d in pairs_sorted)
        # Monotonicity read
        deltas = [d for _, d in pairs_sorted]
        values = [v for v, _ in pairs_sorted]
        monotone_pos = all(deltas[i] <= deltas[i+1] for i in range(len(deltas)-1))
        monotone_neg = all(deltas[i] >= deltas[i+1] for i in range(len(deltas)-1))
        peak_idx = deltas.index(max(deltas))
        if monotone_pos:
            mono_read = "trend POSITIVE (higher value -> better delta throughout)"
        elif monotone_neg:
            mono_read = "trend NEGATIVE (higher value -> worse delta throughout)"
        elif peak_idx > 0 and peak_idx < len(deltas) - 1:
            peak_val = values[peak_idx]
            mono_read = (f"peak at value={peak_val:.4g} (delta={deltas[peak_idx]:+.4f}); "
                         f"boundary mapped")
        else:
            mono_read = "non-monotone -- no clear trend"
        lines.append(f"  {param}:")
        lines.append(f"    values: {value_str}")
        lines.append(f"    deltas: {delta_str}")
        lines.append(f"    read  : {mono_read}")

    if n_with_trend == 0:
        return ""

    return "\n".join(lines)


def _load_opportunity_context(max_chars=2000):
    """
    Read the tail of logs/opportunity_report.txt for injection into agent prompts.
    Returns empty string if file not available.
    """
    try:
        if not OPP_REPORT.exists():
            return ""
        with open(OPP_REPORT, "r", encoding="ascii", errors="replace") as f:
            text = f.read()
        return text[-max_chars:] if len(text) > max_chars else text
    except Exception:
        return ""


def _fmt_oos_sample(sample):
    """
    Format the OOS sample dict into an ASCII block for Agent 1 prompt.
    """
    lines = []

    if sample.get("cache_warn"):
        lines.append(f"[OOS SAMPLE WARNING] {sample['cache_warn']}")

    lines.append("=" * 66)
    lines.append("  ANALYSIS SAMPLE (for Opportunity Analyst)")
    lines.append("=" * 66)

    # Executed trades
    executed = sample.get("executed", [])
    if executed:
        lines.append(f"\nEXECUTED TRADES ({len(executed)}):")
        for t in executed:
            pnl = t.get("pnl_pct", 0)
            lines.append(
                f"  {t.get('ticker','?'):<7} {t.get('entry_date','?')} -> {t.get('exit_date','?')} "
                f"PnL={pnl:+.1f}%  exit={t.get('exit_reason','?')[:25]}  "
                f"score={t.get('score','?')}  conviction={t.get('conviction','?')}"
            )
            gm = t.get("gate_margins", {})
            barely = [g for g, info in gm.items() if info.get("barely")]
            if barely:
                lines.append(f"    BARELY_PASSED: {', '.join(barely[:3])}")

    # GATE_REJECTED missed winners
    gr = sample.get("gate_rejected", [])
    if gr:
        lines.append(f"\nGATE_REJECTED MISSED WINNERS ({len(gr)}) -- actual gate findings:")
        for r in gr:
            lines.append(
                f"  {r.get('ticker','?'):<7} {r.get('date','?')}  "
                f"fwd3m={r.get('fwd_ret_3m_pct',0):+.1f}%  "
                f"industry={str(r.get('sharadar_industry',''))[:20]}  "
                f"regime={r.get('regime','')}  "
                f"failed_gate={r.get('gate_failed_name','?')}  "
                f"score={r.get('gate_score','?')}  thr={r.get('gate_threshold','?')}"
            )

    # Dodged losers
    dodged = sample.get("dodged", [])
    if dodged:
        lines.append(f"\nDODGED LOSERS ({len(dodged)}) -- defense record:")
        for r in dodged:
            lines.append(
                f"  {r.get('ticker','?'):<7} {r.get('date','?')}  "
                f"fwd3m={r.get('fwd_ret_3m_pct',0):+.1f}%  "
                f"maxdd={round((r.get('max_dd_3m') or 0)*100,1):+.1f}%  "
                f"failed_gate={r.get('gate_failed_name','?')}  "
                f"score={r.get('gate_score','?')}  thr={r.get('gate_threshold','?')}"
            )

    return "\n".join(lines)


# ============================================================================
#  AGENT 1: OPPORTUNITY ANALYST  (Haiku)
# ============================================================================

_ANALYST_SYSTEM = """\
You are a quantitative opportunity analyst for a portfolio backtest system.
Your job: analyze the provided sample of trades and output STRICT JSON.

ROUTING RULES (first output line, then JSON):
  TARGET_CONFIG  -> changes to stops, sizing, conviction multipliers, MA periods, hold days
  TARGET_TESTER  -> changes to gate thresholds, scoring logic, veto conditions,
                    regime-conditional sector bans, bear-regime momentum requirements

You receive three types of items:

1. EXECUTED_TRADE: a trade that ran through the system.
   -> Identify the best realistic ENTRY WINDOW and EXIT WINDOW (not single perfect points).
      e.g., 'entry was capturable 03-10->03-18 near $42-45'.
   -> Which gate delayed/blocked the better entry, or which exit rule fired early/late?
   -> Cite the exact parameter name, its current value, and compute:
      'changing {param} from {X} to {Y} would have captured approximately {Z}% more on this trade'

2. GATE_REJECTED_MISSED_WINNER: in tradeable universe, passed liquidity, rejected by gate.
   -> This is a gate finding. Name the gate, the margin below threshold, and:
      'threshold {gate_name} from {current_thr} to {admit_thr} would have admitted this stock
       which then returned {fwd_ret_3m_pct}%.'
   -> Fixed threshold OR relative-percentile threshold -- state which you recommend and why.

3. DODGED_LOSER: correctly rejected by a gate that would have caused a loss.
   -> Explicit credit: 'gate {gate_name} saved us from {fwd_ret_3m_pct}% by rejecting
      at score={gate_score} vs thr={gate_threshold}.'
   -> This credit MUST appear in parameter_signals so Agent 2 sees the cost of loosening.

OUTPUT FORMAT (strict JSON, ASCII only, no prose outside the JSON):

Line 1: TARGET_TESTER  (or TARGET_CONFIG) -- required
Line 2+: valid JSON object:
{
  "item_verdicts": [
    {
      "item_id": 1,
      "ticker": "PLTR",
      "type": "EXECUTED_TRADE",
      "entry_window": "2023-12-04 to 2023-12-08 near $18-19",
      "exit_window": "2024-01-15 to 2024-01-22 near $23-24",
      "gate_or_param": "exits.below_ma_trend_floor",
      "current_value": 0.085,
      "proposed_value": 0.10,
      "captured_additional_pct": 18.5,
      "reasoning": "exit fired 2023-12-29 at -3.2%; stock rebounded +50.4% next 60d"
    }
  ],
  "parameter_signals": [
    {
      "param": "exits.below_ma_trend_floor",
      "direction": "raise",
      "count": 3,
      "total_opportunity_pct": 42.1,
      "evidence_classes": {
        "captured_more": 2,
        "missed_winner": 1,
        "dodged_loser": 0
      },
      "tension": null
    }
  ]
}

CRITICAL RULES:
  - When missed_winner evidence and dodged_loser evidence point OPPOSITE directions on
    the same gate, set tension to the net $ opportunity string instead of picking a side.
    e.g., 'winners: +120% total, dodged: -85% saved -- net +35% loosening benefit'
  - Every dodged_loser MUST appear as a parameter_signals entry with direction=defend
    and evidence_classes.dodged_loser >= 1.
  - Quantify everything. 'approximately' + a number beats vague language.
  - ASCII only. No Unicode.
  - ONE parameter_signals entry per param (aggregate multiple items).
"""


def run_analyst(summary_text, config_block, change_log_tail):
    """
    Run Agent 1 (Opportunity Analyst, Haiku).
    Returns (target: str, full_response: str).
    target is 'TARGET_TESTER' or 'TARGET_CONFIG'.
    Now includes OOS sample from feature_cache + opportunity context.
    """
    print("\n  [AGENT 1 - Opportunity Analyst / Haiku]", flush=True)

    # Load OOS sample (graceful if cache not built yet)
    sample = _load_oos_sample(n_executed=15, n_gate_rejected=10, n_dodged=5)
    sample_block = _fmt_oos_sample(sample)

    # Load opportunity context (tail of opportunity_report.txt)
    opp_ctx = _load_opportunity_context(max_chars=1500)
    opp_block = (f"\nOPPORTUNITY REPORT SUMMARY (top gate findings):\n{opp_ctx}\n"
                 if opp_ctx else "")

    user = (
        f"{summary_text}\n\n"
        f"CURRENT CONFIG BLOCK (portfolio_simulator.py):\n"
        f"{config_block}\n\n"
        f"RECENT CHANGE LOG (do NOT repeat these):\n"
        f"{change_log_tail}\n"
        f"{_fmt_walk_forward_block()}\n"
        f"{sample_block}\n"
        f"{opp_block}\n"
        f"GUARDRAILS:\n"
        f"  max_drawdown must stay >= {DRAWDOWN_CEILING}%\n"
        f"  n_closed must stay >= {STAY_INVESTED_MIN*100:.0f}% of baseline\n"
        f"  avg_cash_pct must stay <= {CASH_IDLE_MAX*100:.0f}%\n\n"
        f"Analyze ALL items in the sample above. Output TARGET line then strict JSON."
    )

    text, _, _ = _call_anthropic(MODEL_ANALYST, _ANALYST_SYSTEM, user, max_tokens=4000)
    print(f"  [AGENT 1] Response (first 700 chars):\n"
          f"{textwrap.indent(text[:700], '    ')}", flush=True)

    # Extract TARGET sentinel
    target = None
    for line in text.splitlines():
        s = line.strip()
        if s in ("TARGET_TESTER", "TARGET_CONFIG"):
            target = s
            break
    if target is None:
        if "TARGET_TESTER" in text:
            target = "TARGET_TESTER"
        elif "TARGET_CONFIG" in text:
            target = "TARGET_CONFIG"
        else:
            print("  [AGENT 1] WARNING: no TARGET sentinel -- defaulting TARGET_CONFIG")
            target = "TARGET_CONFIG"

    return target, text


# ============================================================================
#  AGENT 2: CRITIC  (gpt-4o-mini or Haiku fallback)
# ============================================================================

_CRITIC_SYSTEM = """\
You are a quantitative risk officer and parameter optimizer. You have two roles:
  (1) Catch hard concrete blockers: guardrail breach, wrong evidence, repeated failure.
  (2) Evaluate parameter signals from the Opportunity Analyst against the trend history.

You now have:
  - Full backtest report + change log
  - Agent 1's quantified verdicts (per-item and aggregated parameter_signals)
  - Parameter trend section: for each parameter with >=3 tested values,
    a value->delta series and an explicit monotonicity read

EVALUATION CHECKLIST:
  1. Evidence accuracy -- is data cited by the Analyst FACTUALLY CORRECT?
     Quote the actual figure from the report if the Analyst misread it.
  2. Guardrail risk -- would this change almost certainly breach a named guardrail?
     max_drawdown < -45%, n_closed drop > 40% of baseline, avg_cash_pct > 40%.
     Name the guardrail and explain the mechanism.
  3. Already tried -- is an identical change already in the change_log as failed?
     Quote the log line if so.
  4. TENSION CHECK: When Agent 1's parameter_signals show a 'tension' field (non-null),
     the Analyst has flagged that missed-winner and dodged-loser evidence point OPPOSITE
     ways on the same gate. You MUST:
       a. Report the net $ explicitly: 'Net: winners +X% vs dodged -Y% saved => net Z%'
       b. NOT pick the louder side -- instead, recommend the MINIMUM change (half magnitude)
          and mark it for extra scrutiny.
  5. TREND ALIGNMENT: Prefer parameters where the trend direction (from trend section)
     and Agent 1's new evidence agree. When they diverge, flag it as REVISE with
     a note: 'trend says {direction} but Agent 1 says opposite -- verify before committing.'

CRITICAL DUTY -- regime-gaming check:
  The backtest period is 2020-2024 and includes COVID crash, 2022 bear, 2023
  recovery, and 2024 mixed. The backtest can punish recklessness via drawdown
  and Sharpe -- trust those numbers. Still flag risk-increasing changes for scrutiny.
  A change that raises fitness by taking on more risk (e.g. widening stop-loss,
  raising per-position size or conviction multipliers, increasing max positions,
  loosening a protective veto, REMOVING a sector from BEAR_BANNED_SECTORS,
  weakening the bear-regime MA200 requirement in gate_momentum) will look GOOD
  in the backtest while making the live strategy more fragile in future bear markets.
  When a proposal increases risk-taking:
    - Do NOT reject it -- the backtest should still test it.
    - DO flag it with VERDICT: REVISE and instruct the Synthesizer to implement the
      SMALLEST version of the change (e.g. half the proposed magnitude), noting that
      the fitness gain may be regime-dependent and should be treated with caution.
  SPECIAL RULE for bear-regime protections: any proposal to remove a sector from
  BEAR_BANNED_SECTORS or weaken the MA200 bear-regime block must be flagged REVISE
  with instruction to pilot only one sector change at a time and verify the 2022
  bear period specifically does not regress before keeping.

REJECT is valid ONLY for:
  (a) Guardrail breach: almost certain violation -- name it, explain the mechanism.
  (b) Factually wrong evidence: quote the correct figure from the full report provided.
  (c) Exact repeat of a logged failed change: quote the change_log line.

NOT valid grounds for REJECT:
  - Suspicion of overfitting or small sample size. We have a fast backtest; test it.
  - General skepticism. Test it instead.
  - Concern that only one metric improves. The fitness function handles that.
  Default to APPROVE when in doubt. A tested-and-failed change costs one backtest run.
  An untested REJECT wastes a whole iteration of improvement opportunity.

n_closed GUARDRAIL -- READ CAREFULLY:
  The user prompt gives you three numbers: baseline n_closed, the 60% floor, and the
  CURRENT run's n_closed. Read them. Do the arithmetic yourself.
  REJECT on n_closed is valid ONLY when BOTH conditions hold:
    (i)  current n_closed is already close to the floor (within ~10% above it), AND
    (ii) the proposed change clearly removes a large fraction of trades.
  If current n_closed is hundreds of trades above the floor, a stop or sizing tweak
  CANNOT plausibly breach it in one step. Do NOT cite that count as "at risk" --
  that is a phantom concern. APPROVE or REVISE those changes on other merits.
  Do NOT state that a number above the floor is below it -- recheck your arithmetic.

Your LAST LINE must be exactly one of these three forms (no other text on that line):
  VERDICT: APPROVE
  VERDICT: REVISE: <one concrete instruction to the Synthesizer, one sentence>
  VERDICT: REJECT: <specific hard blocker -- guardrail name, exact wrong quote, log ref>

Keep the entire response under 300 words. ASCII only."""


def run_critic(analyst_text, summary, report_text, change_log_tail, use_gpt, baseline_n_closed):
    """
    Run Agent 2 (Parameter Optimizer / Critic).
    Receives Agent 1's signals + full report + params trend section.
    Returns response text.
    """
    print("\n  [AGENT 2 - Parameter Optimizer]", flush=True)

    # Load parameter trend section
    trend_block = _load_params_trend()
    trend_section = (f"\nPARAMETER TREND HISTORY:\n{trend_block}\n"
                     if trend_block else "\n(No parameter trend data -- params_history.json not found)\n")

    user = (
        f"ANALYST PROPOSAL (with quantified verdicts):\n{analyst_text}\n\n"
        f"FULL BACKTEST REPORT (verify Analyst evidence against these figures):\n"
        f"{report_text}\n\n"
        f"RECENT CHANGE LOG (check for repeated failed changes):\n"
        f"{change_log_tail}\n"
        f"{trend_section}"
        f"{_fmt_walk_forward_block()}\n"
        f"GUARDRAILS (hard limits):\n"
        f"  max_drawdown must stay >= {DRAWDOWN_CEILING}%\n"
        f"  n_closed must stay >= {STAY_INVESTED_MIN*100:.0f}% of baseline\n"
        f"  avg_cash_pct must stay <= {CASH_IDLE_MAX*100:.0f}%\n\n"
        f"BASELINE TRADE COUNT (exact figures -- do NOT guess or round):\n"
        f"  baseline n_closed      = {baseline_n_closed}\n"
        f"  60% floor              = {int(0.60 * baseline_n_closed)} trades\n"
        f"  current run n_closed   = {int(summary.get('n_closed') or 0)}\n"
        f"  headroom above floor   = {int(summary.get('n_closed') or 0) - int(0.60 * baseline_n_closed)}"
        f" trades ({max(0.0, (int(summary.get('n_closed') or 0) / max(1, int(0.60 * baseline_n_closed)) - 1) * 100):.0f}% above floor)\n"
        f"\n"
        f"  n_closed REJECT RULE: REJECT on n_closed is valid ONLY if BOTH:\n"
        f"    (i)  current n_closed ({int(summary.get('n_closed') or 0)}) is within 10% above"
        f" the floor ({int(0.60 * baseline_n_closed)}), AND\n"
        f"    (ii) the proposed change plausibly removes a large fraction of trades.\n"
        f"  Current headroom is {int(summary.get('n_closed') or 0) - int(0.60 * baseline_n_closed)}"
        f" trades above the floor. A modest stop/sizing change cannot bridge that gap\n"
        f"  in one step -- do NOT REJECT on n_closed in that case. Evaluate on other\n"
        f"  merits or issue APPROVE/REVISE so the backtest can test it.\n"
        f"  Do NOT state that {int(summary.get('n_closed') or 0)} is below or near"
        f" {int(0.60 * baseline_n_closed)} -- verify your arithmetic before citing numbers.\n"
        f"  CRITICAL: Do NOT cite a previous change_log entry about \"n_closed risk\" as grounds\n"
        f"  for REJECT unless the CURRENT headroom (computed above) is within 10% of the floor.\n"
        f"  A log entry about n_closed risk from a prior session is not evidence the current\n"
        f"  proposal will breach the guardrail -- it must be re-evaluated against current numbers.\n"
        f"  If current headroom is >50 trades above floor, n_closed REJECT is categorically\n"
        f"  invalid regardless of what the change log says.\n\n"
        f"Remember: end your response with a VERDICT line as instructed."
    )

    if use_gpt and _OPENAI_OK:
        text, _, _ = _call_openai(MODEL_CRITIC_GPT, _CRITIC_SYSTEM, user, max_tokens=700)
    else:
        text, _, _ = _call_anthropic(MODEL_ANALYST, _CRITIC_SYSTEM, user, max_tokens=700)

    print(f"  [AGENT 2] Response:\n{textwrap.indent(text[:800], '    ')}", flush=True)
    return text


def parse_verdict(critic_text):
    """
    Parse the structured VERDICT line from Critic output.
    Returns (verdict: str, detail: str).
    verdict is 'APPROVE', 'REVISE', or 'REJECT'.
    No VERDICT line found -> APPROVE (default to action, let backtest decide).
    """
    for line in reversed(critic_text.splitlines()):
        m = re.match(r"VERDICT:\s*(APPROVE|REVISE|REJECT)(:\s*(.*))?",
                     line.strip(), re.IGNORECASE)
        if m:
            verdict = m.group(1).upper()
            detail  = (m.group(3) or "").strip()
            return verdict, detail
    # No structured verdict -> default to action
    return "APPROVE", "(no VERDICT line found -- defaulting to APPROVE)"


# ============================================================================
#  AGENT 3: SYNTHESIZER  (Opus -- implements the change)
# ============================================================================

_SYNTH_SYSTEM_CONFIG = """\
You are a senior quantitative developer implementing a specific CONFIG change.
TARGET FILE: engine/portfolio_simulator.py (CONFIG block only).

RULES:
  1. Edit ONLY values inside the CONFIG block (between # CONFIG and # END CONFIG).
  2. Inline-comment every changed line with the old value:   # was 0.20
  3. Max 50 changed lines total.
  4. Output ONLY PATCH blocks -- no prose outside the blocks.
  5. ASCII only. No Unicode.

PATCH FORMAT (repeat for each independent change):
<<<PATCH_START>>>
<<<OLD>>>
exact original line(s) -- must match the file character-for-character
<<<NEW>>>
replacement line(s)
<<<PATCH_END>>>

End with one summary line: "# Changed: <one sentence>" outside the patch blocks."""


_SYNTH_SYSTEM_TESTER = """\
You are a senior quantitative developer implementing a specific gate change.
TARGET FILE: engine/tester.py

RULES:
  1. Edit ONLY gate logic, thresholds, and scoring -- preserve sector maps,
     ticker lists, and file paths exactly.
  2. Inline-comment every changed line with the old value:   # was 5.5
  3. Max 50 changed lines total.
  4. Output ONLY PATCH blocks -- no prose outside the blocks.
  5. ASCII only. No Unicode.

PATCH FORMAT (repeat for each independent change):
<<<PATCH_START>>>
<<<OLD>>>
exact original line(s) -- must match the file character-for-character
<<<NEW>>>
replacement line(s)
<<<PATCH_END>>>

End with one summary line: "# Changed: <one sentence>" outside the patch blocks."""


def run_synthesizer(target, analyst_text, critic_text, revise_note=""):
    """
    Run Agent 3 (Synthesizer, Opus).
    Reads the target file in full to give Opus exact context.
    revise_note: concrete revision from Critic REVISE verdict; injected into prompt.
    Returns (patches: list, raw_response: str).
    """
    print("\n  [AGENT 3 - Synthesizer / Opus]", flush=True)

    # Read full target file for context
    target_file = TESTER_MAIN if target == "TARGET_TESTER" else SIMULATOR
    try:
        with open(target_file, "r", encoding="ascii", errors="replace") as f:
            file_content = f.read()
    except Exception as exc:
        print(f"  [AGENT 3] Cannot read {target_file}: {exc}")
        return [], ""

    system = (_SYNTH_SYSTEM_TESTER if target == "TARGET_TESTER"
               else _SYNTH_SYSTEM_CONFIG)

    # For TARGET_CONFIG, send only the CONFIG block (saves tokens)
    if target == "TARGET_CONFIG":
        context_section = _read_config_block()
        context_label   = "CONFIG BLOCK (only editable section):"
    else:
        context_section = file_content
        context_label   = "FULL FILE (edit only gate logic/thresholds):"

    revise_block = (f"\nCRITIC REVISION INSTRUCTION (apply this):\n{revise_note}\n"
                    if revise_note else "")
    user = (
        f"ANALYST PROPOSAL:\n{analyst_text}\n\n"
        f"CRITIC REVIEW:\n{critic_text}\n"
        f"{revise_block}\n"
        f"{context_label}\n"
        f"```\n{context_section}\n```\n\n"
        f"Implement the analyst's proposal (incorporating any critic revision above). "
        f"Output ONLY PATCH blocks."
    )

    text, _, _ = _call_anthropic(MODEL_SYNTHESIZER, system, user, max_tokens=3000)
    print(f"  [AGENT 3] Response preview (first 800 chars):\n"
          f"{textwrap.indent(text[:800], '    ')}", flush=True)

    patches = extract_patches(text)
    print(f"  [AGENT 3] Extracted {len(patches)} patch block(s)  "
          f"(~{_count_changed_lines(patches)} lines changed)")
    return patches, text


# ============================================================================
#  ITERATION LOGIC
# ============================================================================

def _target_file_path(target):
    return TESTER_MAIN if target == "TARGET_TESTER" else SIMULATOR


def run_one_iteration(baseline_summary, baseline_n_closed, current_fitness,
                       iteration_num, use_gpt, budget_usd):
    """
    Execute one full Analyst -> Critic -> Synthesizer cycle.
    Returns (kept: bool, new_fitness: float, target: str, detail_str: str).
    """
    print(f"\n{'='*60}")
    print(f"  ITERATION {iteration_num}  |  current fitness={current_fitness:.4f}")
    print(f"  Session spend: ${_session_cost:.4f} / budget ${budget_usd:.2f}")
    print(f"{'='*60}")

    # --- Reload current report (may differ from baseline if prior iter kept) ---
    summary, full_data = load_report()
    if summary is None:
        return False, current_fitness, "NONE", "report load failed"

    report_text  = _fmt_report_for_agent(summary, full_data)
    config_block = _read_config_block()
    change_log   = _read_change_log_tail(80)
    fit_before   = compute_fitness(summary)

    # Budget pre-check
    if _session_cost >= budget_usd:
        print("  [BUDGET] Exhausted -- stopping before Agent 1")
        return False, current_fitness, "NONE", "budget exhausted"

    # --- AGENT 1: Analyst ---
    target, analyst_text = run_analyst(report_text, config_block, change_log)
    print(f"  [ROUTING] -> {target}")

    if _session_cost >= budget_usd:
        print("  [BUDGET] Exhausted after Agent 1")
        return False, current_fitness, target, "budget exhausted"

    # --- AGENT 2: Critic ---
    critic_text = run_critic(analyst_text, summary, report_text, change_log, use_gpt, baseline_n_closed)
    verdict, verdict_detail = parse_verdict(critic_text)
    _session_verdicts[verdict] = _session_verdicts.get(verdict, 0) + 1
    sfx = f": {verdict_detail}" if verdict_detail else ""
    print(f"  [VERDICT] {verdict}{sfx}", flush=True)

    # REJECT with a hard concrete blocker -> skip this iteration entirely, no file changes
    if verdict == "REJECT":
        _append_log(
            f"SKIPPED (Critic REJECT)  file={_target_file_path(target).name}  "
            f"fitness_before={fit_before:.4f}  "
            f"blocker={verdict_detail[:150] if verdict_detail else critic_text[:100]}"
        )
        print(f"  [SKIP] Critic REJECT -- no file modified.")
        return False, current_fitness, target, critic_text

    if _session_cost >= budget_usd:
        print("  [BUDGET] Exhausted after Agent 2")
        return False, current_fitness, target, "budget exhausted"

    # REVISE -> Synthesizer applies the concrete revision note; APPROVE -> implement as-is
    revise_note = verdict_detail if verdict == "REVISE" else ""

    # --- AGENT 3: Synthesizer ---
    patches, synth_text = run_synthesizer(target, analyst_text, critic_text, revise_note)
    if not patches:
        reason = "Synthesizer produced no valid PATCH blocks"
        log_rollback(reason, _target_file_path(target).name, fit_before, patches=None)
        return False, current_fitness, target, synth_text

    # --- Backup target file ---
    target_path = _target_file_path(target)
    backup = make_backup(target_path, tag=f"iter{iteration_num}")
    if backup is None:
        print("  [ITER] Backup failed -- aborting iteration for safety")
        return False, current_fitness, target, "backup failed"

    # --- Apply patches ---
    ok, n_applied, err = apply_patches(target_path, patches)
    if not ok:
        reason = f"Patch apply failed after {n_applied}/{len(patches)}: {err[:120]}"
        log_rollback(reason, target_path.name, fit_before, patches=patches)
        restore_backup(target_path, backup)
        return False, current_fitness, target, synth_text

    print(f"  [PATCH] Applied {n_applied}/{len(patches)} patches to {target_path.name}")

    # --- Re-run simulator ---
    sim_ok, sim_err = run_simulator()
    if not sim_ok:
        reason = f"Simulator crashed after patch: {sim_err[:150]}"
        log_rollback(reason, target_path.name, fit_before, patches=patches)
        restore_backup(target_path, backup)
        return False, current_fitness, target, synth_text

    # --- Load new report ---
    new_summary, _ = load_report()
    if new_summary is None:
        reason = "Cannot load portfolio_report.json after run"
        log_rollback(reason, target_path.name, fit_before, patches=patches)
        restore_backup(target_path, backup)
        return False, current_fitness, target, synth_text

    fit_after = compute_fitness(new_summary)

    # --- Guardrail check ---
    gr_ok, gr_reason = check_guardrails(new_summary, baseline_n_closed)
    if not gr_ok:
        log_rollback(f"Guardrail tripped: {gr_reason}",
                     target_path.name, fit_before, fit_after, patches=patches)
        restore_backup(target_path, backup)
        return False, current_fitness, target, synth_text

    # --- Fitness check ---
    # FIX: compare fit_after against current_fitness (the authoritative running
    # best-so-far), NOT fit_before (read from portfolio_report.json).
    # After a rollback, portfolio_report.json still holds the failed sim's
    # numbers -- fit_before is stale-low and cannot be trusted as the keep
    # threshold.  current_fitness is only ever updated on a KEPT change, so
    # it is always >= the starting fitness and immune to stale-JSON drift.
    if fit_after <= current_fitness:
        reason = (f"Fitness did not improve over best-so-far: "
                  f"best={current_fitness:.4f}  new={fit_after:.4f}  "
                  f"json_before={fit_before:.4f}  "
                  f"(ret={new_summary.get('total_return_pct',0):+.2f}%  "
                  f"sharpe={new_summary.get('sharpe',0):.2f})")
        log_rollback(reason, target_path.name, fit_before, fit_after, patches=patches)
        restore_backup(target_path, backup)
        return False, current_fitness, target, synth_text

    # --- Stage 2: walk-forward robustness gate (every Nth KEPT change only) ---
    global _kept_since_wf_check, _walk_forward_cache
    candidate_keep_number = _kept_since_wf_check + 1
    if candidate_keep_number % WALK_FORWARD_CHECK_EVERY == 0:
        prev_wf   = _walk_forward_cache   # snapshot -- run_walk_forward_check overwrites the cache
        wf_result = run_walk_forward_check(iteration_num)
        # INSUFFICIENT_DATA baselines have a meaningless spread of 0.0 -- comparing
        # against one would make any real spread look like a regression. Treat a
        # not-yet-comparable baseline the same as "no baseline" (skip the check).
        prev_comparable = prev_wf is not None and prev_wf["verdict"] != "INSUFFICIENT_DATA"
        if prev_comparable:
            spread_regressed    = wf_result["spread"] > prev_wf["spread"] + WALK_FORWARD_SPREAD_TOLERANCE
            flipped_to_overfit  = (prev_wf["verdict"] in ("ROBUST", "BEAR_WEAK")
                                    and wf_result["verdict"] == "OVERFIT")
            if spread_regressed or flipped_to_overfit:
                reason = (f"Walk-forward regression: spread {prev_wf['spread']:.2f} -> "
                          f"{wf_result['spread']:.2f}  (tolerance {WALK_FORWARD_SPREAD_TOLERANCE:.1f})  "
                          f"verdict {prev_wf['verdict']} -> {wf_result['verdict']}")
                log_rollback(reason, target_path.name, fit_before, fit_after, patches=patches)
                restore_backup(target_path, backup)
                # Code is being reverted -- cache must reflect what's actually on disk.
                _walk_forward_cache = prev_wf
                return False, current_fitness, target, synth_text
        _append_log(
            f"WALK_FORWARD_GATE  iteration={iteration_num}  PASSED  "
            f"verdict={wf_result['verdict']}  spread={wf_result['spread']:.4f}  "
            f"avg_fitness={wf_result['avg_fitness']:.4f}"
        )
    _kept_since_wf_check = candidate_keep_number

    # --- Keep the change ---
    ev_match = re.search(r"EVIDENCE[:\s]*\n(.*?)(?:\n\n|\Z)", analyst_text, re.DOTALL)
    evidence = ev_match.group(1).strip()[:120] if ev_match else analyst_text[:80]
    # Log uses current_fitness as the authoritative before-value so the delta
    # in the log always reflects the true improvement over the running best.
    log_kept(target_path.name, current_fitness, fit_after, evidence, patches=patches)

    # Sync tester copy if tester was changed
    if target == "TARGET_TESTER":
        _sync_tester_copy()

    return True, fit_after, target, synth_text


# ============================================================================
#  BASELINE REPORT PRINTER
# ============================================================================

def print_baseline(summary, fitness, gr_ok, gr_reason, baseline_n_closed):
    print()
    print("=" * 60)
    print("  BASELINE FITNESS REPORT")
    print("=" * 60)
    ret    = summary.get("total_return_pct", 0)
    sharpe = summary.get("sharpe", 0)
    mdd    = summary.get("max_drawdown", 0)
    n_cl   = summary.get("n_closed", 0)
    wr     = summary.get("win_rate", 0)
    pf     = summary.get("profit_factor", 0)
    spy_r  = summary.get("spy_return", 0)
    spy_s  = summary.get("spy_sharpe", "N/A")
    spy_d  = summary.get("spy_max_drawdown", "N/A")
    cash   = summary.get("avg_cash_pct", None)
    print(f"  Total Return   : {ret:+.2f}%  (SPY: {spy_r:+.2f}%)")
    print(f"  Sharpe         : {sharpe:.2f}  (SPY: {spy_s})")
    print(f"  Max Drawdown   : {mdd:+.2f}%  (SPY: {spy_d}%)")
    print(f"  N Closed       : {n_cl}  |  Win Rate: {wr:.1f}%  |  Profit Factor: {pf:.2f}x")
    if cash is not None:
        print(f"  Avg Cash Idle  : {cash:.1%}")
    print()
    print(f"  FITNESS        : {fitness:.4f}")
    print(f"    = total_return * 0.5 + (sharpe * 20) * 0.5")
    print(f"    = {ret:.2f} * 0.5 + ({sharpe:.2f} * 20) * 0.5")
    print(f"    = {ret*0.5:.4f} + {sharpe*20*0.5:.4f}")
    print()
    gr_label = "PASS" if gr_ok else "FAIL"
    print(f"  GUARDRAIL STATUS : {gr_label}")
    if not gr_ok:
        print(f"    Reason: {gr_reason}")
    else:
        print(f"    Drawdown ceiling : {DRAWDOWN_CEILING:.0f}%   [current {mdd:+.2f}%  OK]")
        min_t = int(STAY_INVESTED_MIN * baseline_n_closed)
        print(f"    Stay-invested    : min {min_t} trades ({STAY_INVESTED_MIN*100:.0f}% of {baseline_n_closed})  OK")
        if cash is not None:
            print(f"    Cash idle        : {cash:.1%} <= {CASH_IDLE_MAX*100:.0f}%  OK")
        else:
            print(f"    Cash idle        : avg_cash_pct not in JSON (guardrail skipped)")
    print("=" * 60)


# ============================================================================
#  MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="3-agent auto-optimizer for portfolio_simulator")
    parser.add_argument("--iterations",   type=int,   default=5,
                        help="Max iterations (default 5)")
    parser.add_argument("--once",         action="store_true",
                        help="Run exactly one iteration")
    parser.add_argument("--no-gpt",       action="store_true",
                        help="Use Haiku for Critic instead of gpt-4o-mini")
    parser.add_argument("--budget",       type=float, default=5.00,
                        help="Max API spend USD (default $5.00)")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Run sim, print fitness + guardrail status, no edits")
    args = parser.parse_args()

    use_gpt = not args.no_gpt
    budget  = args.budget
    n_iters = 1 if args.once else args.iterations

    print("=" * 60)
    print("  AUTO OPTIMIZER  v2  --  portfolio fitness loop")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Budget   : ${budget:.2f}")
    if args.baseline_only:
        print("  Mode     : --baseline-only (no edits)")
    elif args.once:
        print("  Mode     : --once")
    else:
        print(f"  Mode     : {n_iters} iterations")
    critic_model = MODEL_CRITIC_GPT if use_gpt else MODEL_ANALYST
    print(f"  Analyst  : {MODEL_ANALYST}")
    print(f"  Critic   : {critic_model}")
    print(f"  Synth    : {MODEL_SYNTHESIZER}")
    print("=" * 60)

    # --- API sanity checks ---
    if not args.baseline_only:
        if not _ANTHROPIC_OK:
            print("  ERROR: anthropic package not installed -- pip install anthropic")
            sys.exit(1)
        if use_gpt and not _OPENAI_OK:
            print("  WARNING: openai not installed -- using Haiku for Critic")
            use_gpt = False

    # --- Step 1: Baseline simulation ---
    print("\n  [STEP 1] Running baseline simulation ...")
    sim_ok, sim_err = run_simulator()
    if not sim_ok:
        print(f"  ERROR: Baseline simulation failed:\n{sim_err[:300]}")
        sys.exit(1)

    summary, full_data = load_report()
    if summary is None:
        print("  ERROR: Cannot load portfolio_report.json after baseline run")
        sys.exit(1)

    baseline_n_closed = int(summary.get("n_closed") or 0)
    baseline_fitness  = compute_fitness(summary)
    gr_ok, gr_reason  = check_guardrails(summary, baseline_n_closed)

    print_baseline(summary, baseline_fitness, gr_ok, gr_reason, baseline_n_closed)

    if args.baseline_only:
        print("\n  [--baseline-only] Harness verified. No edits made.")
        return

    if not gr_ok:
        print(f"\n  WARNING: Baseline itself fails guardrail: {gr_reason}")
        print("  Continuing -- agents will try to improve the system.")

    # --- Step 2: Iteration loop ---
    current_fitness  = baseline_fitness
    current_n_closed = baseline_n_closed
    kept_count = rolled_back_count = 0

    for i in range(1, n_iters + 1):
        if _session_cost >= budget:
            print(f"\n  [BUDGET] ${_session_cost:.3f} >= ${budget:.2f} -- stopping.")
            break

        kept, new_fit, target, detail = run_one_iteration(
            summary, current_n_closed, current_fitness,
            i, use_gpt, budget
        )

        if kept:
            kept_count   += 1
            current_fitness = new_fit
            # Reload n_closed from updated report
            upd_sum, _ = load_report()
            if upd_sum:
                current_n_closed = int(upd_sum.get("n_closed") or current_n_closed)
            print(f"\n  [ITER {i}] KEPT     fitness={current_fitness:.4f}  target={target}")
        else:
            rolled_back_count += 1
            print(f"\n  [ITER {i}] REVERTED fitness={current_fitness:.4f}  target={target}")

    # --- Belt-and-suspenders: session must never end below its start ---
    # This should be structurally impossible after the keep-gate fix, but log
    # an explicit warning if it ever occurs so it cannot pass silently.
    if current_fitness < baseline_fitness:
        _warn = (f"WARNING: session ended BELOW starting fitness  "
                 f"start={baseline_fitness:.4f}  final={current_fitness:.4f}  "
                 f"delta={current_fitness - baseline_fitness:+.4f}  "
                 f"-- rollback logic may still have a gap")
        _append_log(_warn)
        print(f"\n  *** {_warn} ***")

    # --- Final summary ---
    n_approve     = _session_verdicts.get("APPROVE", 0)
    n_revise      = _session_verdicts.get("REVISE",  0)
    n_reject      = _session_verdicts.get("REJECT",  0)
    n_tested      = n_approve + n_revise
    n_backtest_rb = max(0, rolled_back_count - n_reject)
    print()
    print("=" * 60)
    print("  SESSION COMPLETE")
    print(f"  Iterations run      : {kept_count + rolled_back_count}")
    print(f"  Critic verdicts     : APPROVE={n_approve}  REVISE={n_revise}  REJECT={n_reject}")
    print(f"  Went to backtest    : {n_tested}")
    print(f"    Kept              : {kept_count}")
    print(f"    Rolled back       : {n_backtest_rb}  (fitness/guardrail/patch fail)")
    print(f"  Skipped (REJECT)    : {n_reject}  (Critic hard block -- not tested)")
    print(f"  Fitness start       : {baseline_fitness:.4f}")
    print(f"  Fitness final       : {current_fitness:.4f}")
    print(f"  Delta               : {current_fitness - baseline_fitness:+.4f}")
    print(f"  API cost (USD)      : ${_session_cost:.4f}")
    print(f"  Tokens              : in={_session_tokens['in']}  out={_session_tokens['out']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
