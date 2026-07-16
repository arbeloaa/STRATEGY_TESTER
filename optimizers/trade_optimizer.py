#!/usr/bin/env python3
"""
trade_optimizer.py  --  Two-agent parameter optimizer driven by trade evidence
==============================================================================
Agent 1 (Haiku / Trade Analyst): reads 30 sampled trades from
  reports/sampled_trades.json and identifies which parameters appear
  mis-calibrated from the actual trade outcomes.
Agent 2 (Opus / Parameter Optimizer): reads Agent 1's verdicts plus
  the Historian summary and proposes a single specific edit to
  strategy_params.json, which both tester.py and portfolio_simulator.py
  load at startup.
Agent 3 (Haiku / Historian): reads complete params_history.json once per
  session and produces a structured exploration summary for Agent 2.

Loop per iteration:
  1. Run trade_analyzer_prep.py -> reports/sampled_trades.json
  2. Agent 1 analyses each trade narrative -> parameter verdicts (JSON)
  3. Agent 2 reads Agent 1 signals + Historian summary -> proposes one JSON-path edit
  4. Run portfolio_simulator.py -> compute fitness
  5. Check guardrails  (drawdown, stay-invested, sanity)
  6. Keep or revert  -> append to params_history.json + logs/change_log.txt

Usage:
  python trade_optimizer.py --once
  python trade_optimizer.py --iterations 5 --budget 5.00
  python trade_optimizer.py --baseline-only
"""

import sys, json, shutil, subprocess, time, math, argparse
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

sys.path.insert(0, str(Path(__file__).resolve().parent))  # optimizers/
import walk_forward as _wf

# ============================================================================
#  PATHS
# ============================================================================
PROJECT_ROOT      = Path(__file__).resolve().parents[1]
SIMULATOR         = PROJECT_ROOT / "engine"     / "portfolio_simulator.py"
PREP_SCRIPT       = PROJECT_ROOT / "optimizers" / "trade_analyzer_prep.py"
PARAMS_JSON       = PROJECT_ROOT / "config"     / "strategy_params.json"
CURRENT_BEST_JSON = PROJECT_ROOT / "config"     / "current_best_params.json"
SAMPLED_TRADES    = PROJECT_ROOT / "reports"    / "sampled_trades.json"
REPORT_JSON       = PROJECT_ROOT / "reports"    / "portfolio_report.json"
REPORT_TXT        = PROJECT_ROOT / "reports"    / "portfolio_report.txt"
PARAMS_HISTORY    = PROJECT_ROOT / "logs"       / "params_history.json"
CHANGE_LOG        = PROJECT_ROOT / "logs"       / "change_log.txt"
VERDICTS_DIR      = PROJECT_ROOT / "logs"       / "trade_verdicts"

SIM_START = "2020-01-01"
SIM_END   = "2024-12-31"

# Last confirmed-reproducible best fitness. Used as the floor for best_ever, which is
# computed dynamically at session start as max(this, current_best_fitness) so it rises
# automatically with every real improvement and never needs manual editing again.
# Updated to 64.89 on 2026-07-02: 2020-2024 run post regime_exposure_cap fix
# (total_return=+113.97%, sharpe=0.79 -> fitness = 113.97*0.5 + 0.79*20*0.5 = 64.89).
BEST_FITNESS_EVER_OVERRIDE = 64.89

PARAMETER_FAMILIES = {
    "MA_exit_sensitivity": {
        "members": [
            "exits.below_ma_trend_floor", "exits.ma_confirm_days",
            "exits.ma_breakdown_pct", "exits.min_hold_days", "exits.ma100_breakdown_days",
        ],
        "description": (
            "All control when MA-based exits fire. below_ma_trend_floor has been the most"
            " impactful so far; remaining members are untested."
        ),
    },
    "profit_capture": {
        "members": [
            "exits.trailing_stop_pct", "exits.trail_activate_gain_pct", "exits.take_profit_pct",
        ],
        "description": "Control how much of a winner's gain is captured before exit.",
    },
    "entry_quality_solar": {
        "members": [
            "gates.gm_tops.solar_hw", "gates.gm_tops.solar_install", "gates.gm_tops.renewables",
            "gates.gm_mids.solar_hw", "gates.gm_mids.solar_install", "gates.gm_mids.renewables",
        ],
        "description": (
            "Solar and renewable sector gross margin thresholds. Raise gm_tops to tighten;"
            " gm_mids raise crashed fitness hard -- avoid."
        ),
    },
    "entry_quality_tech": {
        "members": [
            "gates.gm_configs.cyber", "gates.gm_configs.infra_saas", "gates.gm_configs.fintech",
        ],
        "description": (
            "Tech sector gross margin thresholds. infra_saas tightening failed hard in prior"
            " sessions -- be cautious."
        ),
    },
    "position_sizing": {
        "members": [
            "sizing.per_buy_fraction",
            "sizing.conviction_mult.HIGH", "sizing.conviction_mult.MED", "sizing.conviction_mult.LOW",
            "sizing.regime_position_mult.BULL_STRONG", "sizing.regime_position_mult.BULL_WEAK",
            "sizing.regime_position_mult.BEAR_GRIND", "sizing.regime_position_mult.BEAR_VOLATILE",
            "sizing.regime_max_positions.BEAR_VOLATILE",
        ],
        "description": (
            "COMPLETELY UNEXPLORED. No attempts on any of these parameters yet."
            " High potential -- start with per_buy_fraction or regime_position_mult."
        ),
    },
    "regime_gate_adjustments": {
        "members": [
            "gates.regime_adjustments.BULL_STRONG", "gates.regime_adjustments.BULL_WEAK",
            "gates.regime_adjustments.BEAR_GRIND", "gates.regime_adjustments.BEAR_VOLATILE",
            "gates.conviction_thresholds.high_margin", "gates.conviction_thresholds.med_margin",
        ],
        "description": (
            "UNEXPLORED. Control how much harder gates become in bear regimes."
            " High leverage over entry quality during market stress."
        ),
    },
    "regime_exposure_caps": {
        "members": [
            "sizing.regime_exposure_cap.BULL_STRONG",
            "sizing.regime_exposure_cap.BULL_WEAK",
            "sizing.regime_exposure_cap.BEAR_GRIND",
            "sizing.regime_exposure_cap.BEAR_VOLATILE",
        ],
        "status": "UNEXPLORED",
        "description": (
            "COMPLETELY UNEXPLORED. New structural brake added 2026-07-02: caps total MTM/equity"
            " exposure per regime independently of position count and per-buy sizing."
            " BULL_WEAK=0.85 and BEAR_VOLATILE=0.40 are first-guess values that have never been"
            " tuned. High headroom -- e.g. is 0.40 optimal for BEAR_VOLATILE, or would 0.30 or"
            " 0.50 do better? Start with BEAR_VOLATILE (most impactful) then BEAR_GRIND."
        ),
    },
}

# ============================================================================
#  WALK-FORWARD SETTINGS
# ============================================================================
WALK_FORWARD_CHECK_EVERY      = 3    # run after every Nth kept change
WALK_FORWARD_SPREAD_TOLERANCE = 8.0  # revert kept change if spread worsens by more than this

_walk_forward_cache   = None    # dict from last run_walk_forward_check(), or None
_historian_summary    = None    # set by run_historian() once per session, before iteration 1
_current_best_fitness = 0.0     # fitness of current_best_params.json; updated on every KEEP

# ============================================================================
#  MODELS
# ============================================================================
MODEL_ANALYST   = "claude-haiku-4-5-20251001"
MODEL_HISTORIAN = "claude-haiku-4-5-20251001"
MODEL_OPTIMIZER = "claude-opus-4-8"

_COST_PER_1M = {
    "claude-haiku-4-5-20251001": {"in": 0.80,  "out": 4.00},
    "claude-opus-4-8":           {"in": 15.00, "out": 75.00},
}

_session_cost      = 0.0
_session_tokens    = {"in": 0, "out": 0}
_session_timestamp   = ""   # set in main(); used by _save_verdict()
_tested_this_session = set()  # (param_path, str(new_value)) — every proposal sent to the sim this session
_known_reverted      = {}     # (param_path, str(new_value)) -> delta_str — loaded from history at session start


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
#  FITNESS + GUARDRAILS
# ============================================================================
DRAWDOWN_CEILING  = -45.0
STAY_INVESTED_MIN = 0.60
CASH_IDLE_MAX     = 0.40


def compute_fitness(summary):
    ret    = float(summary.get("total_return_pct") or 0.0)
    sharpe = float(summary.get("sharpe")           or 0.0)
    if math.isnan(ret):    ret    = 0.0
    if math.isnan(sharpe): sharpe = 0.0
    return round(ret * 0.5 + (sharpe * 20.0) * 0.5, 4)


def check_guardrails(summary, baseline_n_closed):
    n_closed = summary.get("n_closed", 0)
    dd       = float(summary.get("max_drawdown") or 0.0)
    cash_pct = float(summary.get("avg_cash_pct") or 0.0)
    ret      = summary.get("total_return_pct")

    if n_closed == 0 or ret is None or (isinstance(ret, float) and math.isnan(ret)):
        return False, "SANITY: n_closed=0 or total_return_pct missing/NaN"
    if dd < DRAWDOWN_CEILING:
        return False, f"DRAWDOWN: max_drawdown={dd:.2f}% below ceiling {DRAWDOWN_CEILING}%"
    if baseline_n_closed > 0 and n_closed < baseline_n_closed * STAY_INVESTED_MIN:
        return False, (f"STAY_INVESTED: n_closed={n_closed} < "
                       f"{STAY_INVESTED_MIN*100:.0f}% of baseline {baseline_n_closed}")
    if cash_pct > CASH_IDLE_MAX * 100:
        return False, f"CASH_IDLE: avg_cash_pct={cash_pct:.1f}% > {CASH_IDLE_MAX*100:.0f}%"
    return True, "OK"


