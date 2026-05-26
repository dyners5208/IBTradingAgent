"""
Read-only database helper for the dashboard.
Never writes — the agent is the sole writer.
WAL mode guarantees reads never block the running agent.
No pandas — plain sqlite3 returning list[dict].
"""

import glob as _glob
import json
import os
import sqlite3
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(_ROOT, "trade_log.db")
ALERTS_PATH = os.path.join(_ROOT, "alerts.json")
LOGS_DIR    = os.path.join(_ROOT, "logs")

CATEGORIES = ["US Spreads", "Russell", "US Politician", "Wheel", "HK Stocks", "Gems"]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _rows(sql: str, params=()) -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _classify(r: dict) -> str:
    """Classify a trade record dict into one of the 6 strategy categories."""
    src = r.get("scan_source") or ""
    if src == "russell":
        return "Russell"
    if src == "politician":
        return "US Politician"
    if src == "gem":
        return "Gems"
    wt  = r.get("wheel_type") or ""
    stg = r.get("strategy")   or ""
    if wt in ("CSP", "CC") or stg in ("Cash-Secured Put", "Covered Call"):
        return "Wheel"
    if (r.get("market") or "") == "HK":
        return "HK Stocks"
    return "US Spreads"


def _income_usd(r: dict) -> float | None:
    """
    Capital-at-work for one trade:
      stock trades       → cost field (actual capital deployed)
      credit options     → net_credit_per_spread × contracts × 100  (premium income)
      debit options      → abs(net_credit_per_spread) × contracts × 100  (premium paid)
    Returns None when required fields are missing/zero.
    """
    tt = r.get("trade_type") or ""
    if tt == "stock":
        c = r.get("cost")
        return round(float(c), 2) if c else None
    nc = r.get("net_credit_per_spread")
    nq = r.get("num_contracts")
    if nc is None or nq is None:
        return None
    val = abs(float(nc)) * int(nq) * 100
    return round(val, 2) if val else None


def _duration_days(r: dict) -> int | None:
    oa = (r.get("opened_at") or "")[:10]
    ca = (r.get("closed_at") or "")[:10]
    if not oa or not ca:
        return None
    try:
        return (date.fromisoformat(ca) - date.fromisoformat(oa)).days
    except ValueError:
        return None


def _roi_pct(close_pnl, income_usd) -> float | None:
    if not income_usd or income_usd == 0:
        return None
    return round(float(close_pnl or 0) / income_usd * 100, 1)


_HKD_USD = 7.80  # HKD per 1 USD; matches agent/constants.py HKD_USD_RATE default


def _to_usd(amount: float, r: dict) -> float:
    """HK stock P&L and cost are stored in HKD; all other trades are already USD."""
    if (r.get("market") or "") == "HK" and (r.get("trade_type") or "") == "stock":
        return amount / _HKD_USD
    return amount


# ── Overview stats ─────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Overview card data: total P&L, counts, win rate, today's P&L, portfolio unrealized."""
    today_s = date.today().isoformat()
    all_rows = _rows(
        "SELECT status, close_pnl, close_reason, closed_at, market, trade_type, unrealized_pnl FROM trades"
    )
    closed = [r for r in all_rows
              if r["status"] == "closed"
              and (r.get("close_reason") or "") != "cancelled"]
    open_  = [r for r in all_rows if r["status"] == "open"]
    total  = len(closed)
    wins   = sum(1 for r in closed if (r["close_pnl"] or 0) > 0)
    pnl    = sum(_to_usd(r["close_pnl"] or 0, r) for r in closed)
    today  = sum(_to_usd(r["close_pnl"] or 0, r) for r in closed
                 if str(r.get("closed_at") or "").startswith(today_s))
    unrealized_rows = [r for r in open_ if r.get("unrealized_pnl") is not None]
    portfolio_unrealized = sum(_to_usd(r["unrealized_pnl"], r) for r in unrealized_rows)
    return {
        "total_pnl":                  round(pnl, 2),
        "today_pnl":                  round(today, 2),
        "open_count":                 len(open_),
        "closed_count":               total,
        "win_rate":                   round(wins / total * 100, 1) if total else 0,
        "win_count":                  wins,
        "portfolio_unrealized":       round(portfolio_unrealized, 2),
        "portfolio_unrealized_count": len(unrealized_rows),
    }


