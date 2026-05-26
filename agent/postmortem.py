"""
Post-mortem analysis for closed losing trades.

Provides two layers:
  1. Mechanical root cause classification — free, always available
  2. Claude API narrative + recommendations — on-demand, requires ANTHROPIC_API_KEY

Public API:
    classify_loss(trade_dict)              → {root_cause, detail}
    analyze_trade(trade_id, api_key=None)  → full analysis dict, cached in DB
    generate_weekly_insights(api_key)      → aggregate patterns for last 30 days
"""

import json
import os
import sqlite3
import threading
from collections import Counter
from datetime import date, datetime

from agent.constants import (
    HIGH_IV_THRESHOLD,
    POSTMORTEM_FAST_LOSS_DAYS,
    POSTMORTEM_PREMATURE_DAYS,
)
from agent.position_manager import DB_PATH

_LOCK = threading.Lock()

CREDIT_STRATS = frozenset({
    "Bull Put Spread", "Bear Call Spread", "Iron Condor",
    "Cash-Secured Put", "Covered Call",
})
DEBIT_STRATS = frozenset({
    "Bull Call Spread", "Bear Put Spread", "Long Straddle",
})

_POSTMORTEM_DDL = """
CREATE TABLE IF NOT EXISTS postmortem (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id               INTEGER NOT NULL UNIQUE,
    root_cause             TEXT    NOT NULL,
    root_cause_detail      TEXT,
    claude_analysis        TEXT,
    claude_recommendations TEXT,
    generated_at           TEXT    NOT NULL,
    model_used             TEXT
);
CREATE TABLE IF NOT EXISTS weekly_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start    TEXT,
    period_end      TEXT,
    patterns        TEXT NOT NULL,
    recommendations TEXT,
    raw_response    TEXT,
    generated_at    TEXT NOT NULL
);
"""


def _ensure_tables() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.executescript(_POSTMORTEM_DDL)
        conn.commit()
    finally:
        conn.close()


_ensure_tables()


# ── Layer 1: Mechanical root cause ────────────────────────────────────────────

def classify_loss(t: dict) -> dict:
    """Classify a closed losing trade by root cause from stored fields only."""
    reason   = (t.get("close_reason") or "").lower()
    strategy = t.get("strategy") or ""
    entry_score  = t.get("entry_score")
    thesis_score = t.get("thesis_score")
    entry_iv     = t.get("entry_iv")   # raw decimal, e.g. 0.35 = 35%

    # 1. IV regime mismatch (requires entry_iv)
    if entry_iv is not None:
        iv_pct = float(entry_iv) * 100
        if strategy in DEBIT_STRATS and iv_pct > HIGH_IV_THRESHOLD:
            return {
                "root_cause": "IV_REGIME_MISMATCH",
                "detail": (
                    f"Debit spread entered when IV={iv_pct:.1f}% > {HIGH_IV_THRESHOLD}% threshold. "
                    "Overpaid for premium in a high-IV environment."
                ),
            }
        if strategy in CREDIT_STRATS and iv_pct <= HIGH_IV_THRESHOLD:
            return {
                "root_cause": "IV_REGIME_MISMATCH",
                "detail": (
                    f"Credit spread entered when IV={iv_pct:.1f}% <= {HIGH_IV_THRESHOLD}% threshold. "
                    "Insufficient credit collected in a low-IV environment."
                ),
            }

    # 2. Assignment (CSP went ITM)
    if reason == "assigned":
        return {
            "root_cause": "ASSIGNMENT",
            "detail": "Cash-Secured Put assigned — stock moved ITM at expiry.",
        }

    # 3. Cut loss — check duration for timing-based sub-types
    if reason == "cut_loss":
        oa = (t.get("opened_at") or "")[:10]
        ca = (t.get("closed_at") or "")[:10]
        duration = 0
        if oa and ca:
            try:
                duration = (date.fromisoformat(ca) - date.fromisoformat(oa)).days
            except ValueError:
                pass
        if duration <= POSTMORTEM_FAST_LOSS_DAYS:
            return {
                "root_cause": "BAD_ENTRY_TIMING",
                "detail": (
                    f"Cut loss triggered within {duration}d of entry. "
                    "Signal was likely noise or entry caught a news/earnings catalyst."
                ),
            }
        if duration <= POSTMORTEM_PREMATURE_DAYS:
            return {
                "root_cause": "PREMATURE_STOP",
                "detail": (
                    f"Cut loss after only {duration}d — "
                    "trade was stopped before it had time to work."
                ),
            }
        return {
            "root_cause": "PREMATURE_STOP",
            "detail": f"Cut loss triggered after {duration}d.",
        }

    # 4. Thesis invalidation → momentum collapse
    if reason == "thesis_exit":
        e = float(entry_score) if entry_score is not None else 0.0
        c = float(thesis_score) if thesis_score is not None else 0.0
        if (e > 0.1 and c < -0.1) or (e < -0.1 and c > 0.1):
            return {
                "root_cause": "MOMENTUM_COLLAPSE",
                "detail": (
                    f"Direction fully reversed: entry_score={e:+.3f} → thesis_score={c:+.3f}."
                ),
            }
        return {
            "root_cause": "MOMENTUM_COLLAPSE",
            "detail": (
                f"Thesis invalidated: entry_score={e:+.3f}, "
                f"thesis_score_at_exit={c:+.3f}."
            ),
        }

    # 5. Theta bleed
    if reason == "theta_exit":
        return {
            "root_cause": "THETA_BLEED",
            "detail": (
                "Debit position exited at DTE threshold — "
                "time decay eroded value before the directional move occurred."
            ),
        }

    # 6. Roll declined
    if reason in ("roll_declined", "roll_declined_close"):
        return {
            "root_cause": "ROLL_DECLINED",
            "detail": "Roll evaluated but declined (cost exceeded threshold). Closed at a loss.",
        }

    return {
        "root_cause": "UNCLASSIFIED",
        "detail": (
            f"close_reason={t.get('close_reason')}, "
            f"entry_score={entry_score}, thesis_score={thesis_score}"
        ),
    }