# ============================================================================
#  SIMULATION RUNNER
# ============================================================================

def run_simulator(timeout=1800):
    print("  [SIM] Running portfolio_simulator.py ...", flush=True)
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(SIMULATOR), "--start", SIM_START, "--end", SIM_END],
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


def run_prep(analysis_report=None, timeout=300):
    """Re-run trade_analyzer_prep.py to refresh sampled_trades.json."""
    print("  [PREP] Running trade_analyzer_prep.py ...", flush=True)
    t0 = time.time()
    cmd = [sys.executable, str(PREP_SCRIPT)]
    if analysis_report:
        cmd += ["--analysis-report", str(analysis_report)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            encoding="ascii", errors="replace",
        )
        elapsed = time.time() - t0
        print(f"  [PREP] Done in {elapsed:.0f}s  exit={result.returncode}", flush=True)
        if result.returncode != 0:
            print(f"  [PREP] ERROR:\n{result.stderr[-400:]}", flush=True)
            return False
        return True
    except Exception as exc:
        print(f"  [PREP] EXCEPTION: {exc}", flush=True)
        return False


# ============================================================================
#  WALK-FORWARD CHECK
# ============================================================================

def run_walk_forward_check(iteration_num):
    """
    Run all four walk-forward periods using the current strategy_params.json.
    Backs up and restores portfolio_report.json because each period overwrites it.
    Updates _walk_forward_cache and returns the verdict dict.
    """
    global _walk_forward_cache

    # Back up the current report so the main iteration loop reads it back correctly.
    report_bak = report_txt_bak = None
    if REPORT_JSON.exists():
        report_bak = REPORT_JSON.with_suffix(".json.wf_bak")
        shutil.copy2(REPORT_JSON, report_bak)
    if REPORT_TXT.exists():
        report_txt_bak = REPORT_TXT.with_suffix(".txt.wf_bak")
        shutil.copy2(REPORT_TXT, report_txt_bak)

    print(f"\n  [WALK-FORWARD] Running {len(_wf.PERIODS)}-period check "
          f"(after iteration {iteration_num}) ...", flush=True)
    try:
        period_results = {p["label"]: _wf.run_period(p) for p in _wf.PERIODS}
    finally:
        if report_bak is not None:
            shutil.copy2(report_bak, REPORT_JSON)
            report_bak.unlink()
        if report_txt_bak is not None:
            shutil.copy2(report_txt_bak, REPORT_TXT)
            report_txt_bak.unlink()

    verdict_info = _wf.robustness_verdict(period_results)
    verdict_info["iteration"]      = iteration_num
    verdict_info["period_results"] = period_results  # kept for session-end summary

    _walk_forward_cache = verdict_info
    _wf.print_summary(period_results, verdict_info)

    _append_change_log(
        f"WALK_FORWARD  iteration={iteration_num}  "
        f"verdict={verdict_info['verdict']}  "
        f"spread={verdict_info['spread']:.4f}  "
        f"avg_fitness={verdict_info['avg_fitness']:.4f}  "
        f"per_period={verdict_info['per_period']}"
    )
    return verdict_info


# ============================================================================
#  BEST-STATE MANAGEMENT  (current_best_params.json only goes up)
# ============================================================================

def save_current_best(fitness_before=None, fitness_after=None):
    """Snapshot current strategy_params.json as the authoritative known-good state.
    Called only on KEEP or at session-start init -- never on reverts."""
    try:
        shutil.copy2(str(PARAMS_JSON), str(CURRENT_BEST_JSON))
        if fitness_before is not None and fitness_after is not None:
            print(f"  [BEST] current_best_params.json updated: "
                  f"fitness {fitness_before:.4f} -> {fitness_after:.4f}")
        else:
            print(f"  [BEST] Saved current_best_params.json")
    except Exception as exc:
        print(f"  [BEST] Save failed: {exc}")


def restore_to_best():
    """Restore strategy_params.json from the known-good snapshot."""
    if not CURRENT_BEST_JSON.exists():
        print("  [RESTORE] ERROR: current_best_params.json missing -- cannot restore")
        return False
    try:
        shutil.copy2(str(CURRENT_BEST_JSON), str(PARAMS_JSON))
        print(f"  [BEST] Reverted: strategy_params.json restored from current_best_params.json")
        return True
    except Exception as exc:
        print(f"  [RESTORE] FAILED: {exc}")
        return False


def make_backup(tag=""):
    """Write an audit-trail backup. Not used for reverts -- current_best_params.json handles that."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    dst = BACKUPS_DIR / f"strategy_params_{ts}{sfx}.json"
    try:
        shutil.copy2(str(PARAMS_JSON), str(dst))
        print(f"  [BACKUP] strategy_params.json -> backups/{dst.name}")
        return dst
    except Exception as exc:
        print(f"  [BACKUP] FAILED: {exc}")
        return None


# ============================================================================
#  PARAMS HISTORY LOG
# ============================================================================

def load_params_history():
    if not PARAMS_HISTORY.exists():
        return []
    try:
        with open(PARAMS_HISTORY) as f:
            return json.load(f)
    except Exception:
        return []


def append_params_history(entry):
    PARAMS_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    history = load_params_history()
    history.append(entry)
    with open(PARAMS_HISTORY, "w") as f:
        json.dump(history, f, indent=2)


def _params_history_summary(history, max_entries=15):
    """Format last N entries for display."""
    tail = history[-max_entries:] if len(history) > max_entries else history
    if not tail:
        return "(no previous iterations)"
    lines = []
    for h in tail:
        outcome = h.get("reason", "REVERTED").upper() if not h.get("kept") else "KEPT"
        wf_str = ""
        if h.get("walk_forward_spread") is not None:
            wf_str = (f"  [WF spread={h['walk_forward_spread']:.1f} "
                      f"verdict={h.get('walk_forward_verdict','?')}]")
        lines.append(
            f"  [{h.get('ts','')}] iter={h.get('iter','?')}  {outcome}  "
            f"{h.get('param_path','?')}: {h.get('old_value')} -> {h.get('new_value')}  "
            f"fitness {h.get('fitness_before','?')} -> {h.get('fitness_after','?')}  "
            f"rationale: {str(h.get('rationale',''))[:80]}{wf_str}"
        )
    return "\n".join(lines)


# ============================================================================
#  CHANGE LOG
# ============================================================================

def _append_change_log(entry):
    CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = f"\n[{ts}] TRADE_OPTIMIZER: {entry}\n"
    try:
        with open(CHANGE_LOG, "a", encoding="ascii", errors="replace") as f:
            f.write(text)
    except Exception as exc:
        print(f"  [LOG] Write failed: {exc}")


def _save_verdict(iteration_num, current_fitness, agent1_data, param_chosen, outcome):
    """Persist Agent 1 trade verdicts + iteration outcome to logs/trade_verdicts/."""
    if not _session_timestamp:
        return
    try:
        VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
        ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename = f"verdicts_{_session_timestamp}_iter{iteration_num:02d}.json"
        record   = {
            "header": {
                "iteration":         iteration_num,
                "session_timestamp": _session_timestamp,
                "timestamp":         ts,
                "current_fitness":   current_fitness,
                "param_chosen":      param_chosen,
                "outcome":           outcome,
            },
            "agent1_data": agent1_data,
        }
        with open(VERDICTS_DIR / filename, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        print(f"  [VERDICTS] {filename}  outcome={outcome}", flush=True)

        top_signal = agent1_data.get("top_signal", "?")
        top_count  = agent1_data.get("top_signal_count", 0)
        index_line = (
            f"{ts}  iter={iteration_num:02d}  "
            f"top={top_signal}({top_count})  "
            f"chosen={param_chosen or 'none'}  "
            f"{outcome}  {filename}\n"
        )
        with open(VERDICTS_DIR / "index.txt", "a", encoding="utf-8") as f:
            f.write(index_line)
    except Exception as exc:
        print(f"  [VERDICTS] Write failed: {exc}")


def _is_repeat(param_path, new_value):
    """True if this (param, value) was already tested this session or is in history as reverted."""
    key = (param_path, str(new_value))
    return key in _tested_this_session or key in _known_reverted


def _repeat_delta(param_path, new_value):
    """Return the stored delta string for a known-reverted pair, or 'unknown'."""
    return _known_reverted.get((param_path, str(new_value)), "unknown")


# ============================================================================
#  JSON PARAM EDIT
# ============================================================================

def _set_nested(d, path_parts, value):
    """Set d[path_parts[0]][path_parts[1]]... = value in-place."""
    node = d
    for key in path_parts[:-1]:
        node = node[key]
    node[path_parts[-1]] = value


def apply_param_change(param_path, new_value):
    """
    Load strategy_params.json, set param_path (dot-separated) to new_value, save.
    Returns (old_value, ok: bool, error_msg: str).
    """
    try:
        with open(PARAMS_JSON) as f:
            params = json.load(f)
    except Exception as exc:
        return None, False, f"Cannot read strategy_params.json: {exc}"

    parts = param_path.split(".")
    # Read old value
    try:
        node = params
        for k in parts:
            node = node[k]
        old_value = node
    except (KeyError, TypeError) as exc:
        return None, False, f"Invalid path '{param_path}': {exc}"

    # Apply new value (coerce type to match old)
    try:
        if isinstance(old_value, bool):
            typed_new = bool(new_value)
        elif isinstance(old_value, int) and not isinstance(old_value, bool):
            typed_new = int(new_value) if not isinstance(new_value, float) else new_value
        elif isinstance(old_value, float):
            typed_new = float(new_value)
        else:
            typed_new = new_value
        _set_nested(params, parts, typed_new)
    except Exception as exc:
        return old_value, False, f"Cannot apply value: {exc}"

    try:
        with open(PARAMS_JSON, "w") as f:
            json.dump(params, f, indent=2)
    except Exception as exc:
        return old_value, False, f"Cannot write strategy_params.json: {exc}"

    return old_value, True, "OK"


# ============================================================================
#  AGENT 1: TRADE ANALYST  (Haiku)
# ============================================================================

_ANALYST_SYSTEM = """\
You are a quantitative trade analyst. You receive a batch of closed trades from
a portfolio backtest. Each trade record includes entry/exit dates, prices, PnL, exit reason, gate scores
at entry, pre-entry price trend (30-day window), regime at entry, a forward price
path (entry + 3 months, pre-computed), and post-exit price path (up to 120 calendar
days after the sell).