# ── P&L history ────────────────────────────────────────────────────────────────

def get_pnl_history() -> dict:
    """Daily and monthly P&L arrays for charts."""
    rows = _rows(
        "SELECT closed_at, close_pnl, market, trade_type FROM trades "
        "WHERE status='closed' AND closed_at IS NOT NULL "
        "ORDER BY closed_at ASC"
    )
    daily_map: dict = {}
    for r in rows:
        d = (r["closed_at"] or "")[:10]
        if d:
            daily_map[d] = daily_map.get(d, 0) + _to_usd(r["close_pnl"] or 0, r)

    daily = []
    cumulative = 0.0
    for d in sorted(daily_map):
        cumulative += daily_map[d]
        daily.append({"date": d, "pnl": round(daily_map[d], 2),
                      "cumulative": round(cumulative, 2)})

    monthly_map: dict = {}
    for r in rows:
        m = (r["closed_at"] or "")[:7]
        if m:
            monthly_map[m] = monthly_map.get(m, 0) + _to_usd(r["close_pnl"] or 0, r)

    monthly = [{"month": m, "pnl": round(v, 2)}
               for m, v in sorted(monthly_map.items())]

    return {"daily": daily, "monthly": monthly}


# ── Open positions ─────────────────────────────────────────────────────────────

def get_open_trades() -> list:
    """Open positions with computed DTE and days_open."""
    rows = _rows(
        "SELECT stock_code, strategy, market, trade_type, scan_source, wheel_type, "
        "       opened_at, exp_date, num_contracts, net_credit_per_spread, "
        "       tp_value, cl_value, thesis_last_checked, thesis_score, "
        "       unrealized_pnl, qty, limit_price "
        "FROM trades WHERE status='open' ORDER BY opened_at DESC"
    )
    today = date.today()
    for r in rows:
        opened = (r.get("opened_at") or "")[:10]
        r["days_open"] = (today - date.fromisoformat(opened)).days if opened else None
        exp = r.get("exp_date")
        if exp:
            try:
                r["dte"] = (date.fromisoformat(str(exp)[:10]) - today).days
            except ValueError:
                r["dte"] = None
        else:
            r["dte"] = None
    return rows


# ── Closed trades (enriched) ───────────────────────────────────────────────────

def get_closed_trades_enriched() -> list:
    """Closed trades enriched with duration_days, income_usd, roi_pct, category."""
    rows = _rows(
        "SELECT stock_code, strategy, market, trade_type, close_reason, close_pnl, "
        "       opened_at, closed_at, scan_source, wheel_type, exp_date, "
        "       cost, net_credit_per_spread, num_contracts "
        "FROM trades WHERE status='closed' ORDER BY closed_at DESC"
    )
    for r in rows:
        r["category"]      = _classify(r)
        r["duration_days"] = _duration_days(r)
        income             = _income_usd(r)
        if (r.get("market") or "") == "HK" and (r.get("trade_type") or "") == "stock":
            if r.get("close_pnl") is not None:
                r["close_pnl"] = round(float(r["close_pnl"]) / _HKD_USD, 2)
            income = round(income / _HKD_USD, 2) if income else None
        r["income_usd"]    = income
        r["roi_pct"]       = _roi_pct(r.get("close_pnl"), income)
    return rows


def get_closed_trades() -> list:
    """Legacy alias for get_closed_trades_enriched()."""
    return get_closed_trades_enriched()


# ── Category analytics ─────────────────────────────────────────────────────────