# ── Layer 2: Context builder + Claude prompt ──────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def build_trade_context(trade_id: int, conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise ValueError(f"trade {trade_id} not found")
    t = dict(row)

    legs = [dict(r) for r in conn.execute(
        "SELECT * FROM legs WHERE trade_id=?", (trade_id,)
    ).fetchall()]

    # Derive entry_iv from legs if not stored on trade record (historical trades)
    if not t.get("entry_iv") and legs:
        iv_vals = [float(l["iv"]) for l in legs if l.get("iv")]
        if iv_vals:
            t["entry_iv"] = round(sum(iv_vals) / len(iv_vals), 4)

    similar = [dict(r) for r in conn.execute(
        """SELECT stock_code, strategy, close_reason, close_pnl,
                  entry_score, thesis_score, opened_at, closed_at
           FROM trades
           WHERE strategy=? AND close_pnl < 0 AND status='closed' AND id != ?
           ORDER BY closed_at DESC LIMIT 8""",
        (t["strategy"], trade_id),
    ).fetchall()]

    return {
        "trade": t,
        "legs": legs,
        "similar_losses": similar,
        "root_cause": classify_loss(t),
    }


def _build_prompt(ctx: dict) -> str:
    t    = ctx["trade"]
    legs = ctx["legs"]
    root = ctx["root_cause"]
    sim  = ctx["similar_losses"]

    oa  = (t.get("opened_at") or "")[:10]
    ca  = (t.get("closed_at") or "")[:10]
    dur = ""
    if oa and ca:
        try:
            dur = f"{(date.fromisoformat(ca) - date.fromisoformat(oa)).days}d"
        except ValueError:
            pass

    iv_pct = f"{float(t['entry_iv'])*100:.1f}%" if t.get("entry_iv") else "N/A"
    entry_score  = t.get("entry_score")
    thesis_score = t.get("thesis_score")
    es_str = f"{float(entry_score):+.3f}"  if entry_score  is not None else "N/A"
    ts_str = f"{float(thesis_score):+.3f}" if thesis_score is not None else "N/A"

    leg_lines = "\n".join(
        f"  {l.get('side','')} {l.get('call_or_put','')} "
        f"K={l.get('strike','')} delta={l.get('delta','')} iv={l.get('iv','')}"
        for l in legs
    ) or "  (stock trade — no legs)"

    sim_lines = "\n".join(
        f"  {r['stock_code']} | {r['strategy']} | {r['close_reason']} "
        f"| ${r.get('close_pnl',0):.2f} | score={r.get('entry_score','N/A')}"
        for r in sim
    ) or "  (none)"

    return f"""TRADE POST-MORTEM

Stock: {t.get('stock_code')} | Strategy: {t.get('strategy')} | Market: {t.get('market')}
Opened: {oa} | Closed: {ca} | Duration: {dur}
Close reason: {t.get('close_reason')} | P&L: ${t.get('close_pnl',0):.2f}

ENTRY: score={es_str} | IV={iv_pct} | net_credit={t.get('net_credit_per_spread','N/A')}
LEGS AT FILL:
{leg_lines}

EXIT: thesis_score={ts_str}
MECHANICAL CLASSIFICATION: {root['root_cause']} — {root['detail']}

SIMILAR PAST LOSSES ({len(sim)}):
{sim_lines}

CURRENT CONFIG:
HIGH_IV_THRESHOLD={HIGH_IV_THRESHOLD}%, US_SPREAD_MIN_SIGNAL=0.10, RUSSELL_MIN_SCORE=0.25
TP_CREDIT_REMAINING=0.20, CL_CREDIT_MULT=2.0, STOCK_CL_PCT=0.10

Provide a concise analysis:
1. WHAT HAPPENED: 2-3 sentence plain English explanation of why this trade lost.
2. ROOT CAUSE: Confirm or refine the mechanical classification above.
3. RECOMMENDATIONS: 1-2 specific config changes (only if clearly warranted by the data).
   Format each as: CONSTANT_NAME: current_value → suggested_value | Rationale: one sentence
4. AVOID: One entry pattern to avoid in future (one sentence).
"""


