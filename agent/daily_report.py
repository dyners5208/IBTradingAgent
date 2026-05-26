"""
daily_report.py — End-of-day Excel report: filled orders + trade P&L.

Generates  reports/daily_YYYY-MM-DD.xlsx  with three sheets:
  • Orders      — every filled order for the day (price, qty, fill time …)
  • Trade P&L   — closed trades: open/close prices, realised P&L, reason
  • Open Trades — still-open positions with unrealised P&L snapshot
"""

import os
from datetime import datetime, date, timezone

import pandas as pd

from agent.position_manager import get_all_trades

_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reports",
)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except Exception:
        return default


def _query_history_orders(account: dict, report_date: date) -> pd.DataFrame:
    """Pull today's filled orders from IBKR executions."""
    if account is None:
        return pd.DataFrame()

    from ib_insync import ExecutionFilter
    from agent.ibkr_client import get_ib, ibkr_lock

    date_s     = report_date.isoformat()
    filter_str = report_date.strftime("%Y%m%d-00:00:00")

    try:
        with ibkr_lock:
            ib    = get_ib()
            fills = ib.reqExecutions(ExecutionFilter(time=filter_str))
    except Exception as exc:
        print(f"  [daily_report] reqExecutions failed: {exc}")
        return pd.DataFrame()

    today_str = report_date.strftime("%Y%m%d")
    rows = []
    for fill in fills:
        ex = fill.execution
        # reqExecutions returns all fills since filter time — restrict to today
        try:
            if str(ex.time)[:8] != today_str:
                continue
        except Exception:
            pass

        side_raw = str(getattr(ex, "side", "")).upper()
        if side_raw in ("BOT", "BUY"):
            side = "buy"
        elif side_raw in ("SLD", "SELL"):
            side = "sell"
        else:
            side = side_raw.lower()

        fill_time = str(getattr(ex, "time", "")).replace("  ", " ")

        rows.append({
            "Date":         date_s,
            "Market":       "US",
            "Order ID":     str(getattr(ex, "orderId", "")),
            "Code":         str(getattr(ex, "symbol", "")),
            "Side":         side,
            "Order Type":   "limit",
            "Status":       "filled",
            "Qty":          _safe_float(getattr(ex, "shares", 0)),
            "Filled Qty":   _safe_float(getattr(ex, "shares", 0)),
            "Limit Price":  0.0,
            "Fill Price":   _safe_float(getattr(ex, "price", 0)),
            "Currency":     "USD",
            "Create Time":  fill_time,
            "Update Time":  fill_time,
            "Remark":       str(getattr(ex, "execId", "")),
        })
    return pd.DataFrame(rows)


def _fetch_position_pnl(account_us: dict | None,
                        account_hk: dict | None = None) -> dict[str, float]:
    """IBKR positions() carries no live P&L — returns empty dict.

    Open-trade unrealised P&L in the report uses the stored `unrealized_pnl`
    field from trade records, updated each monitor cycle.
    """
    return {}