Your task: for each trade produce a two-axis ENTRY verdict (decision quality ×
stock outcome → verdict_matrix) and a separate EXIT verdict. Aggregate signals into
entry_signals (gate parameters) and exit_signals (exit parameters).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXIT ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Classify post_exit_verdict:
  EXIT_WAS_PREMATURE  -- post-exit price rose >5% within 120 days
  EXIT_WAS_CORRECT    -- price stayed flat or fell after exit
  FLAT                -- inconclusive (<5% move either way)

Assign one exit_parameter_signal with valid paths from:
  exits.trailing_stop_pct, exits.trail_activate_gain_pct, exits.take_profit_pct,
  exits.below_ma_trend_floor, exits.ma_confirm_days, exits.ma_breakdown_pct,
  exits.min_hold_days, exits.max_hold_days, exits.gm_erosion_cyclical_thr,
  exits.gm_erosion_noncyc_thr

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENTRY ANALYSIS  (two-axis: decision quality × stock outcome)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Axis 1 — entry_decision_quality: judge using ONLY information visible at entry
(pass margin, pre-entry trend, regime). HINDSIGHT FORBIDDEN here.
  GOOD     -- uptrend or clean base, comfortable pass margin (≥+0.30), regime
              appropriate for the strategy
  MARGINAL -- some warning sign present: low pass margin, mild regime concern,
              flat or choppy pre-entry trend
  BAD      -- clear red flags: very low pass margin, strong regime mismatch,
              obvious pre-entry weakness or overextension

Axis 2 — stock_outcome: use the "Forward path from entry" line in the narrative.
Hindsight is allowed here -- this measures what the stock actually did.
  HAD_POTENTIAL -- Forward 3m change from entry > +15%
  WEAK          -- Forward 3m change from entry < -10%
  FLAT          -- everything else (-10% to +15%)

verdict_matrix — derived from the two axes plus pnl_pct:
  EXIT_DESTROYED_GOOD_PICK  -- GOOD + HAD_POTENTIAL + pnl_pct < 0
                               The entry was right, the stock had room, but the exit
                               fired too early. Exit-side signal. Cite the specific
                               exit mechanism. Do NOT generate entry_parameter_signal.
  UNLUCKY_PICK              -- GOOD + WEAK
                               Solid entry decision; stock was fundamentally weak.
                               No actionable lever. Do NOT generate entry_parameter_signal.
  GATE_LEAK                 -- (MARGINAL or BAD) + WEAK
                               Entry gate was too permissive and the stock confirmed it.
                               Entry-side signal. Generate entry_parameter_signal + entry_mistake.
  LUCKY_PASS                -- (MARGINAL or BAD) + HAD_POTENTIAL
                               Gate was lax but the stock recovered anyway. Note it;
                               no gate-change signal. Do NOT generate entry_parameter_signal.
  NEUTRAL                   -- all remaining cases (GOOD + HAD_POTENTIAL + pnl >= 0,
                               GOOD + FLAT, MARGINAL/BAD + FLAT, etc.)

Require entry_reasoning: 1-2 sentences citing the specific pre-entry trend numbers
and gate margins from the narrative.

entry_parameter_signal is REQUIRED only when verdict_matrix == "GATE_LEAK".
Valid entry signal paths:
  gates.gm_tops.solar_hw, gates.gm_tops.solar_install, gates.gm_tops.renewables,
  gates.gm_mids.solar_hw, gates.gm_mids.solar_install, gates.gm_mids.renewables,
  gates.gm_configs.cyber, gates.gm_configs.infra_saas, gates.gm_configs.fintech,
  gates.regime_adjustments.BULL_STRONG, gates.regime_adjustments.BULL_WEAK,
  gates.regime_adjustments.BEAR_GRIND, gates.regime_adjustments.BEAR_VOLATILE,
  gates.conviction_thresholds.high_margin, gates.conviction_thresholds.med_margin

If NO existing gate parameter could have blocked the entry, set path to
"NO_EXISTING_LEVER" with reasoning describing what structural filter WOULD have
caught it (e.g. "an entry-extension gate: price was 18% above MA20").
These accumulate evidence for designing future gates.

entry_mistake is REQUIRED only when verdict_matrix == "GATE_LEAK". Attempt to
identify the specific gate failure that let this entry through.

  "identified": true ONLY when the chain from a specific number to the outcome is
    unambiguous: a gate that barely passed on a stock that confirmed weakness;
    a regime threshold that clearly should have blocked the entry.

  "identified": false when gates passed comfortably, pre-entry trend looked fine,
    and the stock weakened regardless. Set what_happened to "no clear gate failure;
    loss appears to be market/stock-specific risk". Do NOT force a diagnosis.

  "evidence_strength": "CLEAR" when gate margin and outcome directly connect.
    "PARTIAL" when suggestive but other factors present. Never output a diagnosis
    rated below PARTIAL -- use identified: false instead.

  "gate_responsible": the path from the valid entry signal paths list, or
    "NO_EXISTING_LEVER" if the failure would require a new gate type.

  "param_path": the specific parameter within that gate.

KEY TEST: A losing trade with GOOD entry and HAD_POTENTIAL stock should be rare
and must have verdict_matrix=EXIT_DESTROYED_GOOD_PICK naming the specific exit.
A loser with GOOD entry and WEAK stock is UNLUCKY_PICK -- do not fabricate an
entry gate failure for market/stock-specific risk that no gate could address.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGGREGATION (separate entry and exit signal groups)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Confidence thresholds (applied independently to each group):
  HIGH   = parameter flagged in 5+ trades
  MEDIUM = flagged in 3-4 trades
  LOW    = flagged in 1-2 trades (omit from aggregated signals -- below threshold)

entry_signals: aggregate entry_parameter_signal.path values from GATE_LEAK trades
  only (3+ occurrences). Include a "NO_EXISTING_LEVER" group if that path appears
  3+ times.
  Include a "mistake_summary" top-level key: count of GATE_LEAK entries with
  identified: true grouped by gate_responsible, plus an "unidentified" count for
  all GATE_LEAK entries where identified: false.
  Include a "verdict_matrix_summary" top-level key: counts of each verdict_matrix
  value across ALL trades (EXIT_DESTROYED_GOOD_PICK, GATE_LEAK, LUCKY_PASS,
  UNLUCKY_PICK, NEUTRAL).

exit_signals: aggregate exit_parameter_signal.path values (3+ occurrences).

top_signal = whichever of top_entry_signal / top_exit_signal has the higher count.

Output ONLY valid JSON matching this exact schema (no text before or after):

{
  "trade_verdicts": [
    {
      "trade_id": 1,
      "ticker": "DDOG",
      "entry_date": "2023-10-04",
      "exit_date": "2023-10-12",
      "pnl_pct": -0.6,
      "exit_reason": "BELOW_MA_DECLINING",
      "post_exit_120d_change": 30.4,
      "post_exit_verdict": "EXIT_WAS_PREMATURE",
      "entry_decision_quality": "BAD",
      "stock_outcome": "HAD_POTENTIAL",
      "verdict_matrix": "LUCKY_PASS",
      "entry_reasoning": "NDX was BEAR_VOLATILE at entry; pre-entry 30d trend -4.1%; pass margin +0.01 [MARGINAL]; entry was lax on regime timing but stock rose +30% over 3 months from entry.",
      "flags": ["PREMATURE_EXIT", "LUCKY_PASS"],
      "exit_parameter_signal": {
        "path": "exits.below_ma_trend_floor",
        "direction": "raise",
        "current_value": 0.07,
        "suggested_value": 0.09,
        "confidence": "HIGH",
        "reasoning": "Exited on weak MA signal then stock rose +30.4% over 120 days; threshold too sensitive"
      }
    },
    {
      "trade_id": 2,
      "ticker": "QCOM",
      "entry_date": "2021-03-02",
      "exit_date": "2021-03-10",
      "pnl_pct": -6.2,
      "exit_reason": "BELOW_MA_DECLINING",
      "post_exit_120d_change": 0.8,
      "post_exit_verdict": "FLAT",
      "entry_decision_quality": "MARGINAL",
      "stock_outcome": "WEAK",
      "verdict_matrix": "GATE_LEAK",
      "entry_reasoning": "Conviction score 0.61 was marginal at threshold 0.60; GM score near threshold with weak sector GM; pre-entry trend flat 0.3% over 30 days suggesting no momentum.",
      "entry_parameter_signal": {
        "path": "gates.conviction_thresholds.high_margin",
        "direction": "raise",
        "current_value": 0.60,
        "suggested_value": 0.65,
        "confidence": "MEDIUM",
        "reasoning": "Raising HIGH conviction threshold would have blocked this marginal entry"
      },
      "entry_mistake": {
        "identified": true,
        "gate_responsible": "gates.conviction_thresholds.high_margin",
        "param_path": "gates.conviction_thresholds.high_margin",
        "what_happened": "Conviction 0.61 passed at threshold 0.60 with only 1pp margin. GM score was near threshold with weak sector GM. Flat pre-entry trend meant no momentum cushion; stock declined immediately.",
        "evidence_strength": "CLEAR"
      },
      "flags": ["CORRECT_EXIT"],
      "exit_parameter_signal": {
        "path": "exits.ma_confirm_days",
        "direction": "raise",
        "current_value": 4,
        "suggested_value": 5,
        "confidence": "LOW",
        "reasoning": "Very short hold but exit was appropriate given volatility; minimal impact expected"
      }
    }
  ],
  "entry_signals": {
    "gates.regime_adjustments.BEAR_VOLATILE": {
      "direction": "lower",
      "count": 8,
      "confidence": "HIGH",
      "supporting_trade_ids": [1, 3, 5, 8, 10, 12, 15, 18],
      "never_tried_before": true
    },
    "NO_EXISTING_LEVER": {
      "count": 4,
      "confidence": "MEDIUM",
      "reasoning_summary": "4 trades had no existing gate that would block entry; suggest entry-extension gate (price vs MA20 distance)",
      "supporting_trade_ids": [2, 7, 11, 19]
    },
    "mistake_summary": {
      "gates.regime_adjustments.BEAR_VOLATILE": 6,
      "gates.conviction_thresholds.high_margin": 3,
      "unidentified": 12
    },
    "verdict_matrix_summary": {
      "EXIT_DESTROYED_GOOD_PICK": 3,
      "GATE_LEAK": 8,
      "LUCKY_PASS": 2,
      "UNLUCKY_PICK": 4,
      "NEUTRAL": 13
    }
  },
  "exit_signals": {
    "exits.below_ma_trend_floor": {
      "direction": "raise",
      "count": 11,
      "confidence": "HIGH",
      "avg_post_exit_gain_on_premature": 18.4,
      "supporting_trade_ids": [1, 4, 7, 12],
      "never_tried_before": true
    }
  },
  "top_entry_signal": "gates.regime_adjustments.BEAR_VOLATILE",
  "top_entry_signal_count": 8,
  "top_entry_signal_confidence": "HIGH",
  "top_exit_signal": "exits.below_ma_trend_floor",
  "top_exit_signal_count": 11,
  "top_exit_signal_confidence": "HIGH",
  "top_signal": "exits.below_ma_trend_floor",
  "top_signal_count": 11,
  "top_signal_confidence": "HIGH"
}