def _extract_recommendations(text: str) -> list:
    recs = []
    in_section = False
    for line in text.splitlines():
        upper = line.upper()
        if "3." in line and "RECOMMEND" in upper:
            in_section = True
            continue
        if in_section and ("4." in line or "AVOID" in upper):
            break
        if in_section and "→" in line and "|" in line:
            try:
                left, right = line.split("|", 1)
                rationale = right.split(":", 1)[-1].strip()
                const_part, vals = left.split(":", 1)
                curr, sugg = vals.split("→", 1)
                recs.append({
                    "constant":  const_part.strip().lstrip("- "),
                    "current":   curr.strip(),
                    "suggested": sugg.strip(),
                    "rationale": rationale,
                })
            except Exception:
                pass
    return recs


def call_claude_analysis(ctx: dict, api_key: str) -> dict:
    try:
        import anthropic
    except ImportError:
        return {
            "error": "anthropic package not installed — run: pip install anthropic",
            "analysis": None,
            "recommendations": [],
        }
    try:
        client = anthropic.Anthropic(api_key=api_key)
        prompt = _build_prompt(ctx)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = msg.content[0].text
        recs = _extract_recommendations(raw)
        return {
            "analysis":        raw,
            "recommendations": recs,
            "model":           "claude-haiku-4-5-20251001",
        }
    except Exception as e:
        return {"error": str(e), "analysis": None, "recommendations": []}


# ── Persistence ───────────────────────────────────────────────────────────────