def get_category_analytics() -> dict:
    """
    Per-category investor metrics:
      open, closed, wins, win_rate, realized_today, realized_alltime,
      profit_factor, avg_duration_days, total_income_usd, roi_pct,
      tp_rate, stop_rate, open_income_usd
    """
    today_s = date.today().isoformat()
    rows = _rows(
        "SELECT scan_source, wheel_type, strategy, market, status, "
        "       close_pnl, close_reason, closed_at, opened_at, "
        "       trade_type, cost, net_credit_per_spread, num_contracts "
        "FROM trades"
    )

    stats: dict = {
        cat: {
            "open":             0,
            "closed":           0,
            "wins":             0,
            "realized_today":   0.0,
            "realized_alltime": 0.0,
            "open_income_usd":  0.0,
            "total_income_usd": 0.0,
            "_wins_sum":        0.0,
            "_losses_sum":      0.0,
            "_durations":       [],
            "_tp_count":        0,
            "_stop_count":      0,
        }
        for cat in CATEGORIES
    }

    for r in rows:
        cat = _classify(r)
        if cat not in stats:
            continue
        s      = stats[cat]
        income = _income_usd(r)

        if r["status"] == "open":
            s["open"] += 1
            if income:
                s["open_income_usd"] += _to_usd(income, r)

        elif r["status"] == "closed":
            if (r.get("close_reason") or "") == "cancelled":
                continue
            pnl = _to_usd(float(r.get("close_pnl") or 0.0), r)
            s["closed"] += 1
            s["realized_alltime"] += pnl
            if pnl > 0:
                s["wins"] += 1
                s["_wins_sum"] += pnl
            elif pnl < 0:
                s["_losses_sum"] += abs(pnl)
            if str(r.get("closed_at") or "").startswith(today_s):
                s["realized_today"] += pnl
            if income:
                income_usd = _to_usd(income, r)
                s["total_income_usd"] += income_usd
            dur = _duration_days(r)
            if dur is not None:
                s["_durations"].append(dur)
            cr = r.get("close_reason") or ""
            if cr == "take_profit":
                s["_tp_count"] += 1
            elif cr == "cut_loss":
                s["_stop_count"] += 1

    for s in stats.values():
        total = s["closed"]
        ws    = s.pop("_wins_sum")
        ls    = s.pop("_losses_sum")
        durs  = s.pop("_durations")
        tp    = s.pop("_tp_count")
        stp   = s.pop("_stop_count")

        s["win_rate"] = round(s["wins"] / total * 100, 1) if total else None

        if ws > 0 and ls > 0:
            s["profit_factor"] = round(ws / ls, 2)
        elif ws > 0:
            s["profit_factor"] = None   # infinite — all wins, shown as "∞" in UI
        else:
            s["profit_factor"] = 0.0

        s["avg_duration_days"] = round(sum(durs) / len(durs), 1) if durs else None
        s["tp_rate"]           = round(tp  / total * 100, 1) if total else None
        s["stop_rate"]         = round(stp / total * 100, 1) if total else None

        ti = s["total_income_usd"]
        s["roi_pct"]           = round(s["realized_alltime"] / ti * 100, 1) if ti else None
        s["realized_today"]    = round(s["realized_today"], 2)
        s["realized_alltime"]  = round(s["realized_alltime"], 2)
        s["total_income_usd"]  = round(ti, 2)
        s["open_income_usd"]   = round(s["open_income_usd"], 2)

    return stats


def get_category_stats() -> dict:
    """Legacy alias for get_category_analytics()."""
    return get_category_analytics()


def get_category_trades(category: str) -> list:
    """
    All trades (open first, then closed) classified into `category`,
    enriched with duration_days, income_usd, roi_pct.
    """
    today = date.today()
    rows = _rows(
        "SELECT stock_code, strategy, market, trade_type, status, "
        "       scan_source, wheel_type, opened_at, closed_at, exp_date, "
        "       num_contracts, net_credit_per_spread, close_pnl, close_reason, "
        "       cost, tp_value, cl_value "
        "FROM trades ORDER BY opened_at DESC"
    )

    result = []
    for r in rows:
        if _classify(r) != category:
            continue
        income             = _income_usd(r)
        r["income_usd"]    = income
        r["duration_days"] = _duration_days(r) if r["status"] == "closed" else None
        r["roi_pct"]       = _roi_pct(r.get("close_pnl"), income) if r["status"] == "closed" else None
        r["category"]      = category

        exp = r.get("exp_date")
        if exp:
            try:
                r["dte"] = (date.fromisoformat(str(exp)[:10]) - today).days
            except ValueError:
                r["dte"] = None
        else:
            r["dte"] = None

        result.append(r)

    # Open first, then closed — already DESC opened_at so just stable-sort by status
    result.sort(key=lambda x: (0 if x["status"] == "open" else 1))
    return result