Rules:
- Every trade_verdict must have entry_decision_quality, stock_outcome, verdict_matrix, entry_reasoning, and exit_parameter_signal
- entry_parameter_signal is required ONLY when verdict_matrix == "GATE_LEAK"; omit for EXIT_DESTROYED_GOOD_PICK, UNLUCKY_PICK, LUCKY_PASS, NEUTRAL
- entry_mistake is required ONLY when verdict_matrix == "GATE_LEAK"; omit for all other verdict_matrix values
- entry_mistake.identified must be true ONLY when the gate margin + outcome chain is unambiguous in the provided data; use false otherwise
- entry_signals aggregates entry_parameter_signal.path from GATE_LEAK trades (3+ occurrences); include NO_EXISTING_LEVER group if 3+ trades have that path
- entry_signals.mistake_summary counts identified:true GATE_LEAK entries grouped by gate_responsible plus "unidentified" count
- entry_signals.verdict_matrix_summary counts each verdict_matrix value across ALL trades
- exit_signals aggregates exit_parameter_signal.path (3+ occurrences)
- never_tried_before is always set to true (Agent 2 will verify against history)
- top_signal = whichever of top_entry_signal / top_exit_signal has the higher count
- Use only valid paths from the lists above for each signal type
- Output ONLY the JSON object, nothing else"""


def _strip_fences(text):
    """Strip markdown code fences from model output."""
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        s = "\n".join(inner)
    return s


def _call_anthropic(model, max_tokens, system, user):
    """Stream a single messages request; return (text, input_tokens, output_tokens)."""
    client = _anthropic.Anthropic()
    text_parts = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for chunk in stream.text_stream:
            text_parts.append(chunk)
        final = stream.get_final_message()
    text = "".join(text_parts)
    return text, final.usage.input_tokens, final.usage.output_tokens


def run_trade_analyst(trades_json_path):
    """
    Run Agent 1 (Trade Analyst, Haiku).
    Returns (verdicts_text: str, raw_response: str).
    """
    print("\n  [AGENT 1 - Trade Analyst / Haiku]", flush=True)

    with open(trades_json_path) as f:
        sampled = json.load(f)

    # Build the trade batch text (narratives only, not raw JSON)
    narratives = "\n\n" + ("=" * 72) + "\n\n"
    narratives = narratives.join(
        t.get("narrative", f"Trade #{t['trade_id']} -- no narrative") +
        f"\n  [gate_margins_json: {json.dumps(t.get('gate_margins', {}))}]"
        for t in sampled["trades"]
    )

    with open(PARAMS_JSON) as _pf:
        _live = json.load(_pf)
    _regime_adj = _live["gates"]["regime_adjustments"]
    _conv_thr   = _live["gates"]["conviction_thresholds"]
    gate_settings = (
        f"CURRENT GATE SETTINGS (live from strategy_params.json):\n"
        f"  regime_adjustments   : {json.dumps(_regime_adj)}\n"
        f"  conviction_thresholds: {json.dumps(_conv_thr)}\n"
        f"\n"
        f"  Base pass thresholds by universe:\n"
        f"    semi=5.0   tech=6.3   medtech=5.7   energy=5.5   default=5.5\n"
        f"\n"
        f"  Counter-cyclical universes (regime adjustment NOT applied): semi\n"
        f"    semi always uses base threshold 5.0 regardless of regime.\n"
        f"\n"
        f"  For all other universes: effective_threshold = base + regime_adjustment\n"
        f"  regime_adjustments are ADDED to the pass threshold -- higher = stricter.\n"
        f"  To make a regime's entries harder, RAISE its adjustment.\n"
        f"\n"
        f"  conviction_thresholds: score must beat threshold by high_margin "
        f"({_conv_thr['high_margin']}) for HIGH confidence, "
        f"med_margin ({_conv_thr['med_margin']}) for MED confidence.\n"
        f"\n"
        f"  IMPORTANT: NEVER compute or derive a threshold yourself from the tables above.\n"
        f"  Every trade narrative already states its own 'Effective threshold' and\n"
        f"  'Pass margin' lines -- use ONLY those numbers for threshold analysis.\n"
        f"  A margin below +0.30 is marginal; above +1.00 is comfortable.\n"
        f"  If a narrative shows rescue_bonus != 0, the raw gate_margins sum differs\n"
        f"  from the displayed score -- use the score (not gate_margins) vs threshold.\n\n"
    )

    user_text = (
        gate_settings
        + f"Total closed trades in report: {sampled['n_total_trades']}\n"
        f"Sampled: {sampled['n_sampled']} trades\n"
        f"Report generated: {sampled['generated_at']}\n\n"
        f"TRADE BATCH:\n\n{narratives}"
    )

    try:
        text, in_tok, out_tok = _call_anthropic(
            MODEL_ANALYST, 32768, _ANALYST_SYSTEM, user_text
        )
        _tally_cost(MODEL_ANALYST, in_tok, out_tok)
    except Exception as exc:
        print(f"  [AGENT 1] API ERROR: {exc}")
        return "", str(exc)

    text = _strip_fences(text)
    safe = text.encode("ascii", errors="replace").decode("ascii")
    print(f"\n  [AGENT 1 RESPONSE]\n{safe}\n", flush=True)
    return text, text


# ============================================================================
#  AGENT 3: HISTORIAN  (Haiku)
# ============================================================================

_HISTORIAN_SYSTEM = """\
You are a strategy optimization historian. You receive the complete history of
every parameter change ever tested on this strategy -- both kept improvements and
reverted failures. Your job is to produce a structured summary that tells the
optimizer what it needs to know to make good decisions.

Analyze the full history and output a single JSON object with this exact structure:

{
  "best_fitness_ever": 63.33,
  "current_best_fitness": 63.01,
  "fitness_gap_to_recover": 0.32,
  "total_iterations_run": 47,
  "total_kept": 8,
  "total_reverted": 39,

  "parameter_history": {
    "exits.below_ma_trend_floor": {
      "best_value_found": 0.085,
      "best_fitness_delta": 3.24,
      "attempts": [
        {"value": 0.07, "delta": 3.24, "verdict": "KEPT", "environment": "UNVERIFIED_ENVIRONMENT"},
        {"value": 0.08, "delta": -2.35, "verdict": "REVERTED", "environment": "UNVERIFIED_ENVIRONMENT"},
        {"value": 0.085, "delta": 1.89, "verdict": "KEPT"},
        {"value": 0.10, "delta": -12.63, "verdict": "REVERTED"},
        {"value": 0.03, "delta": -3.19, "verdict": "REVERTED", "environment": "UNVERIFIED_ENVIRONMENT"}
      ],
      "direction_exhausted": false,
      "next_untested_values": [0.09, 0.095],
      "recommendation": "EXPLORE_CAUTIOUSLY -- tried 0.085 (kept) and 0.10 (crashed). Gap between 0.085 and 0.10 has untested values."
    }
  },

  "family_status": {
    "MA_exit_sensitivity": {
      "tested_members": ["exits.below_ma_trend_floor"],
      "untested_members": ["exits.ma_confirm_days", "exits.ma_breakdown_pct", "exits.min_hold_days", "exits.ma100_breakdown_days"],
      "best_delta": 3.24,
      "status": "PARTIALLY_EXPLORED",
      "recommendation": "Try ma_confirm_days next -- related to below_ma_trend_floor which showed strong gains"
    },
    "position_sizing": {
      "tested_members": [],
      "untested_members": ["sizing.per_buy_fraction", "sizing.conviction_mult.HIGH", "sizing.regime_position_mult.BEAR_VOLATILE"],
      "best_delta": null,
      "status": "UNEXPLORED",
      "recommendation": "No attempts yet -- high priority, start with per_buy_fraction"
    }
  },

  "unexplored_parameters": [
    "sizing.per_buy_fraction",
    "sizing.regime_position_mult.BEAR_VOLATILE",
    "gates.regime_adjustments.BULL_WEAK",
    "exits.take_profit_pct"
  ],

  "patterns_observed": [
    "Solar hardware GM gate changes (gm_tops.solar_hw) improved fitness when tightened"
  ],

  "recovery_path": "One sentence on how to recover fitness if below best-ever"
}