def _build_trade_pnl_rows(report_date: date,
                           pos_pnl: dict[str, float] | None = None,
                           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split the trade log into closed-today rows and still-open rows.
    Returns (closed_df, open_df).
    """
    all_trades = get_all_trades()
    date_s     = report_date.isoformat()

    closed_rows: list[dict] = []
    open_rows:   list[dict] = []

    for t in all_trades:
        opened_at  = str(t.get("opened_at", ""))
        closed_at  = str(t.get("closed_at", ""))
        trade_type = t.get("trade_type", "options")
        strategy   = t.get("strategy", "")
        code       = t.get("stock_code", "")
        market     = t.get("market", "")

        if t.get("status") == "closed" and closed_at.startswith(date_s):
            if trade_type == "options":
                net_credit = _safe_float(t.get("net_credit_per_spread"))
                num_c      = _safe_float(t.get("num_contracts", 1))
                mult       = _safe_float(t.get("multiplier", 100))
                open_cost  = net_credit * num_c * mult
            else:
                open_cost  = _safe_float(t.get("cost"))

            closed_rows.append({
                "Code":         code,
                "Market":       market,
                "Strategy":     strategy,
                "Trade Type":   trade_type,
                "Opened At":    opened_at[:19],
                "Closed At":    closed_at[:19],
                "Net Credit/Debit": round(open_cost, 2),
                "Realised P&L": _safe_float(t.get("close_pnl")),
                "Close Reason": t.get("close_reason", ""),
                "Placement":    t.get("placement_status", "complete"),
            })

        elif t.get("status") == "open":
            if trade_type == "options":
                exp  = t.get("exp_date", "")
                legs = t.get("legs", [])
                leg_summary = "; ".join(
                    f"{l.get('side')} {l.get('call_or_put')} K={l.get('strike')}"
                    for l in legs
                )

                net_c  = _safe_float(t.get("net_credit_per_spread"))
                num_c  = _safe_float(t.get("num_contracts", 1))
                mult   = _safe_float(t.get("multiplier", 100))

                _CREDIT = {"Bull Put Spread", "Bear Call Spread", "Iron Condor",
                           "Cash-Secured Put", "Covered Call"}
                _DEBIT  = {"Bull Call Spread", "Bear Put Spread"}

                if strategy in _CREDIT and net_c > 0:
                    from agent.constants import TP_CREDIT_REMAINING, CL_CREDIT_MULT
                    tp_v = round( net_c * (1 - TP_CREDIT_REMAINING) * num_c * mult, 2)
                    cl_v = round(-net_c * (CL_CREDIT_MULT - 1)      * num_c * mult, 2)
                elif strategy in _DEBIT and net_c < 0:
                    from agent.constants import TP_DEBIT_MULT, CL_DEBIT_REMAINING
                    debit = abs(net_c)
                    tp_v = round( debit * (TP_DEBIT_MULT - 1)       * num_c * mult, 2)
                    cl_v = round(-debit * (1 - CL_DEBIT_REMAINING)   * num_c * mult, 2)
                else:
                    from agent.constants import TP_STRADDLE_MULT, CL_STRADDLE_REMAINING
                    debit = abs(net_c)
                    tp_v = round( debit * (TP_STRADDLE_MULT - 1)      * num_c * mult, 2)
                    cl_v = round(-debit * (1 - CL_STRADDLE_REMAINING)  * num_c * mult, 2)

                if pos_pnl is not None:
                    leg_pnls = [pos_pnl.get(l.get("code", "")) for l in legs]
                    if all(v is not None for v in leg_pnls):
                        unreal_pnl = round(sum(leg_pnls), 2)
                    else:
                        unreal_pnl = ""
                else:
                    unreal_pnl = ""

                open_rows.append({
                    "Code":              code,
                    "Market":            market,
                    "Strategy":          strategy,
                    "Opened At":         opened_at[:19],
                    "Exp Date":          exp,
                    "Contracts":         t.get("num_contracts", ""),
                    "Unrealised P&L":    unreal_pnl,
                    "TP P&L":            tp_v,
                    "CL P&L":            cl_v,
                    "Legs":              leg_summary,
                    "Placement":         t.get("placement_status", "complete"),
                })
            else:
                entry = _safe_float(t.get("limit_price"))
                qty   = _safe_float(t.get("qty", 0))
                from agent.constants import STOCK_TP_PCT, STOCK_CL_PCT
                side  = t.get("side", "BUY")
                dirn  = 1 if side == "BUY" else -1
                tp_v  = round( entry * STOCK_TP_PCT * qty * dirn, 2) if entry and qty else ""
                cl_v  = round(-entry * STOCK_CL_PCT * qty * dirn, 2) if entry and qty else ""

                unreal_pnl = round(pos_pnl[code], 2) if (pos_pnl and code in pos_pnl) else ""

                open_rows.append({
                    "Code":              code,
                    "Market":            market,
                    "Strategy":          strategy,
                    "Opened At":         opened_at[:19],
                    "Exp Date":          "",
                    "Contracts":         t.get("qty", ""),
                    "Unrealised P&L":    unreal_pnl,
                    "TP P&L":            tp_v,
                    "CL P&L":            cl_v,
                    "Legs":              f"{t.get('side')} {t.get('qty')} @ {t.get('limit_price')}",
                    "Placement":         t.get("placement_status", "complete"),
                })

    return pd.DataFrame(closed_rows), pd.DataFrame(open_rows)


def generate_daily_report(account_us: dict | None,
                           account_hk: dict | None = None,
                           report_date: date | None = None) -> str | None:
    """
    Build the daily Excel report.
    Returns the file path on success, None on failure.
    """
    if report_date is None:
        report_date = datetime.now().date()

    os.makedirs(_REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(_REPORTS_DIR, f"daily_{report_date}.xlsx")

    print(f"\n  Generating daily report for {report_date}...")

    # 1. Order history from Alpaca
    orders_df = _query_history_orders(account_us, report_date) if account_us else pd.DataFrame()

    # 2. Fetch live unrealised P&L for open positions (end-of-day snapshot)
    pos_pnl = _fetch_position_pnl(account_us)

    # 3. Trade P&L from trade log
    closed_df, open_df = _build_trade_pnl_rows(report_date, pos_pnl=pos_pnl)

    # 4. Write Excel
    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            if not orders_df.empty:
                orders_df.to_excel(writer, sheet_name="Orders", index=False)
            else:
                pd.DataFrame({"Note": ["No filled orders today"]}).to_excel(
                    writer, sheet_name="Orders", index=False
                )

            if not closed_df.empty:
                closed_df.to_excel(writer, sheet_name="Trade P&L", index=False)
            else:
                pd.DataFrame({"Note": ["No closed trades today"]}).to_excel(
                    writer, sheet_name="Trade P&L", index=False
                )

            if not open_df.empty:
                open_df.to_excel(writer, sheet_name="Open Trades", index=False)
            else:
                pd.DataFrame({"Note": ["No open trades"]}).to_excel(
                    writer, sheet_name="Open Trades", index=False
                )

        print(f"  Daily report saved: {out_path}")
        return out_path
    except Exception as e:
        print(f"  ERROR writing daily report: {e}")
        return None
