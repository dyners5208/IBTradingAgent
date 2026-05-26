"""
category_tracker.py — Classify trades into the 5 strategy channels and aggregate P&L.

Pure functions only: no I/O, no moomoo API calls.
Classification is derived entirely from fields already present in every trade record.
"""
from __future__ import annotations

from datetime import date

from agent.position_manager import get_all_trades

CATEGORIES: list[str] = [
    "US Spreads",
    "Russell",
    "US Politician",
    "Wheel",
    "HK Stocks",
    "Gems",
]


def classify_trade(trade: dict) -> str:
    """Map a trade record to one of the 6 strategy categories."""
    scan_source = trade.get("scan_source", "")
    if scan_source == "russell":
        return "Russell"
    if scan_source == "politician":
        return "US Politician"
    if scan_source == "gem":
        return "Gems"

    strategy   = trade.get("strategy", "")
    wheel_type = trade.get("wheel_type", "")
    if wheel_type in ("CSP", "CC") or strategy in ("Cash-Secured Put", "Covered Call"):
        return "Wheel"

    if trade.get("market") == "HK":
        return "HK Stocks"

    return "US Spreads"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except Exception:
        return default


def compute_category_stats(
    monitor_results: list[dict] | None = None,
    pos_pnl: dict[str, float] | None = None,
    hkd_rate: float = 7.80,
) -> dict[str, dict]:
    """
    Aggregate P&L and counts by category across all trades in the log.

    Unrealized P&L resolution order (first available wins):
      1. monitor_results  — pnl from the just-completed monitor cycle (zero extra API calls)
      2. pos_pnl          — live snapshot from position_list_query (standalone command)
      3. 0.0              — fallback when no price data available

    For options unrealized P&L via pos_pnl, sums pl_val across all filled legs.
    For stocks via pos_pnl, uses the code directly from pos_pnl.

    HK Stocks monetary figures are stored in HKD; they are divided by hkd_rate so
    all returned values are in USD.

    Trades with close_reason='cancelled' are excluded from closed counts and win rate
    (they are phantom/cancelled orders, not real completed trades).

    Returns dict keyed by category name, each value a dict with:
      open, closed_alltime, wins, realized_today, realized_alltime,
      unrealized, win_rate (float or None)
    """
    today_s = date.today().isoformat()

    # Build a code → pnl lookup from monitor_results (hold-action trades only —
    # trades that were just closed have action != "hold" and their pnl is now realised)
    monitor_pnl: dict[str, float] = {}
    if monitor_results:
        for r in monitor_results:
            code = r.get("stock_code", "")
            if code and r.get("action") == "hold":
                monitor_pnl[code] = _safe_float(r.get("pnl", 0.0))

    stats: dict[str, dict] = {
        cat: {
            "open":            0,
            "closed_alltime":  0,
            "wins":            0,
            "realized_today":  0.0,
            "realized_alltime": 0.0,
            "unrealized":      0.0,
            "win_rate":        None,
        }
        for cat in CATEGORIES
    }

    for trade in get_all_trades():
        cat = classify_trade(trade)
        if cat not in stats:
            continue

        s    = stats[cat]
        code = trade.get("stock_code", "")

        if trade.get("status") == "open":
            s["open"] += 1

            # Unrealized P&L: monitor_results → pos_pnl → 0
            unreal = 0.0
            if code in monitor_pnl:
                unreal = monitor_pnl[code]
            elif pos_pnl is not None:
                if code in pos_pnl:
                    unreal = pos_pnl[code]
                elif trade.get("trade_type") == "options":
                    # Sum leg-level pl_val for available legs.
                    # Using available (not all-or-nothing) handles partial fills —
                    # unfilled protection legs absent from pos_pnl should not zero
                    # out the filled anchor leg's unrealized P&L.
                    leg_vals = [
                        pos_pnl.get(leg.get("code", ""))
                        for leg in trade.get("legs", [])
                    ]
                    available = [v for v in leg_vals if v is not None]
                    if available:
                        unreal = sum(available)
            s["unrealized"] += unreal

        elif trade.get("status") == "closed":
            # Skip cancelled trades — they are phantom/voided orders, not real closes
            if trade.get("close_reason") == "cancelled":
                continue
            s["closed_alltime"] += 1
            pnl = _safe_float(trade.get("close_pnl") or 0.0)
            s["realized_alltime"] += pnl
            if pnl > 0:
                s["wins"] += 1
            closed_at = str(trade.get("closed_at", ""))
            if closed_at.startswith(today_s):
                s["realized_today"] += pnl

    # Convert HK Stocks monetary values from HKD to USD
    hk = stats.get("HK Stocks")
    if hk and hkd_rate and hkd_rate != 1.0:
        hk["realized_today"]   /= hkd_rate
        hk["realized_alltime"] /= hkd_rate
        hk["unrealized"]       /= hkd_rate

    # Gems can include HK-market stocks whose P&L is also stored in HKD.
    # We need per-trade conversion so we re-derive it from the trade log.
    gems = stats.get("Gems")
    if gems and hkd_rate and hkd_rate != 1.0:
        hk_open_unreal = 0.0
        hk_real_today  = 0.0
        hk_real_all    = 0.0
        for trade in get_all_trades():
            if classify_trade(trade) != "Gems":
                continue
            if trade.get("market") != "HK":
                continue
            if trade.get("status") == "open":
                code = trade.get("stock_code", "")
                raw  = monitor_pnl.get(code, pos_pnl.get(code, 0.0) if pos_pnl else 0.0)
                hk_open_unreal += raw
            elif trade.get("status") == "closed":
                if trade.get("close_reason") == "cancelled":
                    continue
                pnl = _safe_float(trade.get("close_pnl") or 0.0)
                hk_real_all += pnl
                if str(trade.get("closed_at", "")).startswith(today_s):
                    hk_real_today += pnl
        # Subtract HKD amounts already summed, then add back the USD-converted amounts
        gems["unrealized"]       -= hk_open_unreal
        gems["realized_alltime"] -= hk_real_all
        gems["realized_today"]   -= hk_real_today
        gems["unrealized"]       += hk_open_unreal / hkd_rate
        gems["realized_alltime"] += hk_real_all    / hkd_rate
        gems["realized_today"]   += hk_real_today  / hkd_rate

    # Compute win_rate from wins / closed_alltime
    for s in stats.values():
        total = s["closed_alltime"]
        s["win_rate"] = (s["wins"] / total) if total > 0 else None

    return stats


def compute_monthly_trend(
    all_trades: list[dict] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Pivot realized P&L by calendar month and category.

    Returns: {
        "2025-03": {"US Spreads": 1240.0, "Russell": 580.0, ...},
        "2025-04": {...},
        ...
    }
    Months are ISO year-month strings, sorted ascending.
    Only includes months that have at least one closed trade.
    """
    if all_trades is None:
        all_trades = get_all_trades()

    by_month: dict[str, dict[str, float]] = {}

    for trade in all_trades:
        if trade.get("status") != "closed":
            continue
        closed_at = str(trade.get("closed_at", ""))
        if len(closed_at) < 7:
            continue
        month = closed_at[:7]   # "YYYY-MM"
        cat   = classify_trade(trade)
        pnl   = _safe_float(trade.get("close_pnl", 0.0))

        if month not in by_month:
            by_month[month] = {c: 0.0 for c in CATEGORIES}
        by_month[month][cat] = round(by_month[month].get(cat, 0.0) + pnl, 2)

    return dict(sorted(by_month.items()))