Rules:
- best_fitness_ever: use the CONFIRMED BEST FITNESS value given in the user message -- do NOT compute from history entries
- current_best_fitness: fitness_after of the most recent KEPT entry (or fitness_before of first entry if no KEPTs)
- Include EVERY parameter ever tested in parameter_history with ALL attempts
- For each attempt, if the entry's ts is before "2026-07-01", add "environment": "UNVERIFIED_ENVIRONMENT" to that attempt object; these deltas are unreliable (measured against a corrupted baseline before a backup/restore bug was fixed)
- direction_exhausted = true ONLY when both higher AND lower values have been tried and reverted by VERIFIED entries (ts >= "2026-07-01"); UNVERIFIED_ENVIRONMENT attempts must never be the sole basis for marking a direction exhausted
- next_untested_values: up to 3 values between best-kept and nearest-reverted boundary (empty list if direction_exhausted)
- unexplored_parameters: actionable paths never tested at all -- choose from exits.*, sizing.per_buy_fraction, sizing.conviction_mult.*, sizing.regime_position_mult.*, gates.gm_tops.*, gates.gm_configs.*, gates.regime_adjustments.*
- patterns_observed: 2-4 cross-parameter patterns, not single-parameter observations
- family_status: the user message includes PARAMETER_FAMILIES. For each family, compute:
  - tested_members: members whose paths appear as param_path in parameter_history
  - untested_members: members NOT in parameter_history
  - best_delta: max (fitness_after - fitness_before) across KEPT attempts for any tested member; null if UNEXPLORED
  - status: "UNEXPLORED" if tested_members is empty, "PARTIALLY_EXPLORED" if some tested, "FULLY_EXPLORED" if all tested
  - recommendation: one sentence on highest-value next action for this family
- Output ONLY the JSON object, no prose before or after"""


def run_historian(params_history, best_ever=None):
    """
    Run Agent 3 (Historian, Haiku). Called once per session before iteration 1.
    best_ever: dynamically computed max(BEST_FITNESS_EVER_OVERRIDE, current_best_fitness).
    Sets _historian_summary global and returns it.
    """
    global _historian_summary
    _best_ever = best_ever if best_ever is not None else BEST_FITNESS_EVER_OVERRIDE
    print("\n  [AGENT 3 - Historian / Haiku]", flush=True)
    print(f"  [AGENT 3] best_ever={_best_ever:.4f} (passed from session start)", flush=True)

    if not params_history:
        print("  [AGENT 3] No history yet -- using empty summary")
        _historian_summary = {
            "best_fitness_ever": _best_ever,
            "current_best_fitness": _best_ever,
            "fitness_gap_to_recover": 0.0,
            "total_iterations_run": 0,
            "total_kept": 0,
            "total_reverted": 0,
            "parameter_history": {},
            "unexplored_parameters": [
                "sizing.per_buy_fraction",
                "exits.take_profit_pct",
                "sizing.conviction_mult.HIGH",
                "gates.regime_adjustments.BULL_STRONG",
                "exits.trail_activate_gain_pct",
            ],
            "patterns_observed": ["No history yet -- all parameters unexplored"],
            "recovery_path": "No prior testing -- start with conservative first steps on any unexplored parameter",
        }
        return _historian_summary

    user_text = (
        f"PARAMETER FAMILIES (use these to compute family_status in your output):\n"
        + json.dumps(PARAMETER_FAMILIES, indent=2) + "\n\n"
        + f"CONFIRMED BEST FITNESS: {_best_ever:.4f}\n"
        f"This is the highest fitness confirmed reproducible on the current simulator "
        f"(computed as max of the floor constant and the current session's baseline). "
        f"Use {_best_ever:.4f} as best_fitness_ever in your output -- do NOT derive it "
        f"from the history entries below. "
        f"fitness_gap_to_recover = {_best_ever:.4f} - current_best_fitness.\n\n"
        f"DATA HYGIENE -- UNVERIFIED HISTORY ENTRIES:\n"
        f"History entries with ts before '2026-07-01' were recorded before a backup/restore "
        f"bug was fixed on that date. The bug could cause a parameter change to be measured "
        f"against a corrupted or inconsistent baseline, making those fitness deltas unreliable. "
        f"For each attempt in parameter_history, add \"environment\": \"UNVERIFIED_ENVIRONMENT\" "
        f"if the entry's ts is before '2026-07-01'. "
        f"CRITICAL: direction_exhausted must NEVER be set to true based solely on "
        f"UNVERIFIED_ENVIRONMENT attempts. Only post-fix entries (ts >= '2026-07-01') "
        f"define hard direction boundaries.\n\n"
        f"Complete parameter change history ({len(params_history)} entries):\n\n"
        + json.dumps(params_history, indent=2)
    )

    try:
        text, in_tok, out_tok = _call_anthropic(
            MODEL_HISTORIAN, 32768, _HISTORIAN_SYSTEM, user_text
        )
        _tally_cost(MODEL_HISTORIAN, in_tok, out_tok)
    except Exception as exc:
        print(f"  [AGENT 3] API ERROR: {exc}")
        _historian_summary = {"error": str(exc)}
        return _historian_summary

    text = _strip_fences(text)
    safe = text.encode("ascii", errors="replace").decode("ascii")
    print(f"\n  [AGENT 3 RESPONSE]\n{safe}\n", flush=True)

    try:
        _historian_summary = json.loads(text)
        print(f"  [AGENT 3] best_fitness_ever={_historian_summary.get('best_fitness_ever')}, "
              f"total_kept={_historian_summary.get('total_kept')}, "
              f"unexplored={len(_historian_summary.get('unexplored_parameters', []))}")
    except json.JSONDecodeError as e:
        print(f"  [AGENT 3] JSON parse failed: {e} -- using raw text fallback")
        _historian_summary = {"raw": text, "parse_error": str(e)}

    return _historian_summary


# ============================================================================
#  AGENT 2: PARAMETER OPTIMIZER  (Opus)
# ============================================================================

_OPTIMIZER_SYSTEM = """\
You are a strategy parameter optimizer. You receive:
  1. Agent 1's structured JSON signals from this iteration's trade analysis
  2. The Historian's structured summary of all prior testing across all sessions
  3. The current strategy_params.json contents
  4. Current and best-ever fitness values

Your task: propose ONE specific, measurable change to strategy_params.json that
is most likely to improve portfolio fitness (fitness = total_return_pct*0.5 + sharpe*20*0.5).

CRITICAL -- G2 GROSS MARGIN GATE HIERARCHY (two-level, must understand before changing any gm_ param):
  gates.gm_tops.{sector}  : HIGH-quality threshold. GM >= this -> full score.
  gates.gm_mids.{sector}  : MID-quality threshold. GM in [gm_mids, gm_tops) -> partial score (0.7x).
                            GM < gm_mids -> G2 FAILS entirely.

  To TIGHTEN G2 (reject more marginal entries):
    -> Raise gates.gm_tops.{sector}   -- pushes "barely passed at 0.7x" into FAIL territory. CORRECT.
    -> Do NOT raise gm_mids unless you intend to eliminate the mid-band for that sector entirely.

  KNOWN FAILURE: raising gm_mids.solar_hw 22->26 crashed fitness -11.2 points. DO NOT repeat.
  The correct change for "tighten G2 for solar hardware" is to raise gm_tops.solar_hw, not gm_mids.

DEAD PATHS (metadata only -- changes here have NO effect, do NOT propose these):
  gates.gate_weights (removed)
  gates.ps_ratio_max, gates.ps_growth_max, gates.momentum_min, gates.revenue_growth_min,
  gates.momentum_lookback_days, gates.sector_rule40_overrides, gates.sector_fcf_overrides,
  gates.sector_roic_overrides (never read by tester.py)
  gates.rule40_min, gates.fcf_margin_min, gates.roic_min, gates.dilution_max
  (shadowed -- tester.py uses hardcoded sector-specific values; JSON ignored)

The gate parameters in strategy_params.json are the ONLY tunable entry parameters.
tester.py contains sector-specific hardcoded thresholds that are NOT tunable through
this system -- never propose paths not in the actionable list.

ENTRY vs EXIT SIGNALS:
  Agent 1 now provides a two-axis entry verdict (entry_decision_quality × stock_outcome
  → verdict_matrix) and entry_signals / exit_signals as separate evidence classes.

  VERDICT MATRIX SUMMARY — read this first to understand the signal mix:
    EXIT_DESTROYED_GOOD_PICK: good entry + stock had potential + trade lost → exit problem
      These counts feed exit_signals. A high count here means exit is the main issue.
    GATE_LEAK: bad/marginal entry + weak stock → gate was too permissive
      Only GATE_LEAK trades generate entry_parameter_signals. Use these to justify gate changes.
    UNLUCKY_PICK: good entry + weak stock → no lever exists; stock was just bad
      Do NOT respond to UNLUCKY_PICK with gate tightening -- you would block good entries.
    LUCKY_PASS: bad entry + stock had potential → gate was lax but stock recovered
      Interesting for future structural work but NOT an immediate tuning signal.
    NEUTRAL: winning trades and flat-stock outcomes -- not actionable.

  Rule: ONLY GATE_LEAK counts justify gate parameter changes. If verdict_matrix_summary
  shows GATE_LEAK < 3 but UNLUCKY_PICK is high, do NOT tighten gates -- use exit signals.

MISTAKE SUMMARY (from entry_signals.mistake_summary):
  Counts GATE_LEAK entries where Agent 1 identified a specific gate failure with
  CLEAR evidence, grouped by gate_responsible. Use it to weight tuning targets:
  - A gate with multiple CLEAR-evidence mistakes is a stronger tuning target than one
    with high signal counts but only PARTIAL evidence.
  - A high "unidentified" count means GATE_LEAK losses lack a clear addressable gate.
    Do NOT respond to unidentified losses by tightening arbitrary gates -- this
    sacrifices good entries without fixing the underlying cause.