def _save_postmortem(
    trade_id: int,
    root_cause: str,
    detail: str,
    analysis: str | None,
    recs: list | None,
    model: str | None,
) -> None:
    with _LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                conn.execute(
                    """INSERT OR REPLACE INTO postmortem
                       (trade_id, root_cause, root_cause_detail,
                        claude_analysis, claude_recommendations,
                        generated_at, model_used)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        trade_id, root_cause, detail,
                        analysis,
                        json.dumps(recs) if recs is not None else None,
                        datetime.now().isoformat(),
                        model,
                    ),
                )
        finally:
            conn.close()


# ── Public orchestrator ───────────────────────────────────────────────────────

def analyze_trade(trade_id: int, api_key: str | None = None) -> dict:
    """
    Classify the trade mechanically, optionally call Claude, cache result in DB.
    Returns a dict with root_cause, root_cause_detail, claude_analysis, recommendations.
    If api_key is None, only mechanical classification is performed.
    """
    conn = _get_conn()
    try:
        ctx = build_trade_context(trade_id, conn)
    finally:
        conn.close()

    root     = ctx["root_cause"]
    analysis = recs = model = None

    if api_key:
        result   = call_claude_analysis(ctx, api_key)
        analysis = result.get("analysis")
        recs     = result.get("recommendations") or []
        model    = result.get("model")

    _save_postmortem(trade_id, root["root_cause"], root["detail"], analysis, recs, model)

    return {
        "trade_id":           trade_id,
        "root_cause":         root["root_cause"],
        "root_cause_detail":  root["detail"],
        "claude_analysis":    analysis,
        "claude_recommendations": recs,
        "model":              model,
    }


# ── Weekly aggregate insights ─────────────────────────────────────────────────

def generate_weekly_insights(api_key: str) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic package not installed — run: pip install anthropic"}

    conn = _get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            """SELECT t.strategy, t.close_reason, t.close_pnl,
                      t.entry_score, t.trade_type, p.root_cause
               FROM trades t
               LEFT JOIN postmortem p ON p.trade_id = t.id
               WHERE t.status='closed'
                 AND t.closed_at >= date('now','-30 days')
                 AND (t.close_reason IS NULL OR t.close_reason != 'cancelled')"""
        ).fetchall()]
    finally:
        conn.close()

    if not rows:
        return {"error": "No closed trades in the last 30 days"}

    total     = len(rows)
    wins      = [r for r in rows if (r.get("close_pnl") or 0) > 0]
    losses    = [r for r in rows if (r.get("close_pnl") or 0) < 0]
    total_pnl = sum(r.get("close_pnl") or 0 for r in rows)

    reason_counts   = Counter(r["close_reason"] for r in rows)
    root_counts     = Counter(r["root_cause"] for r in losses if r.get("root_cause"))

    def _wr(lst):
        if not lst: return "N/A"
        w = sum(1 for r in lst if (r.get("close_pnl") or 0) > 0)
        return f"{100*w//len(lst)}%"

    bull = [r for r in rows if (r.get("entry_score") or 0) > 0.25]
    bear = [r for r in rows if (r.get("entry_score") or 0) < -0.25]
    neut = [r for r in rows if abs(r.get("entry_score") or 0) <= 0.25]

    strat_stats: dict = {}
    for r in rows:
        s = r["strategy"]
        strat_stats.setdefault(s, {"n": 0, "wins": 0, "pnl": 0.0, "stops": 0})
        strat_stats[s]["n"] += 1
        if (r.get("close_pnl") or 0) > 0:
            strat_stats[s]["wins"] += 1
        strat_stats[s]["pnl"]  += r.get("close_pnl") or 0
        if r.get("close_reason") == "cut_loss":
            strat_stats[s]["stops"] += 1

    strat_lines = "\n".join(
        f"  {s}: {v['n']} trades, "
        f"{100*v['wins']//v['n'] if v['n'] else 0}% win, "
        f"${v['pnl']:.2f} pnl, {v['stops']} stops"
        for s, v in strat_stats.items()
    )
    reason_lines = " | ".join(f"{k}: {v}" for k, v in reason_counts.most_common())
    root_lines   = " | ".join(f"{k}: {v}" for k, v in root_counts.most_common()) or "none classified yet"

    prompt = f"""WEEKLY TRADING INSIGHTS (last 30 days)

Trades: {total} | Wins: {len(wins)} ({100*len(wins)//total if total else 0}%) | Losses: {len(losses)} | Total P&L: ${total_pnl:.2f}
Close reasons: {reason_lines}
Loss root causes: {root_lines}

Strategy breakdown:
{strat_lines}

Score accuracy:
  Bullish entries (score>0.25):  {len(bull)} trades, {_wr(bull)} win rate
  Bearish entries (score<-0.25): {len(bear)} trades, {_wr(bear)} win rate
  Neutral entries:               {len(neut)} trades, {_wr(neut)} win rate

Current config:
HIGH_IV_THRESHOLD={HIGH_IV_THRESHOLD}%, US_SPREAD_MIN_SIGNAL=0.10, RUSSELL_MIN_SCORE=0.25
TP_CREDIT_REMAINING=0.20, CL_CREDIT_MULT=2.0, STOCK_CL_PCT=0.10

Identify the top 3 recurring loss patterns from the data above.
For each pattern use this format:
PATTERN N: [short name]
  Evidence: [specific numbers from above]
  Config change: CONSTANT_NAME: current_value → suggested_value
  Expected impact: one sentence

Keep each pattern under 4 sentences. Focus on data-driven changes only.
If the data is insufficient for a pattern, say so rather than guessing.
"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
    except Exception as e:
        return {"error": str(e)}

    _save_weekly_insights(raw)
    return {
        "patterns":         raw,
        "generated_at":     datetime.now().isoformat(),
        "trades_analyzed":  total,
    }


def _save_weekly_insights(raw: str) -> None:
    with _LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                conn.execute(
                    """INSERT INTO weekly_insights
                       (period_start, period_end, patterns, recommendations,
                        raw_response, generated_at)
                       VALUES (date('now','-30 days'), date('now'), ?, ?, ?, ?)""",
                    (raw, "[]", raw, datetime.now().isoformat()),
                )
        finally:
            conn.close()