def get_portfolio_composition() -> list:
    """Open position counts and income by category for the donut chart."""
    rows = _rows(
        "SELECT scan_source, wheel_type, strategy, market, "
        "       trade_type, cost, net_credit_per_spread, num_contracts "
        "FROM trades WHERE status='open'"
    )
    agg: dict = {cat: {"category": cat, "open_count": 0, "open_income_usd": 0.0}
                 for cat in CATEGORIES}
    for r in rows:
        cat = _classify(r)
        if cat not in agg:
            continue
        agg[cat]["open_count"] += 1
        income = _income_usd(r)
        if income:
            agg[cat]["open_income_usd"] += income
    result = []
    for g in agg.values():
        g["open_income_usd"] = round(g["open_income_usd"], 2)
        result.append(g)
    return result


# ── Strategy analytics ─────────────────────────────────────────────────────────

def get_strategy_analytics() -> list:
    """
    Per-strategy investor metrics:
      strategy, trades, wins, win_rate, total_pnl, avg_pnl,
      profit_factor, avg_duration_days, tp_rate, stop_rate,
      total_income_usd, credit_capture_rate, flag
    """
    rows = _rows(
        "SELECT strategy, close_pnl, close_reason, opened_at, closed_at, "
        "       trade_type, market, cost, net_credit_per_spread, num_contracts "
        "FROM trades WHERE status='closed'"
    )

    agg: dict = {}
    for r in rows:
        if (r.get("close_reason") or "") == "cancelled":
            continue
        s_key = r["strategy"] or "Unknown"
        if s_key not in agg:
            agg[s_key] = {
                "strategy":        s_key,
                "trades":          0,
                "wins":            0,
                "total_pnl":       0.0,
                "_wins_sum":       0.0,
                "_losses_sum":     0.0,
                "_durations":      [],
                "_tp_count":       0,
                "_stop_count":     0,
                "_incomes":        [],
            }
        g = agg[s_key]
        pnl = _to_usd(float(r.get("close_pnl") or 0.0), r)
        g["trades"]    += 1
        g["total_pnl"] += pnl
        if pnl > 0:
            g["wins"]        += 1
            g["_wins_sum"]   += pnl
        elif pnl < 0:
            g["_losses_sum"] += abs(pnl)
        dur = _duration_days(r)
        if dur is not None:
            g["_durations"].append(dur)
        cr = r.get("close_reason") or ""
        if cr == "take_profit":
            g["_tp_count"] += 1
        elif cr == "cut_loss":
            g["_stop_count"] += 1
        income = _income_usd(r)
        if income:
            income_usd = _to_usd(income, r)
            g["_incomes"].append((pnl, income_usd))

    result = []
    for g in agg.values():
        total = g["trades"]
        ws    = g.pop("_wins_sum")
        ls    = g.pop("_losses_sum")
        durs  = g.pop("_durations")
        tp    = g.pop("_tp_count")
        stp   = g.pop("_stop_count")
        incs  = g.pop("_incomes")

        g["win_rate"]  = round(g["wins"] / total * 100, 1) if total else 0
        g["avg_pnl"]   = round(g["total_pnl"] / total, 2) if total else 0
        g["total_pnl"] = round(g["total_pnl"], 2)

        if ws > 0 and ls > 0:
            g["profit_factor"] = round(ws / ls, 2)
        elif ws > 0:
            g["profit_factor"] = None   # infinite
        else:
            g["profit_factor"] = 0.0

        g["avg_duration_days"] = round(sum(durs) / len(durs), 1) if durs else None
        g["tp_rate"]           = round(tp  / total * 100, 1) if total else None
        g["stop_rate"]         = round(stp / total * 100, 1) if total else None

        if incs:
            g["total_income_usd"] = round(sum(inc for _, inc in incs), 2)
            avg_inc = sum(inc for _, inc in incs) / len(incs)
            avg_pnl_inc = sum(p for p, _ in incs) / len(incs)
            g["credit_capture_rate"] = round(avg_pnl_inc / avg_inc * 100, 1) if avg_inc else None
        else:
            g["total_income_usd"]    = 0.0
            g["credit_capture_rate"] = None

        wr = g["win_rate"] or 0
        pf = g["profit_factor"] or 0
        sr = g["stop_rate"] or 0
        if wr < 40 or pf < 1.0:
            g["flag"] = "warn"
        elif sr > 50:
            g["flag"] = "stop_rate"
        else:
            g["flag"] = None

        result.append(g)

    return sorted(result, key=lambda x: x["total_pnl"], reverse=True)