Rules:
  - Check Historian summary FIRST: if direction_exhausted=true for a parameter, skip it entirely.
  - Prefer unexplored_parameters and parameters with non-empty next_untested_values.
  - For parameters with next_untested_values, try the value closest to best-kept that is untested.
  - For unexplored_parameters, propose a conservative first step (5-10% change from current value).
  - Never propose an exact match to a prior REVERTED attempt.
  - Propose only ONE parameter path change per iteration.
  - Prefer conservative adjustments (10-20% change from current value).

Output ONLY this JSON (no other text):
{
  "param_path": "exits.trailing_stop_pct",
  "old_value": 0.145,
  "new_value": 0.165,
  "reason": "one sentence citing specific trade evidence from Agent 1 signals",
  "predicted_impact": "one sentence on expected fitness impact"
}"""


def run_param_optimizer(agent1_data, current_params, current_best_fitness,
                        session_blacklist=None, rejection_msg=None):
    """
    Run Agent 2 (Parameter Optimizer, Opus).
    session_blacklist: set of (param_path, new_value) tuples already tried-and-reverted
                       this session; injected into the prompt as FORBIDDEN entries.
    Returns (param_path: str, new_value, rationale: str, raw_response: str).
    """
    print("\n  [AGENT 2 - Parameter Optimizer / Opus]", flush=True)

    # Support new two-group format; fall back gracefully if Agent 1 returned old format
    if "entry_signals" in agent1_data or "exit_signals" in agent1_data:
        entry_signals     = agent1_data.get("entry_signals", {})
        exit_signals      = agent1_data.get("exit_signals", {})
    else:
        old_sigs      = agent1_data.get("parameter_signals", {})
        entry_signals = {k: v for k, v in old_sigs.items() if k.startswith("gates.")}
        exit_signals  = {k: v for k, v in old_sigs.items() if not k.startswith("gates.")}
    top_entry_signal  = agent1_data.get("top_entry_signal", "")
    top_entry_count   = agent1_data.get("top_entry_signal_count", 0)
    top_exit_signal   = agent1_data.get("top_exit_signal", "")
    top_exit_count    = agent1_data.get("top_exit_signal_count", 0)
    top_signal        = agent1_data.get("top_signal", top_exit_signal or top_entry_signal)
    top_count         = agent1_data.get("top_signal_count", max(top_entry_count, top_exit_count))
    mistake_summary   = (entry_signals or {}).get("mistake_summary", {})
    matrix_summary    = (entry_signals or {}).get("verdict_matrix_summary", {})

    best_fitness_ever = (
        _historian_summary.get("best_fitness_ever", current_best_fitness)
        if _historian_summary else current_best_fitness
    )
    historian_json   = json.dumps(_historian_summary, indent=2) if _historian_summary else "{}"
    family_status    = _historian_summary.get("family_status", {}) if _historian_summary else {}
    family_status_json = json.dumps(family_status, indent=2)

    # Inject session blacklist so Agent 2 never re-proposes an already-reverted move
    if session_blacklist:
        forbidden_lines = "\n".join(
            f"  - {p}: {v}  (REVERTED this session -- do NOT propose again)"
            for p, v in sorted(session_blacklist)
        )
        blacklist_block = (
            f"\nSESSION BLACKLIST (these param+value pairs were ALREADY TESTED AND REVERTED "
            f"this session -- proposing them again wastes a backtest run and is FORBIDDEN):\n"
            f"{forbidden_lines}\n"
        )
    else:
        blacklist_block = ""

    rejection_prefix = (
        f"IMPORTANT -- PREVIOUS PROPOSAL REJECTED:\n{rejection_msg}\n\n"
        if rejection_msg else ""
    )

    user_text = f"""{rejection_prefix}VERDICT MATRIX SUMMARY (distribution of trade types -- read before acting on entry signals):
{json.dumps(matrix_summary, indent=2)}

AGENT 1 ENTRY SIGNALS (from GATE_LEAK trades only -- justify gate changes with these):
{json.dumps(entry_signals, indent=2)}

MISTAKE SUMMARY (CLEAR-evidence GATE_LEAK failures, grouped by gate_responsible):
{json.dumps(mistake_summary, indent=2)}

TOP ENTRY SIGNAL: {top_entry_signal} ({top_entry_count}/30 trades)

AGENT 1 EXIT SIGNALS (exit calibration problems):
{json.dumps(exit_signals, indent=2)}

TOP EXIT SIGNAL: {top_exit_signal} ({top_exit_count}/30 trades)

OVERALL TOP SIGNAL: {top_signal} ({top_count}/30 trades)

HISTORIAN SUMMARY (complete history of all parameters ever tested):
{historian_json}

PARAMETER FAMILIES (groups of related parameters to explore together):
{json.dumps(PARAMETER_FAMILIES, indent=2)}

FAMILY STATUS (from Historian -- which families are unexplored vs partially explored):
{family_status_json}

CURRENT STRATEGY_PARAMS (the live scratchpad -- will be reverted to current_best if change fails):
{json.dumps(current_params, indent=2)}

CURRENT BEST FITNESS: {current_best_fitness:.4f}
BEST FITNESS EVER: {best_fitness_ever:.4f}
{blacklist_block}

ACTIONABLE PARAMETER PATHS (changes here WILL affect results):
exits.trailing_stop_pct, exits.trail_activate_gain_pct, exits.take_profit_pct,
exits.below_ma_trend_floor, exits.ma_confirm_days, exits.ma_breakdown_pct,
exits.min_hold_days, exits.max_hold_days, exits.gm_erosion_cyclical_thr,
exits.gm_erosion_noncyc_thr, gates.gm_tops.solar_hw, gates.gm_tops.solar_install,
gates.gm_tops.renewables, gates.gm_mids.solar_hw, gates.gm_mids.solar_install,
gates.gm_mids.renewables, gates.gm_configs.cyber, gates.gm_configs.infra_saas,
gates.gm_configs.fintech, gates.regime_adjustments.BULL_STRONG,
gates.regime_adjustments.BULL_WEAK, gates.regime_adjustments.BEAR_GRIND,
gates.regime_adjustments.BEAR_VOLATILE, sizing.per_buy_fraction,
sizing.conviction_mult.HIGH, sizing.conviction_mult.MED, sizing.conviction_mult.LOW,
sizing.regime_position_mult.BULL_STRONG, sizing.regime_position_mult.BULL_WEAK,
sizing.regime_position_mult.BEAR_GRIND, sizing.regime_position_mult.BEAR_VOLATILE,
sizing.regime_max_positions.BULL_STRONG, sizing.regime_max_positions.BULL_WEAK,
sizing.regime_max_positions.BEAR_GRIND, sizing.regime_max_positions.BEAR_VOLATILE,
sizing.regime_exposure_cap.BULL_STRONG, sizing.regime_exposure_cap.BULL_WEAK,
sizing.regime_exposure_cap.BEAR_GRIND, sizing.regime_exposure_cap.BEAR_VOLATILE,
gates.conviction_thresholds.high_margin, gates.conviction_thresholds.med_margin

DEAD PATHS (do NOT propose):
gates.gate_weights, gates.ps_ratio_max, gates.ps_growth_max, gates.rule40_min,
gates.fcf_margin_min, gates.roic_min, gates.dilution_max, gates.momentum_min,
gates.revenue_growth_min, gates.momentum_lookback_days, gates.sector_rule40_overrides,
gates.sector_fcf_overrides, gates.sector_roic_overrides

DECISION RULES:
1. Check HISTORIAN SUMMARY first -- if direction_exhausted=true for a parameter, skip it entirely
2. Check FAMILY STATUS: UNEXPLORED families have the most upside -- prioritize them over PARTIALLY_EXPLORED ones
3. When selecting from an UNEXPLORED family, pick the member most directly supported by Agent 1 signals; if no signal overlap, use the family description's suggested starting point
4. Prefer unexplored_parameters and next_untested_values from the Historian
5. Check Agent 1 signals for evidence supporting the chosen parameter
6. For parameters with next_untested_values, try the value closest to best-kept that is untested
7. For unexplored_parameters, propose a conservative first step (5-10% change from current value)
8. Never propose an exact match to a prior REVERTED attempt (check Historian attempts list)
9. Output ONLY this JSON, nothing else:
{{
  "param_path": "exits.below_ma_trend_floor",
  "old_value": 0.07,
  "new_value": 0.09,
  "reason": "...",
  "predicted_impact": "..."
}}
"""

    try:
        text, in_tok, out_tok = _call_anthropic(
            MODEL_OPTIMIZER, 512, _OPTIMIZER_SYSTEM, user_text
        )
        _tally_cost(MODEL_OPTIMIZER, in_tok, out_tok)
    except Exception as exc:
        print(f"  [AGENT 2] API ERROR: {exc}")
        return None, None, str(exc), str(exc)

    safe = text.encode("ascii", errors="replace").decode("ascii")
    print(f"\n  [AGENT 2 RESPONSE]\n{safe}\n", flush=True)

    # Parse JSON output -- strip fences first, then extract first {...} block
    # so that prose preambles like "Note: ..." before the JSON don't crash the parser.
    def _extract_json_obj(s):
        """Return the first top-level {...} substring found in s, or s itself."""
        import re as _re
        m = _re.search(r'\{.*\}', s, _re.DOTALL)
        return m.group(0) if m else s

    try:
        agent2_data = json.loads(_extract_json_obj(_strip_fences(text)))
        param_path  = agent2_data["param_path"]
        new_value   = agent2_data["new_value"]
        rationale   = agent2_data.get("reason", "")
        print(f"  [AGENT 2] Chosen: {param_path}: {agent2_data.get('old_value')} -> {new_value}")
        print(f"  [AGENT 2] Reason: {rationale}")
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [AGENT 2] Parse failed: {e} -- skipping iteration")
        return None, None, "parse error", text

    return param_path, new_value, rationale, text


# ============================================================================
#  REPORT READER
# ============================================================================

def read_report():
    """Load portfolio_report.json and return the summary dict."""
    try:
        with open(REPORT_JSON) as f:
            data = json.load(f)
        return data.get("summary", {})
    except Exception as exc:
        print(f"  [REPORT] Read failed: {exc}")
        return {}


def read_params():
    with open(PARAMS_JSON) as f:
        return json.load(f)


# ============================================================================
#  MAIN LOOP
# ============================================================================

def baseline_run():
    """Run simulator once, return (summary, fitness)."""
    ok, err = run_simulator()
    if not ok:
        print(f"  [BASELINE] Simulation failed: {err}")
        sys.exit(1)
    summary = read_report()
    fitness = compute_fitness(summary)
    print(f"\n  [BASELINE] fitness={fitness:.4f}  "
          f"return={summary.get('total_return_pct',0):+.2f}%  "
          f"sharpe={summary.get('sharpe',0):.3f}  "
          f"n_closed={summary.get('n_closed',0)}")
    return summary, fitness


def run_iteration(iteration_num, baseline_n_closed, current_fitness, budget_remaining,
                  kept_before=0, current_best_fitness=0.0, session_blacklist=None,
                  analysis_report=None):
    """
    Run one full optimization iteration.
    kept_before: number of changes kept so far (used to trigger walk-forward check).
    session_blacklist: set of (param_path, new_value) tuples reverted this session.
    Returns (new_fitness: float, kept: bool, param_path: str).
    """
    global _current_best_fitness

    print(f"\n{'='*72}")
    print(f"  ITERATION {iteration_num}  |  current_fitness={current_fitness:.4f}  "
          f"  budget_left=${budget_remaining:.2f}")
    print(f"{'='*72}\n", flush=True)

    # Historian top signal for history entry annotation
    historian_top = "unknown"
    if _historian_summary:
        unexplored = _historian_summary.get("unexplored_parameters", [])
        historian_top = unexplored[0] if unexplored else "none_unexplored"

    # Step 1: Refresh trade sample (re-run prep)
    if not SAMPLED_TRADES.exists() or iteration_num == 1:
        print("  [PREP] Refreshing sampled_trades.json ...")
        if not run_prep(analysis_report=analysis_report):
            print("  [PREP] FAILED -- skipping iteration")
            return current_fitness, False, ""

    # Step 2: Agent 1 -- trade analysis
    analyst_text, _ = run_trade_analyst(SAMPLED_TRADES)
    if not analyst_text:
        print("  [ITER] Agent 1 returned no output -- skipping")
        return current_fitness, False, ""

    # Parse Agent 1 structured JSON output
    try:
        agent1_data      = json.loads(analyst_text)
        top_signal       = agent1_data.get("top_signal", "unknown")
        top_count        = agent1_data.get("top_signal_count", 0)
        top_entry        = agent1_data.get("top_entry_signal", "")
        top_entry_cnt    = agent1_data.get("top_entry_signal_count", 0)
        top_exit         = agent1_data.get("top_exit_signal", "")
        top_exit_cnt     = agent1_data.get("top_exit_signal_count", 0)
        print(f"  [AGENT 1] Top signal: {top_signal} ({top_count}/30 trades)")
        print(f"  [AGENT 1] Entry: {top_entry}({top_entry_cnt})  Exit: {top_exit}({top_exit_cnt})")
        entry_sigs = agent1_data.get("entry_signals", {})
        exit_sigs  = agent1_data.get("exit_signals", agent1_data.get("parameter_signals", {}))
        all_sigs   = {**entry_sigs, **exit_sigs}
        if all_sigs:
            sigs = ", ".join(
                k + ":" + str(v.get("count", 0))
                for k, v in sorted(all_sigs.items(), key=lambda x: -x[1].get("count", 0))
            )
            print(f"  [AGENT 1] All signals: {sigs}")
    except json.JSONDecodeError as e:
        print(f"  [AGENT 1] JSON parse failed: {e}")
        print(f"  [AGENT 1] Raw response: {analyst_text[:500]}")
        return current_fitness, False, ""

    # Step 3: Agent 2 -- propose one parameter change
    current_params = read_params()
    param_path, new_value, rationale, raw_resp = run_param_optimizer(
        agent1_data, current_params, current_best_fitness,
        session_blacklist=session_blacklist,
    )
    if param_path is None:
        print("  [ITER] Agent 2 returned no valid proposal -- skipping")
        _save_verdict(iteration_num, current_fitness, agent1_data, None, "A2_FAILED")
        return current_fitness, False, ""

    # Code-level repeat blocker: enforce before any simulator run
    if _is_repeat(param_path, new_value):
        known_delta = _repeat_delta(param_path, new_value)
        reject_msg = (
            f"REJECTED: {param_path}={new_value} was already tested and reverted "
            f"(delta={known_delta}). You MUST choose a different parameter or value. "
            f"Unexplored family regime_exposure_caps has four members never tested: "
            f"sizing.regime_exposure_cap.BULL_STRONG/BULL_WEAK/BEAR_GRIND/BEAR_VOLATILE."
        )
        print(f"  [BLOCKER] {reject_msg}", flush=True)
        _append_change_log(
            f"BLOCKED_REPEAT  iter={iteration_num}  param={param_path}  value={new_value}"
        )
        # Re-prompt Agent 2 once
        current_params = read_params()
        param_path, new_value, rationale, _ = run_param_optimizer(
            agent1_data, current_params, current_best_fitness,
            session_blacklist=_tested_this_session,
            rejection_msg=reject_msg,
        )
        if param_path is None:
            _save_verdict(iteration_num, current_fitness, agent1_data, None, "A2_REPEAT_BLOCKED")
            return current_fitness, False, ""
        if _is_repeat(param_path, new_value):
            print(
                f"  [BLOCKER] 2nd proposal {param_path}={new_value} also blocked -- skipping iteration",
                flush=True,
            )
            _append_change_log(
                f"BLOCKED_REPEAT  iter={iteration_num}  param={param_path}  value={new_value}"
                f"  (2nd attempt also blocked)"
            )
            _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "A2_REPEAT_BLOCKED")
            return current_fitness, False, ""

    # Step 4: Apply the change
    # strategy_params.json is the scratchpad; current_best_params.json is the ground truth.
    # No per-iteration backup needed -- restore always reads from current_best_params.json.
    old_value, ok, err_msg = apply_param_change(param_path, new_value)
    if not ok:
        print(f"  [ITER] apply_param_change failed: {err_msg}")
        restore_to_best()
        _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "APPLY_FAILED")
        return current_fitness, False, param_path

    _tested_this_session.add((param_path, str(new_value)))  # mark before sim so it's blocked even if sim crashes

    print(f"\n  [CHANGE] {param_path}: {old_value} -> {new_value}")
    print(f"  [RATIONALE] {rationale}")

    # Step 5: Run simulator with new params
    ok, err = run_simulator()
    if not ok:
        print(f"  [ITER] Sim failed after change: {err} -- reverting")
        restore_to_best()
        _append_change_log(
            f"ROLLBACK  iter={iteration_num}  {param_path}: {old_value}->{new_value}  "
            f"reason=sim_error"
        )
        append_params_history({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iter": iteration_num,
            "kept": False,
            "param_path": param_path,
            "old_value": old_value,
            "new_value": new_value,
            "fitness_before": current_fitness,
            "fitness_after": None,
            "reason": "sim_error",
            "rationale": rationale,
            "session_best_fitness_before": current_best_fitness,
            "session_best_fitness_after":  current_best_fitness,
            "historian_top_signal": historian_top,
        })
        _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "SIM_ERROR")
        return current_fitness, False, param_path

    new_summary = read_report()
    new_fitness = compute_fitness(new_summary)
    delta       = new_fitness - current_fitness

    print(f"\n  [RESULT] fitness {current_fitness:.4f} -> {new_fitness:.4f}  "
          f"delta={delta:+.4f}")

    # Step 6: Guardrail check
    guardrails_ok, guardrail_reason = check_guardrails(new_summary, baseline_n_closed)
    if not guardrails_ok:
        print(f"  [GUARDRAIL] FAILED: {guardrail_reason} -- reverting")
        restore_to_best()
        _append_change_log(
            f"ROLLBACK  iter={iteration_num}  {param_path}: {old_value}->{new_value}  "
            f"reason=guardrail: {guardrail_reason}"
        )
        append_params_history({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iter": iteration_num,
            "kept": False,
            "param_path": param_path,
            "old_value": old_value,
            "new_value": new_value,
            "fitness_before": current_fitness,
            "fitness_after": new_fitness,
            "reason": f"guardrail: {guardrail_reason}",
            "rationale": rationale,
            "session_best_fitness_before": current_best_fitness,
            "session_best_fitness_after":  current_best_fitness,
            "historian_top_signal": historian_top,
        })
        _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "REVERTED")
        return current_fitness, False, param_path

    # Step 7: Fitness check -- keep if improved
    if new_fitness > current_fitness:
        print(f"  [KEPT] fitness improved by {delta:+.4f}")

        # Walk-forward check every Nth kept change
        new_kept_count = kept_before + 1
        wf_spread = wf_verdict = None
        if new_kept_count % WALK_FORWARD_CHECK_EVERY == 0:
            prev_spread = _walk_forward_cache["spread"] if _walk_forward_cache else None
            wf_info    = run_walk_forward_check(iteration_num)
            wf_spread  = wf_info["spread"]
            wf_verdict = wf_info["verdict"]

            # Revert if spread regressed beyond tolerance vs previous WF check
            if prev_spread is not None and (wf_spread - prev_spread) > WALK_FORWARD_SPREAD_TOLERANCE:
                print(f"  [WF-REVERT] spread {prev_spread:.2f} -> {wf_spread:.2f} "
                      f"(+{wf_spread - prev_spread:.2f} > tolerance {WALK_FORWARD_SPREAD_TOLERANCE}) "
                      f"-- reverting kept change")
                restore_to_best()
                _append_change_log(
                    f"REVERTED_WF_REGRESSION  iter={iteration_num}  "
                    f"{param_path}: {old_value}->{new_value}  "
                    f"fitness {current_fitness:.4f}->{new_fitness:.4f}  "
                    f"wf_spread {prev_spread:.2f}->{wf_spread:.2f}"
                )
                append_params_history({
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "iter": iteration_num,
                    "kept": False,
                    "param_path": param_path,
                    "old_value": old_value,
                    "new_value": new_value,
                    "fitness_before": current_fitness,
                    "fitness_after": new_fitness,
                    "reason": "REVERTED_WF_REGRESSION",
                    "rationale": rationale,
                    "walk_forward_spread": wf_spread,
                    "walk_forward_verdict": wf_verdict,
                    "session_best_fitness_before": current_best_fitness,
                    "session_best_fitness_after":  current_best_fitness,
                    "historian_top_signal": historian_top,
                })
                _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "REVERTED")
                return current_fitness, False, param_path

        _append_change_log(
            f"KEPT  iter={iteration_num}  {param_path}: {old_value}->{new_value}  "
            f"fitness {current_fitness:.4f}->{new_fitness:.4f}  delta={delta:+.4f}  "
            f"rationale={rationale[:120]}"
        )
        append_params_history({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iter": iteration_num,
            "kept": True,
            "param_path": param_path,
            "old_value": old_value,
            "new_value": new_value,
            "fitness_before": current_fitness,
            "fitness_after": new_fitness,
            "reason": "improved",
            "rationale": rationale,
            "walk_forward_spread":  wf_spread,
            "walk_forward_verdict": wf_verdict,
            "best_fitness_after": new_fitness,
            "session_best_fitness_before": current_best_fitness,
            "session_best_fitness_after":  new_fitness,
            "historian_top_signal": historian_top,
        })
        # Update known-good snapshot (only goes up -- never on reverts)
        save_current_best(current_fitness, new_fitness)
        _current_best_fitness = new_fitness
        _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "KEPT")
        run_prep()
        return new_fitness, True, param_path

    else:
        print(f"  [REVERTED] fitness did not improve ({delta:+.4f}) -- reverting")
        restore_to_best()
        _append_change_log(
            f"ROLLBACK  iter={iteration_num}  {param_path}: {old_value}->{new_value}  "
            f"fitness {current_fitness:.4f}->{new_fitness:.4f}  delta={delta:+.4f}  "
            f"reason=no_improvement"
        )
        append_params_history({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iter": iteration_num,
            "kept": False,
            "param_path": param_path,
            "old_value": old_value,
            "new_value": new_value,
            "fitness_before": current_fitness,
            "fitness_after": new_fitness,
            "reason": "no_improvement",
            "rationale": rationale,
            "session_best_fitness_before": current_best_fitness,
            "session_best_fitness_after":  current_best_fitness,
            "historian_top_signal": historian_top,
        })
        _save_verdict(iteration_num, current_fitness, agent1_data, param_path, "REVERTED")
        return current_fitness, False, param_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations",    type=int,   default=1)
    ap.add_argument("--budget",        type=float, default=10.0,
                    help="Max total API spend in USD")
    ap.add_argument("--once",          action="store_true",
                    help="Run exactly one iteration")
    ap.add_argument("--baseline-only", action="store_true",
                    help="Run baseline simulation only, no agent loop")
    ap.add_argument("--no-restore",      action="store_true",
                    help="Diagnostic: skip session-start restore, run whatever is in strategy_params.json")
    ap.add_argument("--analysis-report", default=None,
                    help="Path to portfolio_report.json to sample trades from (default: reports/portfolio_report.json)")
    args = ap.parse_args()

    if not _ANTHROPIC_OK:
        print("ERROR: anthropic package not installed  (pip install anthropic)")
        sys.exit(1)

    global _session_timestamp
    _session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    n_iterations = 1 if args.once else args.iterations
    budget_limit = args.budget

    print(f"\n{'='*72}")
    print(f"  TRADE OPTIMIZER  --  {n_iterations} iteration(s)  budget=${budget_limit:.2f}")
    print(f"{'='*72}\n")

    # ---- Session-start: restore strategy_params.json from current_best_params.json ----
    # This ensures every session starts from the true validated best state,
    # not from a partially-modified or corrupted previous session's scratchpad.
    if args.no_restore:
        print("  [BEST] --no-restore: skipping session-start restore, running strategy_params.json as-is")
        live = json.loads(PARAMS_JSON.read_text())
        print(f"  [BEST] Live params: trailing_stop_pct={live['exits']['trailing_stop_pct']}, "
              f"below_ma_trend_floor={live['exits']['below_ma_trend_floor']}, "
              f"gm_tops.solar_hw={live['gates']['gm_tops']['solar_hw']}")
    else:
        if not CURRENT_BEST_JSON.exists():
            shutil.copy2(str(PARAMS_JSON), str(CURRENT_BEST_JSON))
            print("  [BEST] Initialized current_best_params.json from current strategy_params.json")

        shutil.copy2(str(CURRENT_BEST_JSON), str(PARAMS_JSON))
        print(f"  [BEST] Session start: restored strategy_params.json from current_best_params.json")
        restored = json.loads(CURRENT_BEST_JSON.read_text())
        print(f"  [BEST] Verified: trailing_stop_pct={restored['exits']['trailing_stop_pct']}, "
              f"below_ma_trend_floor={restored['exits']['below_ma_trend_floor']}, "
              f"gm_tops.solar_hw={restored['gates']['gm_tops']['solar_hw']}")

    # ---- Baseline (runs on the restored best-state params) ----
    print("  Running baseline simulation ...")
    base_summary, base_fitness = baseline_run()
    baseline_n_closed = base_summary.get("n_closed", 0)

    global _current_best_fitness
    _current_best_fitness = base_fitness
    current_best_fitness  = base_fitness

    # Dynamic best_ever: rises with every real improvement, never needs manual editing.
    best_ever = max(BEST_FITNESS_EVER_OVERRIDE, current_best_fitness)
    print(f"  [BEST] Best-ever fitness : {best_ever:.4f}  "
          f"(floor={BEST_FITNESS_EVER_OVERRIDE:.2f}, baseline={current_best_fitness:.4f})")

    # ---- Run Historian (Agent 3) once -- even in baseline-only mode ----
    history = load_params_history()
    run_historian(history, best_ever=best_ever)

    # ---- Load all-time reverted pairs for the code-level repeat blocker ----
    global _known_reverted, _tested_this_session
    _tested_this_session = set()
    _known_reverted = {}
    for entry in history:
        if not entry.get("kept", True) and entry.get("reason") != "INVALID_CODE_VERSION":
            p = entry.get("param_path")
            v = entry.get("new_value")
            if p is not None and v is not None:
                key = (p, str(v))
                fb, fa = entry.get("fitness_before"), entry.get("fitness_after")
                try:
                    delta_str = (
                        f"{float(fa) - float(fb):+.4f}"
                        if fb is not None and fa is not None
                        else "?"
                    )
                except Exception:
                    delta_str = "?"
                _known_reverted[key] = delta_str
    print(f"  [BLOCKER] Loaded {len(_known_reverted)} known-reverted pairs from history")

    if args.baseline_only:
        print("\n  [BASELINE-ONLY] Done.")
        return

    current_fitness = base_fitness
    kept_total      = 0

    i = 0
    for i in range(1, n_iterations + 1):
        budget_remaining = budget_limit - _session_cost
        if budget_remaining <= 0:
            print(f"\n  [BUDGET] Session cost ${_session_cost:.4f} >= limit "
                  f"${budget_limit:.2f} -- stopping")
            break

        new_fitness, kept, param_path = run_iteration(
            i, baseline_n_closed, current_fitness, budget_remaining,
            kept_before=kept_total,
            current_best_fitness=current_best_fitness,
            session_blacklist=_tested_this_session,
            analysis_report=args.analysis_report,
        )
        current_fitness = new_fitness
        if kept:
            current_best_fitness = new_fitness

    # ---- Session-end summary ----
    # best_ever was computed at session start as max(floor, baseline); raise it by any
    # new KEPTs this session.  Do not read it back from the Historian -- the Historian
    # annotates history but should not be the authority on the confirmed ceiling.
    best_fitness_ever = max(best_ever, current_best_fitness)
    gap = current_best_fitness - best_fitness_ever
    n_reverted = i - kept_total if i > 0 else 0

    print(f"\n{'='*72}")
    print(f"  SESSION COMPLETE")
    print(f"  Iterations run      : {i}")
    print(f"  Changes kept        : {kept_total}")
    print(f"  Changes reverted    : {n_reverted}")
    print()
    print(f"  Session fitness     : {base_fitness:.4f} -> {current_fitness:.4f}  "
          f"({current_fitness - base_fitness:+.4f})")
    print(f"  Best-ever fitness   : {best_fitness_ever:.4f}")
    if gap < -0.001:
        print(f"  Gap to best-ever    : {gap:+.4f}  (run more sessions to recover)")
    else:
        print(f"  Gap to best-ever    : {gap:+.4f}  (at or above best-ever)")
    print()
    print(f"  current_best_params.json: fitness {current_best_fitness:.4f}")
    print(f"  (this file only goes up -- never modified by reverts)")
    print()
    print(f"  Session cost        : ${_session_cost:.4f}")
    print(f"  Tokens in/out       : {_session_tokens['in']}/{_session_tokens['out']}")
    if _walk_forward_cache is not None:
        print(f"\n  WALK-FORWARD (last check at iteration {_walk_forward_cache['iteration']}):")
        _wf.print_summary(
            _walk_forward_cache["period_results"],
            _walk_forward_cache,
        )
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