def get_by_strategy() -> list:
    """Legacy alias for get_strategy_analytics()."""
    return get_strategy_analytics()


# ── Wheel tracker ──────────────────────────────────────────────────────────────

def get_wheel() -> list:
    """Wheel tracker: per-stock summary."""
    rows = _rows(
        "SELECT stock_code, wheel_type, close_pnl, status, "
        "       wheel_assignment_price, wheel_premium_accumulated "
        "FROM trades WHERE wheel_type IS NOT NULL"
    )
    agg: dict = {}
    for r in rows:
        sc = r["stock_code"]
        if sc not in agg:
            agg[sc] = {
                "stock_code":        sc,
                "assignment_price":  r.get("wheel_assignment_price"),
                "premiums_collected": 0.0,
                "csp_count":         0,
                "cc_count":          0,
                "total_pnl":         0.0,
                "any_open":          False,
            }
        g  = agg[sc]
        wt = r.get("wheel_type", "")
        if wt == "CSP":
            g["csp_count"] += 1
        elif wt == "CC":
            g["cc_count"] += 1
        pnl = r.get("close_pnl") or 0
        g["total_pnl"] += pnl
        if r.get("status") == "open":
            g["any_open"] = True
        wp = r.get("wheel_premium_accumulated") or 0
        if wp > g["premiums_collected"]:
            g["premiums_collected"] = wp

    result = []
    for g in agg.values():
        asgn = g["assignment_price"] or 0
        prem = g["premiums_collected"]
        g["net_cost_basis"]    = round(asgn - prem / 100, 2) if asgn else None
        g["status"]            = "open" if g.pop("any_open") else "closed"
        g["total_pnl"]         = round(g["total_pnl"], 2)
        g["premiums_collected"] = round(prem, 2)
        result.append(g)

    return sorted(result, key=lambda x: x["stock_code"])


# ── Alerts ─────────────────────────────────────────────────────────────────────

def get_alerts() -> list:
    """Last 200 alerts from alerts.json, newest first."""
    if not os.path.exists(ALERTS_PATH):
        return []
    try:
        with open(ALERTS_PATH) as f:
            data = json.load(f)
        return list(reversed(data[-200:]))
    except Exception:
        return []


# ── Session log ────────────────────────────────────────────────────────────────

def get_session_log(lines: int = 300) -> dict:
    """Return last N lines from today's session log file."""
    today    = date.today().strftime("%Y-%m-%d")
    log_path = os.path.join(LOGS_DIR, f"agent_{today}.log")

    if not os.path.exists(log_path):
        files = sorted(_glob.glob(os.path.join(LOGS_DIR, "agent_*.log")))
        if not files:
            return {"lines": [], "file": None}
        log_path = files[-1]

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return {
            "lines": [ln.rstrip("\n") for ln in all_lines[-lines:]],
            "file":  os.path.basename(log_path),
            "total": len(all_lines),
        }
    except Exception as exc:
        return {"lines": [f"Error reading log: {exc}"], "file": None, "total": 0}


# ── Post-mortem analytics ──────────────────────────────────────────────────────
# These tables are created by agent/postmortem.py at import time.
# The dashboard reads them; the postmortem module writes them.

def get_postmortem(trade_id: int) -> dict | None:
    """Return cached postmortem for a trade, or None if not yet generated."""
    rows = _rows("SELECT * FROM postmortem WHERE trade_id=?", (trade_id,))
    if not rows:
        return None
    r = rows[0]
    # Deserialise JSON recommendations
    if r.get("claude_recommendations"):
        try:
            r["claude_recommendations"] = json.loads(r["claude_recommendations"])
        except Exception:
            r["claude_recommendations"] = []
    return r


def get_insights() -> dict | None:
    """Return the most recently generated weekly insights, or None."""
    rows = _rows(
        "SELECT * FROM weekly_insights ORDER BY generated_at DESC LIMIT 1"
    )
    return rows[0] if rows else None


def get_loss_breakdown() -> list:
    """Root cause distribution for the Insights donut chart."""
    rows = _rows(
        """SELECT p.root_cause, t.close_pnl, t.market, t.trade_type
           FROM postmortem p
           JOIN trades t ON t.id = p.trade_id
           WHERE t.close_pnl < 0"""
    )
    agg: dict = {}
    for r in rows:
        rc  = r["root_cause"]
        pnl = _to_usd(float(r.get("close_pnl") or 0), r)
        if rc not in agg:
            agg[rc] = {"root_cause": rc, "count": 0, "total_pnl": 0.0}
        agg[rc]["count"]     += 1
        agg[rc]["total_pnl"] += pnl
    result = sorted(agg.values(), key=lambda x: x["count"], reverse=True)
    for r in result:
        r["total_pnl"] = round(r["total_pnl"], 2)
    return result


def get_losing_trades_with_postmortem() -> list:
    """Closed losing trades (excl. cancelled) enriched with postmortem data."""
    rows = _rows(
        """SELECT t.id, t.stock_code, t.strategy, t.market, t.trade_type,
                  t.close_reason, t.close_pnl,
                  t.opened_at, t.closed_at, t.exp_date,
                  t.scan_source, t.wheel_type,
                  t.entry_score, t.thesis_score, t.entry_iv,
                  t.net_credit_per_spread, t.num_contracts, t.cost,
                  p.root_cause, p.root_cause_detail,
                  p.claude_analysis, p.claude_recommendations,
                  p.generated_at AS postmortem_generated_at
           FROM trades t
           LEFT JOIN postmortem p ON p.trade_id = t.id
           WHERE t.status = 'closed'
             AND t.close_pnl < 0
             AND (t.close_reason IS NULL OR t.close_reason != 'cancelled')
           ORDER BY t.closed_at DESC"""
    )
    for r in rows:
        r["category"] = _classify(r)
        if (r.get("market") or "") == "HK" and (r.get("trade_type") or "") == "stock":
            if r.get("close_pnl") is not None:
                r["close_pnl"] = round(float(r["close_pnl"]) / _HKD_USD, 2)
        if r.get("claude_recommendations"):
            try:
                r["claude_recommendations"] = json.loads(r["claude_recommendations"])
            except Exception:
                r["claude_recommendations"] = []
        oa = (r.get("opened_at") or "")[:10]
        ca = (r.get("closed_at") or "")[:10]
        r["duration_days"] = _duration_days({"opened_at": oa, "closed_at": ca})
    return rows


# ── Gem universe ───────────────────────────────────────────────────────────────

def get_today_activity() -> dict:
    """Trades opened and closed today, for the Overview 'Activity Today' section."""
    today_s = date.today().isoformat()
    rows = _rows(
        "SELECT stock_code, strategy, market, trade_type, status, scan_source, "
        "       wheel_type, opened_at, closed_at, close_pnl, close_reason "
        "FROM trades ORDER BY opened_at DESC"
    )
    opened = [r for r in rows if str(r.get("opened_at") or "").startswith(today_s)]
    closed = [r for r in rows
              if r["status"] == "closed"
              and str(r.get("closed_at") or "").startswith(today_s)
              and (r.get("close_reason") or "") != "cancelled"]
    for r in closed:
        r["close_pnl_usd"] = round(_to_usd(r.get("close_pnl") or 0, r), 2)
    return {
        "opened":        opened,
        "closed":        closed,
        "opened_count":  len(opened),
        "closed_count":  len(closed),
    }


def get_pnl_by_ticker() -> list:
    """Closed P&L grouped by stock_code, top 8 by abs(pnl), for the command center chart."""
    rows = _rows(
        "SELECT stock_code, close_pnl, market, trade_type FROM trades "
        "WHERE status='closed' AND close_pnl IS NOT NULL "
        "AND (close_reason IS NULL OR close_reason != 'cancelled')"
    )
    agg: dict = {}
    for r in rows:
        code = r["stock_code"]
        pnl  = _to_usd(float(r.get("close_pnl") or 0), r)
        agg[code] = agg.get(code, 0.0) + pnl
    result = [{"stock_code": k, "pnl": round(v, 2)} for k, v in agg.items()]
    result.sort(key=lambda x: abs(x["pnl"]), reverse=True)
    return result[:8]


def get_gem_universe(status: str = "active") -> list:
    """Return gem_universe rows for the given status (default: active)."""
    gems = _rows(
        "SELECT * FROM gem_universe WHERE status=? ORDER BY conviction DESC, added_at ASC",
        (status,),
    )
    for g in gems:
        raw = g.get("metadata")
        g["metadata"] = json.loads(raw) if raw else {}
    return gems
