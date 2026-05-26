"""
Trade monitor — IBKR version.

Checks all open trades for P&L thresholds, theta decay, and thesis changes.
Uses ib_insync for all broker calls; all IB calls serialized through ibkr_lock.

Key IBKR differences vs Alpaca version:
- Order IDs are integers; legacy UUID strings fall back to position-based detection.
- ib.positions() carries no live P&L — pos_pnl_map always has pnl=0.0.
- Spread close uses a BAG/ComboLeg contract; single-leg uses Option + LimitOrder.
- Snap map built via ib.reqTickers() instead of Alpaca data clients.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from agent.constants import (
    THETA_EXIT_DTE_DEBIT, THETA_ROLL_DTE_CREDIT,
    TP_CREDIT_REMAINING, TP_DEBIT_MULT, TP_STRADDLE_MULT,
    CL_CREDIT_MULT, CL_DEBIT_REMAINING, CL_STRADDLE_REMAINING,
    STOCK_TP_PCT, STOCK_CL_PCT, STOCK_TRAIL_ACTIVATION_PCT, STOCK_TRAIL_PCT,
    CSP_MIN_COMPOSITE_SCORE, US_SPREAD_MIN_SIGNAL,
    INTRADAY_CHECK_ENABLED, INTRADAY_MIN_MOVE_PCT, INTRADAY_COOLDOWN_MINUTES,
    INTRADAY_GATE_MIN_BARS, INTRADAY_GATE_RSI_PERIOD,
    INTRADAY_GATE_RSI_BULL_FLOOR, INTRADAY_GATE_RSI_BEAR_CEILING,
    INTRADAY_GATE_VWAP_PCT_THRESHOLD,
    ORDER_TIMEOUT_MINS_SINGLE, ORDER_TIMEOUT_MINS_SPREAD, ORDER_RETRY_SECONDS,
    IBKR_OPT_TICKER_TIMEOUT,
)
from agent.position_manager import get_open_trades, close_trade, update_trade
from agent.market_hours import is_us_market_open, is_hk_market_open, market_today
from agent.data_fetcher import fetch_kline, fetch_intraday_kline, _to_ibkr_stock
from agent.scorer import score_stock
from agent.alert_log import log_alert
from agent.order_executor import wait_for_fill, cancel_order

_CREDIT_STRATEGIES = {"Bull Put Spread", "Bear Call Spread", "Iron Condor",
                      "Cash-Secured Put", "Covered Call"}
_DEBIT_STRATEGIES  = {"Bull Call Spread", "Bear Put Spread"}
_STRADDLE          = {"Long Straddle"}

_FILLED   = "filled"
_TERMINAL = frozenset({"canceled", "expired", "replaced", "done_for_day",
                        "rejected", "suspended"})

_IC_DIRECTION_THRESHOLD = 0.25
_BULLISH_STRATEGIES = {"Bull Put Spread", "Bull Call Spread"}
_BEARISH_STRATEGIES = {"Bear Call Spread", "Bear Put Spread"}
_WHEEL_STRATEGIES   = {"Cash-Secured Put", "Covered Call"}

# Background close thread tracking
_active_close_threads: dict[str, threading.Thread] = {}
_close_threads_lock   = threading.Lock()

# Per-stock cooldown for intraday check
_intraday_checked: dict[str, datetime] = {}


# ── IBKR helpers ───────────────────────────────────────────────────────────────

def _map_ibkr_status(status: str) -> str:
    """Map IBKR order status string to internal status label."""
    s = status.lower()
    if s == "filled":
        return "filled"
    if s in ("submitted", "presubmitted", "pendingsubmit", "pendingcancel"):
        return "pending_new"
    if s in ("cancelled", "apicancelled", "inactive"):
        return "canceled"
    return s


# ── Fill detection ─────────────────────────────────────────────────────────────

def _get_fill_details(trade: dict,
                      pos_pnl_map: dict | None = None) -> dict:
    """Query IBKR for order fill status.

    For BAG options trades, all legs share a single order_id — the spread is
    atomic (fills all or none). For legacy Alpaca UUID order IDs, falls back
    to position-map inference.

    Returns dict: all_filled, any_filled, all_dead, is_partial,
                  effective_qty, legs_detail, summary.
    """
    from agent.ibkr_client import get_ib, ibkr_lock

    if trade.get("trade_type") == "options":
        oids = list({str(l["order_id"]) for l in trade.get("legs", [])
                     if l.get("order_id")})
    else:
        oid = trade.get("order_id")
        oids = [str(oid)] if oid else []

    if not oids:
        return {
            "all_filled": True, "any_filled": True, "all_dead": False,
            "is_partial":  False,
            "effective_qty": trade.get("num_contracts", trade.get("qty", 1)),
            "legs_detail": [], "summary": "no order IDs (assumed filled)",
        }

    placement_complete = trade.get("placement_status") == "complete"
    qty_expected = trade.get("num_contracts", trade.get("qty", 1))

    # Fetch IBKR open trades and fills once
    try:
        with ibkr_lock:
            ib           = get_ib()
            ibkr_open    = ib.openTrades()
            ibkr_fills   = ib.fills()
    except Exception as exc:
        print(f"  [monitor] _get_fill_details: IBKR query failed: {exc}")
        ibkr_open  = []
        ibkr_fills = []

    legs_detail: list[dict] = []

    for oid_str in set(oids):
        # Try to parse as IBKR integer order ID
        try:
            oid_int = int(oid_str)
        except (ValueError, TypeError):
            oid_int = None  # Legacy Alpaca UUID — not searchable in IBKR

        status_found = None
        fill_price   = 0.0
        filled_qty   = qty_expected

        if oid_int is not None:
            # Check active open trades
            for trd in ibkr_open:
                if trd.order.orderId == oid_int:
                    raw          = trd.orderStatus.status
                    filled_qty   = int(trd.orderStatus.filled) or qty_expected
                    fill_price   = float(trd.orderStatus.avgFillPrice or 0)
                    status_found = _map_ibkr_status(raw)
                    break

            if status_found is None:
                # Check completed fills (today's session)
                total_qty = 0.0
                total_val = 0.0
                for fill in ibkr_fills:
                    if fill.execution.orderId == oid_int:
                        q          = float(fill.execution.shares)
                        total_qty += q
                        total_val += q * float(fill.execution.price)
                if total_qty > 0:
                    fill_price   = total_val / total_qty
                    filled_qty   = int(total_qty)
                    status_found = _FILLED

        if status_found is not None:
            legs_detail.append({
                "order_id": oid_str, "code": "", "side": "",
                "status":         status_found,
                "filled_qty":     filled_qty,
                "avg_fill_price": fill_price,
            })
        elif placement_complete:
            # Order aged out of IBKR history — trade was already marked complete
            legs_detail.append({
                "order_id": oid_str, "code": "", "side": "", "status": _FILLED,
                "filled_qty": qty_expected, "avg_fill_price": 0.0,
            })
        elif pos_pnl_map is not None:
            leg_codes = {l["code"] for l in trade.get("legs", []) if l.get("code")}
            stock_in  = trade.get("stock_code", "") in pos_pnl_map
            if (leg_codes and not leg_codes.isdisjoint(pos_pnl_map)) or stock_in:
                legs_detail.append({
                    "order_id": oid_str, "code": "", "side": "", "status": _FILLED,
                    "filled_qty": qty_expected, "avg_fill_price": 0.0,
                })
            else:
                legs_detail.append({
                    "order_id": oid_str, "code": "", "side": "",
                    "status": "query_failed", "filled_qty": 0, "avg_fill_price": 0.0,
                })
        else:
            legs_detail.append({
                "order_id": oid_str, "code": "", "side": "",
                "status": "query_failed", "filled_qty": 0, "avg_fill_price": 0.0,
            })

    filled  = [l for l in legs_detail if l["status"] == _FILLED]
    dead    = [l for l in legs_detail if l["status"] in _TERMINAL]
    all_filled  = len(filled) > 0 and len(filled) == len(legs_detail)
    any_filled  = len(filled) > 0
    all_dead    = len(dead) == len(legs_detail) and len(legs_detail) > 0
    effective_q = legs_detail[0]["filled_qty"] if filled else 0

    # If every leg shows query_failed AND pos_pnl_map is available (IBKR connection
    # is healthy), the order was rejected/cancelled by the broker and left no
    # position. Treat as terminal so the monitor auto-closes the trade record.
    if (not all_dead and not all_filled
            and pos_pnl_map is not None
            and legs_detail
            and all(l["status"] == "query_failed" for l in legs_detail)):
        all_dead = True
        for l in legs_detail:
            l["status"] = "canceled"

    if all_filled:
        summary = "filled"
    elif all_dead:
        summary = "canceled/expired"
    else:
        statuses = {l["status"] for l in legs_detail}
        summary  = f"awaiting fill: {statuses}"

    return {
        "all_filled":    all_filled,
        "any_filled":    any_filled,
        "all_dead":      all_dead,
        "is_partial":    False,   # IBKR BAG orders are atomic
        "effective_qty": effective_q,
        "legs_detail":   legs_detail,
        "summary":       summary,
    }


# ── Position map ───────────────────────────────────────────────────────────────

def _fetch_pos_pnl_map() -> dict[str, dict] | None:
    """Return {symbol: {"pnl": float, "current_price": float}} from IBKR positions.

    IBKR ib.positions() does not provide live P&L — pnl is always 0.0.
    The caller must guard P&L overrides with `if v["pnl"] != 0.0`.
    Returns None on API error, empty dict when account has no open positions.
    """
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.options_analyzer import _to_occ_symbol
    from datetime import date as _date

    try:
        with ibkr_lock:
            ib        = get_ib()
            positions = ib.positions()
    except Exception as exc:
        print(f"  [monitor] _fetch_pos_pnl_map error: {exc}")
        return None

    if not positions:
        return {}

    result: dict[str, dict] = {}
    for pos in positions:
        con = pos.contract
        try:
            if con.secType == "OPT":
                exp_str  = con.lastTradeDateOrContractMonth
                exp_date = _date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
                key      = _to_occ_symbol(con.symbol, exp_date, con.right, float(con.strike))
            elif con.exchange == "SEHK" or con.currency == "HKD":
                key = f"HK.{con.symbol}"
            else:
                key = con.symbol
            result[key] = {"pnl": 0.0, "current_price": 0.0}
        except Exception:
            pass

    return result


# ── Snap map ───────────────────────────────────────────────────────────────────

def _build_snap_map(open_trades: list[dict]) -> dict:
    """Pre-fetch current prices for all stocks and option legs via IBKR reqTickers.

    Returns {symbol: {"bid": float, "ask": float, "last": float,
                       "theta": float, "change_rate": None}}.
    """
    from ib_insync import Stock, Option
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.options_analyzer import _parse_occ

    snap_map: dict[str, dict] = {}

    stock_tickers = list({t["stock_code"] for t in open_trades})
    opt_codes     = list({
        l["code"] for t in open_trades
        for l in t.get("legs", []) if l.get("code")
    })

    # ── Stock quotes ──────────────────────────────────────────────────────────
    if stock_tickers:
        stock_cons = [_to_ibkr_stock(tkr) for tkr in stock_tickers]
        try:
            with ibkr_lock:
                ib         = get_ib()
                ib.qualifyContracts(*stock_cons)
                stk_result = ib.reqTickers(*stock_cons)
                ib.sleep(2)
            with ibkr_lock:
                ib2 = get_ib()
                for con in stock_cons:
                    try:
                        ib2.cancelMktData(con)
                    except Exception:
                        pass
            # Build conId → ticker map for safe matching
            cid_to_tkr = {con.conId: tkr for tkr, con in zip(stock_tickers, stock_cons)}
            for t in stk_result:
                if not t.contract:
                    continue
                tkr = cid_to_tkr.get(t.contract.conId)
                if tkr is None:
                    continue
                bid  = float(t.bid)  if t.bid  and t.bid  > 0 else 0.0
                ask  = float(t.ask)  if t.ask  and t.ask  > 0 else 0.0
                last = float(t.last) if t.last and t.last > 0 else 0.0
                snap_map[tkr] = {
                    "bid":         bid,
                    "ask":         ask,
                    "last":        last or ask or bid,
                    "theta":       0.0,
                    "change_rate": None,
                }
        except Exception as exc:
            print(f"  [monitor] stock quote fetch error: {exc}")

    # ── Option quotes (batch by 50) ───────────────────────────────────────────
    # Build a conId lookup from stored leg conids to avoid unnecessary qualifies
    stored_conid: dict[str, int] = {}
    for t in open_trades:
        for leg in t.get("legs", []):
            code  = leg.get("code", "")
            conid = int(leg.get("conid", 0) or 0)
            if code and conid > 0:
                stored_conid[code] = conid

    BATCH = 50
    for i in range(0, len(opt_codes), BATCH):
        batch_codes = opt_codes[i:i + BATCH]
        opt_cons:    list[Option] = []
        valid_codes: list[str]   = []

        for code in batch_codes:
            parsed = _parse_occ(code)
            if not parsed:
                continue
            exp_str = parsed["exp_date"].strftime("%Y%m%d")
            con     = Option(parsed["ticker"], exp_str,
                             parsed["strike"], parsed["call_or_put"], "SMART")
            cid = stored_conid.get(code, 0)
            if cid > 0:
                con.conId = cid
            opt_cons.append(con)
            valid_codes.append(code)

        if not opt_cons:
            continue

        try:
            with ibkr_lock:
                ib         = get_ib()
                ib.qualifyContracts(*opt_cons)
                opt_result = ib.reqTickers(*opt_cons)
                ib.sleep(IBKR_OPT_TICKER_TIMEOUT)
            with ibkr_lock:
                ib2 = get_ib()
                for con in opt_cons:
                    try:
                        ib2.cancelMktData(con)
                    except Exception:
                        pass
            cid_to_code = {con.conId: code for code, con in zip(valid_codes, opt_cons)}
            for t in opt_result:
                if not t.contract:
                    continue
                code = cid_to_code.get(t.contract.conId)
                if code is None:
                    continue
                bid   = float(t.bid)  if t.bid  and t.bid  > 0 else 0.0
                ask   = float(t.ask)  if t.ask  and t.ask  > 0 else 0.0
                g     = t.modelGreeks
                theta = float(g.theta) if g and g.theta is not None else 0.0
                snap_map[code] = {
                    "bid":         bid,
                    "ask":         ask,
                    "last":        (bid + ask) / 2 if (bid > 0 and ask > 0) else (ask or bid),
                    "theta":       theta,
                    "change_rate": None,
                }
        except Exception as exc:
            print(f"  [monitor] option quote fetch error (batch {i}): {exc}")

    return snap_map


# ── Helpers ────────────────────────────────────────────────────────────────────

def _days_to_expiry(exp_date_str: str) -> int:
    try:
        exp      = datetime.strptime(str(exp_date_str)[:10], "%Y-%m-%d")
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        return max((exp - now_naive).days, 0)
    except Exception:
        return 999


def _current_leg_value(leg: dict, snap_map: dict | None = None) -> float | None:
    """Get current mid-price for an option leg."""
    code = leg.get("code", "")
    if snap_map and code in snap_map:
        s   = snap_map[code]
        bid = float(s.get("bid", 0))
        ask = float(s.get("ask", 0))
        if bid > 0 or ask > 0:
            return (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)

    # Fallback: individual IBKR call
    from ib_insync import Option
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.options_analyzer import _parse_occ

    parsed = _parse_occ(code)
    if not parsed:
        return None
    try:
        exp_str = parsed["exp_date"].strftime("%Y%m%d")
        con     = Option(parsed["ticker"], exp_str,
                         parsed["strike"], parsed["call_or_put"], "SMART")
        cid = int(leg.get("conid", 0) or 0)
        if cid > 0:
            con.conId = cid
        with ibkr_lock:
            ib       = get_ib()
            ib.qualifyContracts(con)
            tickers  = ib.reqTickers(con)
            ib.sleep(IBKR_OPT_TICKER_TIMEOUT)
            try:
                ib.cancelMktData(con)
            except Exception:
                pass
        if tickers:
            t   = tickers[0]
            bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
            ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
            if bid > 0 or ask > 0:
                return (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)
    except Exception:
        pass
    return None


def _current_theta(leg: dict, snap_map: dict | None = None) -> float:
    code = leg.get("code", "")
    if snap_map and code in snap_map:
        return float(snap_map[code].get("theta", 0))
    return 0.0


def _update_trailing_high(trade: dict, high: float) -> None:
    if high > 0:
        update_trade(trade["stock_code"], {"_trailing_high": round(high, 4)})


# ── Stock trade evaluation ─────────────────────────────────────────────────────

def _evaluate_stock_trade(trade: dict, fill_details: dict | None,
                           snap_map: dict | None = None,
                           pos_pnl_entry: dict | None = None) -> dict:
    """P&L evaluation for a plain stock trade."""
    code       = trade["stock_code"]
    side       = trade.get("side", "BUY")
    is_partial = fill_details.get("is_partial", False) if fill_details else False

    entry_price = 0.0
    actual_qty  = trade.get("qty", 1)
    if fill_details and fill_details["legs_detail"]:
        ld = fill_details["legs_detail"][0]
        entry_price = ld.get("avg_fill_price", 0.0)
        if fill_details["effective_qty"] > 0:
            actual_qty = fill_details["effective_qty"]
    if entry_price <= 0:
        entry_price = trade.get("limit_price", 0.0)

    if entry_price <= 0:
        return {"action": "hold", "pnl": 0, "dte": 999, "is_partial": is_partial,
                "reason": "No valid entry price stored"}

    current_price = 0.0
    if snap_map and code in snap_map:
        s = snap_map[code]
        current_price = float(s.get("last") or s.get("ask") or s.get("bid") or 0)
    # pos_pnl_entry.current_price is always 0.0 from IBKR — skip as fallback
    if current_price <= 0:
        return {"action": "hold", "pnl": 0, "dte": 999, "is_partial": is_partial,
                "reason": "Could not fetch current price"}

    direction = 1 if side == "BUY" else -1
    pnl     = (current_price - entry_price) * actual_qty * direction
    pnl_pct = (current_price - entry_price) / entry_price * direction
    partial_note = " [PARTIAL]" if is_partial else ""

    trail_high = float(trade.get("_trailing_high") or 0.0)
    if pnl_pct >= STOCK_TRAIL_ACTIVATION_PCT:
        trail_high = max(trail_high, current_price)

    trail_stop_price = max(entry_price, trail_high * (1.0 - STOCK_TRAIL_PCT)) if trail_high > 0 else 0.0
    trail_active     = trail_high >= entry_price * (1.0 + STOCK_TRAIL_ACTIVATION_PCT)
    trail_triggered  = trail_active and (current_price <= trail_stop_price)

    if pnl_pct <= -STOCK_CL_PCT:
        _update_trailing_high(trade, trail_high)
        return {"action": "cut_loss", "pnl": pnl, "dte": 999, "is_partial": is_partial,
                "reason": f"P&L={pnl:+.2f} ({pnl_pct*100:+.1f}%) <= CL -{STOCK_CL_PCT*100:.0f}%{partial_note}"}

    if pnl_pct >= STOCK_TP_PCT:
        _update_trailing_high(trade, trail_high)
        return {"action": "take_profit", "pnl": pnl, "dte": 999, "is_partial": is_partial,
                "reason": f"P&L={pnl:+.2f} ({pnl_pct*100:+.1f}%) >= TP +{STOCK_TP_PCT*100:.0f}%{partial_note}"}

    if trail_triggered:
        _update_trailing_high(trade, trail_high)
        return {"action": "trail_stop", "pnl": pnl, "dte": 999, "is_partial": is_partial,
                "reason": (f"Price {current_price:.3f} <= trail stop {trail_stop_price:.3f}"
                           f"  P&L={pnl:+.2f} ({pnl_pct*100:+.1f}%){partial_note}")}

    _update_trailing_high(trade, trail_high)
    return {"action": "hold", "pnl": pnl, "dte": 999, "is_partial": is_partial,
            "reason": f"Within bounds{partial_note}. P&L={pnl:+.2f}  entry={entry_price:.3f}  now={current_price:.3f}",
            "trail_active": trail_active, "trail_high": trail_high, "trail_stop": trail_stop_price}


# ── Close orders ───────────────────────────────────────────────────────────────

def close_stock_trade(trade: dict, reason: str) -> tuple[bool, int | str]:
    """Place a limit order to close a stock position. Returns (success, order_id)."""
    from ib_insync import Stock, LimitOrder
    from agent.ibkr_client import get_ib, ibkr_lock

    code      = trade["stock_code"]
    qty       = trade.get("qty", 1)
    open_side = trade.get("side", "BUY")

    lim = float(trade.get("limit_price", 0) or 0)
    try:
        stock_con = _to_ibkr_stock(code)
        with ibkr_lock:
            ib         = get_ib()
            ib.qualifyContracts(stock_con)
            stk_ticks  = ib.reqTickers(stock_con)
            ib.sleep(1)
            try:
                ib.cancelMktData(stock_con)
            except Exception:
                pass
        if stk_ticks:
            t   = stk_ticks[0]
            bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
            ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
            if open_side == "BUY":
                lim = round(bid, 3) if bid > 0 else lim   # selling: take bid
            else:
                lim = round(ask, 3) if ask > 0 else lim   # buying back: take ask
    except Exception:
        pass

    close_side = "SELL" if open_side == "BUY" else "BUY"
    try:
        stock_con = _to_ibkr_stock(code)
        order     = LimitOrder(action=close_side, totalQuantity=qty,
                               lmtPrice=round(max(lim, 0.01), 2), tif="DAY")
        with ibkr_lock:
            ib        = get_ib()
            ib.qualifyContracts(stock_con)
            trade_obj = ib.placeOrder(stock_con, order)
        order_id = trade_obj.order.orderId
        print(f"    Close {close_side} {qty}x {code} @ {lim:.3f}  "
              f"order_id={order_id}  ({reason})")
        return True, order_id
    except Exception as exc:
        print(f"    close_stock_trade({code}) FAILED: {exc}")
        return False, 0


def _cancel_and_reprice_stock_close(trade: dict, reason: str) -> None:
    """Cancel the existing stock close order and reprice at fresh taker price."""
    old_oid = trade.get("close_order_id")
    if old_oid is not None and str(old_oid) not in ("0", "None", ""):
        cancel_order(old_oid)
        print(f"    {trade['stock_code']}: prior close order {old_oid} cancelled — repricing")

    ok, new_oid = close_stock_trade(trade, reason)
    if ok:
        update_trade(trade["stock_code"], {"close_order_id": new_oid})
    else:
        print(f"    {trade['stock_code']}: reprice failed — will retry next cycle")


def close_spread(trade: dict, reason: str) -> bool:
    """Close all legs of a spread with a single IBKR BAG order (multi-leg)
    or a plain LimitOrder (single-leg CSP/CC).

    Returns True if the close order fills before the timeout.
    """
    from ib_insync import Option, Contract, ComboLeg, LimitOrder
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.options_analyzer import _parse_occ

    ticker = trade["stock_code"]
    legs   = trade.get("legs", [])
    num_c  = trade.get("num_contracts", 1)

    if not legs:
        return False

    # Build Option contracts from leg data; use stored conId when available
    leg_cons: list[tuple[dict, Option]] = []
    for leg in legs:
        parsed = _parse_occ(leg["code"])
        if not parsed:
            print(f"    close_spread: cannot parse OCC {leg['code']}")
            return False
        exp_str = parsed["exp_date"].strftime("%Y%m%d")
        con     = Option(parsed["ticker"], exp_str,
                         parsed["strike"], parsed["call_or_put"], "SMART")
        cid = int(leg.get("conid", 0) or 0)
        if cid > 0:
            con.conId = cid
        leg_cons.append((leg, con))

    # Qualify any contracts whose conId was not stored
    need_qualify = [con for _, con in leg_cons if not getattr(con, "conId", 0)]
    if need_qualify:
        try:
            with ibkr_lock:
                ib = get_ib()
                ib.qualifyContracts(*need_qualify)
        except Exception as exc:
            print(f"    close_spread: qualify failed: {exc}")

    # Fetch live bid/ask for pricing
    all_cons = [con for _, con in leg_cons]
    live_prices: dict[str, tuple[float, float]] = {}
    try:
        with ibkr_lock:
            ib         = get_ib()
            tik_result = ib.reqTickers(*all_cons)
            ib.sleep(2)
        with ibkr_lock:
            ib2 = get_ib()
            for con in all_cons:
                try:
                    ib2.cancelMktData(con)
                except Exception:
                    pass
        cid_to_leg = {con.conId: leg for leg, con in leg_cons}
        for t in tik_result:
            if not t.contract:
                continue
            leg = cid_to_leg.get(t.contract.conId)
            if leg is None:
                continue
            bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
            ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
            live_prices[leg["code"]] = (bid, ask)
    except Exception as exc:
        print(f"    close_spread: price fetch error: {exc}")

    def _close_price(leg: dict) -> float:
        b, a = live_prices.get(leg["code"], (leg.get("bid", 0), leg.get("ask", 0)))
        if leg["side"] == "SELL":
            return a if a > 0 else max((b + a) / 2, 0.01)  # buy back short: pay ask
        else:
            return b if b > 0 else max((b + a) / 2, 0.01)  # sell long: receive bid

    is_single = len(legs) == 1

    # ── Single-leg (CSP / CC) ──────────────────────────────────────────────────
    if is_single:
        leg, con = leg_cons[0]
        lim = max(round(_close_price(leg), 2), 0.01)
        try:
            order = LimitOrder(action="BUY", totalQuantity=num_c,
                               lmtPrice=lim, tif="DAY")
            with ibkr_lock:
                ib        = get_ib()
                trade_obj = ib.placeOrder(con, order)
            oid = trade_obj.order.orderId
            print(f"    Close BUY {num_c}× {leg['code']} @ {lim:.2f}  "
                  f"order_id={oid}  ({reason})")
            filled, _ = wait_for_fill(trade_obj, ORDER_TIMEOUT_MINS_SINGLE,
                                      ORDER_RETRY_SECONDS)
            return filled
        except Exception as exc:
            print(f"    close_spread ({ticker}) single-leg: {exc}")
            return False

    # ── Multi-leg: BAG contract ────────────────────────────────────────────────
    net_close   = sum(_close_price(l) if l["side"] == "SELL" else -_close_price(l)
                      for l in legs)
    outer_action = "BUY" if net_close > 0 else "SELL"
    limit_price  = max(round(abs(net_close), 2), 0.01)

    bag          = Contract()
    bag.symbol   = ticker
    bag.secType  = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"
    bag.comboLegs = []

    for leg, con in leg_cons:
        cl          = ComboLeg()
        cl.conId    = con.conId
        cl.ratio    = 1
        cl.action   = "BUY" if leg["side"] == "SELL" else "SELL"
        cl.exchange = "SMART"
        bag.comboLegs.append(cl)

    order = LimitOrder(action=outer_action, totalQuantity=num_c,
                       lmtPrice=limit_price, tif="DAY")
    try:
        with ibkr_lock:
            trade_obj = get_ib().placeOrder(bag, order)
        oid = trade_obj.order.orderId
        print(f"    Close BAG {ticker} {num_c}× @ {limit_price:.2f}  "
              f"order_id={oid}  ({reason})")
        filled, _ = wait_for_fill(trade_obj, ORDER_TIMEOUT_MINS_SPREAD,
                                  ORDER_RETRY_SECONDS)
        return filled
    except Exception as exc:
        print(f"    close_spread ({ticker}) multi-leg: {exc}")
        return False


def _close_spread_thread(trade: dict, action: str) -> None:
    """Background thread: close a spread and update the trade log."""
    stock_code = trade["stock_code"]

    def _clear_pending():
        update_trade(stock_code, {
            "_pending_close_reason": None,
            "_pending_close_pnl":    None,
            "_pending_close_date":   None,
            "_close_attempt_count":  trade.get("_close_attempt_count", 0) + 1,
        })

    try:
        filled = close_spread(trade, action)
        if filled:
            pnl = float(trade.get("_pending_close_pnl", 0) or 0)
            close_trade(stock_code, action, pnl)
            print(f"    [{stock_code}] Close confirmed — trade logged ({action}  P&L={pnl:+.2f})")
        else:
            print(f"    [{stock_code}] Close order timed out — will retry next cycle.")
            _clear_pending()
    except Exception as exc:
        log_alert("CRITICAL", f"Close thread exception for {stock_code}: {exc}",
                  {"stock_code": stock_code, "action": action})
        try:
            _clear_pending()
        except Exception:
            pass
    finally:
        with _close_threads_lock:
            _active_close_threads.pop(stock_code, None)


# ── Options trade evaluation ───────────────────────────────────────────────────

def evaluate_trade(trade: dict, fill_details: dict | None = None,
                   pos_pnl_map: dict | None = None,
                   snap_map: dict | None = None) -> dict:
    """Compute current P&L and action decision for one open trade."""
    if trade.get("trade_type") == "stock":
        pos_entry = (pos_pnl_map or {}).get(trade["stock_code"])
        return _evaluate_stock_trade(trade, fill_details,
                                     snap_map=snap_map, pos_pnl_entry=pos_entry)

    strategy   = trade["strategy"]
    legs       = trade.get("legs", [])
    mult       = trade.get("multiplier", 100)
    exp_date   = trade.get("exp_date", "")
    dte        = _days_to_expiry(exp_date)
    is_partial = fill_details.get("is_partial", False) if fill_details else False

    qty = (fill_details["effective_qty"] if fill_details and fill_details["effective_qty"] > 0
           else trade.get("num_contracts", 1))

    fill_price_map: dict[str, float] = {}
    if fill_details:
        for ld in fill_details["legs_detail"]:
            if ld["status"] == _FILLED and ld["avg_fill_price"] > 0:
                fill_price_map[ld["order_id"]] = ld["avg_fill_price"]

    active_legs = legs

    if not active_legs:
        return {"action": "hold", "pnl": 0, "dte": dte, "is_partial": False,
                "reason": "No legs to evaluate"}

    current_spread_val = 0.0
    current_theta_sum  = 0.0
    for leg in active_legs:
        mid = _current_leg_value(leg, snap_map=snap_map)
        if mid is None:
            return {"action": "hold", "pnl": 0, "dte": dte, "is_partial": False,
                    "reason": f"Could not fetch price for {leg.get('code', '?')}"}
        current_spread_val += mid if leg["side"] == "BUY" else -mid
        theta = _current_theta(leg, snap_map=snap_map)
        current_theta_sum  += theta if leg["side"] == "BUY" else -theta

    current_spread_val_total = current_spread_val * mult * qty

    nc_stored = float(trade.get("net_credit_per_spread") or 0)

    def _open_price(leg: dict) -> float:
        oid = leg.get("order_id", "")
        if str(oid) in fill_price_map:
            return fill_price_map[str(oid)]
        return (leg.get("bid", 0) + leg.get("ask", 0)) / 2

    active_sell = [l for l in active_legs if l["side"] == "SELL"]
    active_buy  = [l for l in active_legs if l["side"] == "BUY"]
    net_credit_actual = (
        sum(_open_price(l) for l in active_sell)
        - sum(_open_price(l) for l in active_buy)
    )
    nc_entry = nc_stored if nc_stored != 0 else net_credit_actual

    if strategy in _CREDIT_STRATEGIES:
        pnl = nc_entry * mult * qty + current_spread_val_total
    else:
        pnl = current_spread_val_total - abs(nc_entry) * mult * qty

    # Override with live position P&L only when non-zero (IBKR always returns 0.0)
    if pos_pnl_map:
        leg_pnl_vals = [pos_pnl_map.get(l["code"]) for l in active_legs]
        if all(v is not None for v in leg_pnl_vals):
            total_live_pnl = sum(v["pnl"] for v in leg_pnl_vals)
            if total_live_pnl != 0.0:
                pnl = total_live_pnl

    # ── Decision logic ─────────────────────────────────────────────────────────
    if strategy in _CREDIT_STRATEGIES:
        cost_to_close   = abs(current_spread_val_total)
        orig_credit_abs = abs(nc_entry * mult * qty)

        if cost_to_close <= orig_credit_abs * TP_CREDIT_REMAINING:
            return {"action": "take_profit", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"Captured {(1-TP_CREDIT_REMAINING)*100:.0f}%+ of max credit. "
                               f"Cost-to-close={cost_to_close:.2f}"}
        if cost_to_close >= orig_credit_abs * CL_CREDIT_MULT:
            return {"action": "cut_loss", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"Loss = {CL_CREDIT_MULT:.0f}x original credit. "
                               f"Cost-to-close={cost_to_close:.2f}"}
        if dte <= THETA_ROLL_DTE_CREDIT:
            return {"action": "roll", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"DTE={dte} <= {THETA_ROLL_DTE_CREDIT}."}

    elif strategy in _DEBIT_STRATEGIES:
        original_debit = abs(nc_entry * mult * qty)
        if pnl >= original_debit * (TP_DEBIT_MULT - 1):
            return {"action": "take_profit", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"P&L={pnl:.2f} reached +{(TP_DEBIT_MULT-1)*100:.0f}% target."}
        if current_spread_val_total <= original_debit * CL_DEBIT_REMAINING:
            return {"action": "cut_loss", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"Value fell to {CL_DEBIT_REMAINING*100:.0f}% of debit."}
        if dte <= THETA_EXIT_DTE_DEBIT and pnl < 0:
            return {"action": "theta_exit", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"DTE={dte} <= {THETA_EXIT_DTE_DEBIT}, negative P&L."}

    else:  # Long Straddle
        original_debit = abs(nc_entry * mult * qty)
        if pnl >= original_debit * (TP_STRADDLE_MULT - 1):
            return {"action": "take_profit", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"Straddle P&L={pnl:.2f} reached target."}
        if current_spread_val_total <= original_debit * CL_STRADDLE_REMAINING:
            return {"action": "cut_loss", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"Straddle lost {(1-CL_STRADDLE_REMAINING)*100:.0f}% of premium."}
        if dte <= THETA_EXIT_DTE_DEBIT:
            return {"action": "theta_exit", "pnl": pnl, "dte": dte, "is_partial": False,
                    "reason": f"DTE={dte} <= {THETA_EXIT_DTE_DEBIT}."}

    return {"action": "hold", "pnl": pnl, "dte": dte, "is_partial": False,
            "reason": f"Within bounds. DTE={dte}, P&L={pnl:+.2f}, Theta/day={current_theta_sum:.4f}"}


# ── Thesis invalidation ────────────────────────────────────────────────────────

def _is_thesis_invalidated(strategy: str, composite_score: float) -> tuple[bool, str]:
    s = composite_score
    if strategy in _BULLISH_STRATEGIES:
        if s <= -US_SPREAD_MIN_SIGNAL:
            return True, f"score turned bearish ({s:+.3f})"
    elif strategy in _BEARISH_STRATEGIES:
        if s >= US_SPREAD_MIN_SIGNAL:
            return True, f"score turned bullish ({s:+.3f})"
    elif strategy == "Iron Condor":
        if abs(s) >= _IC_DIRECTION_THRESHOLD:
            bias = "bullish" if s > 0 else "bearish"
            return True, f"stock developed strong {bias} bias ({s:+.3f})"
    elif strategy == "Cash-Secured Put":
        if s < CSP_MIN_COMPOSITE_SCORE:
            return True, (f"score fell below CSP threshold "
                          f"({s:+.3f} < {CSP_MIN_COMPOSITE_SCORE:+.2f})")
    elif strategy == "Covered Call":
        if s < 0:
            return True, f"score turned bearish ({s:+.3f})"
    elif strategy == "Stock Buy":
        if s <= -US_SPREAD_MIN_SIGNAL:
            return True, f"score turned bearish ({s:+.3f})"
    elif strategy == "Stock Sell Short":
        if s >= US_SPREAD_MIN_SIGNAL:
            return True, f"score turned bullish ({s:+.3f})"
    return False, ""


def _run_thesis_check(trade: dict, eval_pnl: float) -> dict | None:
    from datetime import date as _date

    today    = _date.today().isoformat()
    strategy = trade.get("strategy", "")

    if strategy == "Long Straddle":
        return None
    if trade.get("thesis_last_checked") == today:
        return None

    opened_at = trade.get("opened_at", "")
    if opened_at and str(opened_at)[:10] == today:
        update_trade(trade.get("stock_code", ""), {"thesis_last_checked": today})
        print(f"    [thesis] {trade.get('stock_code', '').split('.')[-1]}: "
              f"opened today — skipping first-day thesis check.")
        return None

    stock_code = trade["stock_code"]
    ticker     = stock_code.split(".")[-1]
    print(f"    [thesis] Re-scoring {ticker}...")

    df = fetch_kline(stock_code, days=260)
    if df is None or len(df) < 30:
        update_trade(stock_code, {"thesis_last_checked": today})
        return None

    scores = score_stock(df)
    if scores is None:
        update_trade(stock_code, {"thesis_last_checked": today})
        return None

    composite = scores["composite_score"]
    update_trade(stock_code, {"thesis_last_checked": today, "thesis_score": composite})
    print(f"    [thesis] {ticker}: composite={composite:+.3f}  "
          f"mf={scores['money_flow_score']:+.3f}  "
          f"dir={scores['direction_score']:+.3f}")

    invalidated, inv_reason = _is_thesis_invalidated(strategy, composite)
    if not invalidated:
        print(f"    [thesis] Thesis intact for {ticker}.")
        return None

    print(f"    [thesis] INVALIDATED — {ticker}: {inv_reason}")
    if strategy in _WHEEL_STRATEGIES:
        if eval_pnl >= 0:
            return {"action": "thesis_exit",
                    "reason": f"Thesis invalidated ({inv_reason}). P&L={eval_pnl:+.2f} — closing."}
        else:
            return {"action": "roll",
                    "reason": f"Thesis invalidated ({inv_reason}). P&L={eval_pnl:+.2f} — rolling."}
    else:
        return {"action": "thesis_exit",
                "reason": f"Thesis invalidated ({inv_reason}). Closing {strategy}."}


_INTRADAY_BULLISH = {"Bull Put Spread", "Bull Call Spread", "Cash-Secured Put",
                     "Covered Call", "Stock Buy"}
_INTRADAY_BEARISH = {"Bear Call Spread", "Bear Put Spread", "Stock Sell Short"}


def _run_intraday_check(trade: dict, snap_map: dict, eval_pnl: float) -> dict | None:
    if not INTRADAY_CHECK_ENABLED:
        return None
    strategy   = trade.get("strategy", "")
    stock_code = trade["stock_code"]
    if strategy == "Long Straddle":
        return None

    last = _intraday_checked.get(stock_code)
    if last and (datetime.now() - last).total_seconds() < INTRADAY_COOLDOWN_MINUTES * 60:
        return None

    snap        = snap_map.get(stock_code, {})
    change_rate = snap.get("change_rate") if hasattr(snap, "get") else None
    if change_rate is None:
        return None

    is_bullish = strategy in _INTRADAY_BULLISH
    is_bearish = strategy in _INTRADAY_BEARISH
    if not (is_bullish or is_bearish):
        return None

    move_pct = float(change_rate) * 100
    stage1 = (
        (is_bullish and move_pct <= -INTRADAY_MIN_MOVE_PCT) or
        (is_bearish and move_pct >=  INTRADAY_MIN_MOVE_PCT)
    )
    if not stage1:
        return None

    _intraday_checked[stock_code] = datetime.now()

    df = fetch_intraday_kline(stock_code, bars=80)
    if df is None or len(df) < INTRADAY_GATE_MIN_BARS:
        return None

    close  = df["close"]
    delta  = close.diff()
    gain   = delta.clip(lower=0).ewm(com=INTRADAY_GATE_RSI_PERIOD - 1,
                                     min_periods=INTRADAY_GATE_RSI_PERIOD).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=INTRADAY_GATE_RSI_PERIOD - 1,
                                        min_periods=INTRADAY_GATE_RSI_PERIOD).mean()
    rs     = gain / loss.replace(0, float("nan"))
    rsi    = (100 - 100 / (1 + rs)).iloc[-1]

    total_vol = df["volume"].sum()
    vwap      = (df["close"] * df["volume"]).sum() / total_vol if total_vol > 0 else df["close"].iloc[-1]
    vwap_pct  = (df["close"].iloc[-1] - vwap) / vwap * 100

    bull_hit = is_bullish and rsi < INTRADAY_GATE_RSI_BULL_FLOOR and vwap_pct < -INTRADAY_GATE_VWAP_PCT_THRESHOLD
    bear_hit = is_bearish and rsi > INTRADAY_GATE_RSI_BEAR_CEILING and vwap_pct >  INTRADAY_GATE_VWAP_PCT_THRESHOLD

    if not (bull_hit or bear_hit):
        return None

    if strategy in _WHEEL_STRATEGIES and eval_pnl < 0:
        return None

    return {"action": "thesis_exit",
            "reason": f"Intraday momentum: RSI-9={rsi:.0f}, VWAP dev={vwap_pct:+.1f}%, day={move_pct:+.1f}%"}


# ── Main monitor loop ──────────────────────────────────────────────────────────

def run_monitor(account_us: dict) -> list[dict]:
    """Check all open trades. Execute TP/CL/theta-exit actions."""
    open_trades = get_open_trades()
    if not open_trades:
        print("  No open trades to monitor.")
        return []

    print(f"\n  Monitoring {len(open_trades)} open trade(s)")

    pos_pnl_map = _fetch_pos_pnl_map()
    snap_map    = _build_snap_map(open_trades)

    _HDR = (f"  {'Stock':<14} {'Strategy':<22} {'DTE':>5} "
            f"{'P&L':>10} {'Action':<14} Reason")
    _SEP = "  " + "─" * 100
    print(_HDR)
    print(_SEP)

    results: list[dict] = []

    for trade in open_trades:
        stock_code   = trade["stock_code"]
        fill_details = _get_fill_details(trade, pos_pnl_map=pos_pnl_map)

        if not fill_details["any_filled"]:
            if fill_details.get("all_dead"):
                close_trade(stock_code, "cancelled", 0.0)
                print(f"  {stock_code:<14} {trade['strategy']:<22} "
                      f"{'':>5} {'':>10} {'AUTO-CLOSED':<14} all orders cancelled")
                results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                 "action": "cancelled", "pnl": 0, "dte": 0,
                                 "is_partial": False, "reason": fill_details["summary"]})
            else:
                print(f"  {stock_code:<14} {trade['strategy']:<22} "
                      f"{'':>5} {'':>10} {'PENDING FILL':<14} {fill_details['summary'][:70]}")
                results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                 "action": "pending", "pnl": 0, "dte": 0,
                                 "is_partial": False, "reason": fill_details["summary"]})
            continue

        if (trade.get("trade_type") == "stock"
                and trade.get("placement_status") != "complete"):
            update_trade(stock_code, {"placement_status": "complete"})

        if (trade.get("trade_type") == "options"
                and trade.get("placement_status") != "complete"):
            # First fill detected — upgrade pending_fill to complete.
            # For BAG orders all legs share one order_id; avg_fill_price is the
            # combo premium. Recalculate TP/CL from actual fill price.
            actual_fill = next(
                (l["avg_fill_price"] for l in fill_details.get("legs_detail", [])
                 if l["status"] == _FILLED and l["avg_fill_price"] > 0),
                0.0,
            )
            if actual_fill > 0:
                old_nc = float(trade.get("net_credit_per_spread", 0))
                nc     = actual_fill if old_nc >= 0 else -actual_fill
                mult_v = int(trade.get("multiplier", 100))
                n_c    = int(trade.get("num_contracts", 1))
                strat  = trade.get("strategy", "")
                if strat in _CREDIT_STRATEGIES:
                    tp_val = nc * (1 - TP_CREDIT_REMAINING) * n_c * mult_v
                    cl_val = -nc * (CL_CREDIT_MULT - 1) * n_c * mult_v
                elif strat in _DEBIT_STRATEGIES:
                    debit  = abs(nc)
                    tp_val =  debit * (TP_DEBIT_MULT - 1)      * n_c * mult_v
                    cl_val = -debit * (1 - CL_DEBIT_REMAINING) * n_c * mult_v
                else:  # Long Straddle
                    debit  = abs(nc)
                    tp_val =  debit * (TP_STRADDLE_MULT - 1)      * n_c * mult_v
                    cl_val = -debit * (1 - CL_STRADDLE_REMAINING) * n_c * mult_v
                update_trade(stock_code, {
                    "placement_status":      "complete",
                    "net_credit_per_spread": round(nc, 4),
                    "tp_value":              round(tp_val, 2),
                    "cl_value":              round(cl_val, 2),
                })
                print(f"  [{stock_code}] Spread fill confirmed: "
                      f"nc={nc:.4f}  TP={tp_val:.2f}  CL={cl_val:.2f}")
            else:
                update_trade(stock_code, {"placement_status": "complete"})

        # ── Pending close tracking ─────────────────────────────────────────────
        pending_reason = trade.get("_pending_close_reason")
        if pending_reason:
            with _close_threads_lock:
                thread_active = (stock_code in _active_close_threads
                                 and _active_close_threads[stock_code].is_alive())
            if thread_active:
                dte_disp = str(_days_to_expiry(trade.get("exp_date", "")))
                print(f"  {stock_code:<14} {trade['strategy']:<22} "
                      f"{dte_disp:>5} {'—':>10} {'CLOSING':<14} "
                      f"close thread active ({pending_reason})...")
                results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                 "action": "closing", "pnl": 0, "dte": 0,
                                 "is_partial": False, "reason": f"Close thread running ({pending_reason})"})
                continue

            today_s      = datetime.now().date().isoformat()
            pending_date = trade.get("_pending_close_date", "")
            is_stale     = (not pending_date) or (pending_date != today_s)
            resolved     = False

            if pos_pnl_map is not None:
                if trade.get("trade_type") == "options":
                    leg_codes    = {l["code"] for l in trade.get("legs", [])}
                    position_gone = bool(leg_codes) and leg_codes.isdisjoint(pos_pnl_map)
                else:
                    position_gone = stock_code not in pos_pnl_map

                if position_gone:
                    stored_pnl = float(trade.get("_pending_close_pnl", 0))
                    close_trade(stock_code, pending_reason, stored_pnl)
                    dte_disp = str(_days_to_expiry(trade.get("exp_date", "")))
                    print(f"  {stock_code:<14} {trade['strategy']:<22} "
                          f"{dte_disp:>5} {stored_pnl:>+10.2f} {'CLOSED':<14} "
                          f"confirmed filled ({pending_reason})")
                    results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                     "action": pending_reason, "pnl": stored_pnl, "dte": 0,
                                     "is_partial": False, "reason": f"Pending close filled ({pending_reason})"})
                    resolved = True

                elif is_stale:
                    update_trade(stock_code, {
                        "_pending_close_reason": None,
                        "_pending_close_pnl":    None,
                        "_pending_close_date":   None,
                    })
                    print(f"    {stock_code}: {pending_reason} close order from prior session expired — re-evaluating")

            if resolved:
                continue
            if not is_stale:
                market_open = (
                    is_hk_market_open() if trade.get("market") == "HK"
                    else is_us_market_open()
                )
                if (trade.get("trade_type") == "stock"
                        and pos_pnl_map is not None
                        and stock_code in pos_pnl_map
                        and market_open):
                    _cancel_and_reprice_stock_close(trade, pending_reason)
                else:
                    dte_disp = str(_days_to_expiry(trade.get("exp_date", "")))
                    print(f"  {stock_code:<14} {trade['strategy']:<22} "
                          f"{dte_disp:>5} {'—':>10} {'PEND.CLOSE':<14} "
                          f"awaiting {pending_reason} close order...")
                results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                 "action": "pending_close", "pnl": 0, "dte": 0,
                                 "is_partial": False, "reason": f"Close order outstanding ({pending_reason})"})
                continue

        # ── External-close detection ───────────────────────────────────────────
        if pos_pnl_map is not None and fill_details["any_filled"]:
            try:
                opened_dt  = datetime.fromisoformat(str(trade.get("opened_at", "")))
                too_recent = (datetime.now() - opened_dt.replace(tzinfo=None)) < timedelta(minutes=5)
            except Exception:
                too_recent = False

            if not too_recent:
                if trade.get("trade_type") == "options":
                    leg_codes    = {l["code"] for l in trade.get("legs", [])}
                    position_gone = bool(leg_codes) and leg_codes.isdisjoint(pos_pnl_map)
                else:
                    position_gone = stock_code not in pos_pnl_map

                if position_gone:
                    dte        = _days_to_expiry(trade.get("exp_date", ""))
                    is_expired = trade.get("trade_type") == "options" and dte == 0

                    if is_expired and trade.get("strategy", "") in _CREDIT_STRATEGIES:
                        strategy_now = trade.get("strategy", "")
                        if strategy_now == "Cash-Secured Put":
                            stock_assigned = stock_code in (pos_pnl_map or {})
                            if stock_assigned:
                                sell_legs        = [l for l in trade.get("legs", []) if l["side"] == "SELL"]
                                assignment_price = float(sell_legs[0]["strike"]) if sell_legs else 0.0
                                num_c = trade.get("num_contracts", 1)
                                mult  = trade.get("multiplier", 100)
                                csp_premium = abs(float(trade.get("net_credit_per_spread", 0))) * num_c * mult
                                update_trade(stock_code, {
                                    "wheel_assignment_price":    assignment_price,
                                    "wheel_premium_accumulated": round(csp_premium, 2),
                                })
                                pnl_close  = 0.0
                                ext_reason = "assigned"
                                net_basis  = assignment_price - csp_premium / (num_c * mult)
                                print(f"  {stock_code:<14} {'Cash-Secured Put':<22} "
                                      f"    [ASSIGNED] strike=${assignment_price:.2f}  "
                                      f"premium=${csp_premium:.2f}  basis=${net_basis:.2f}/sh")
                            else:
                                net_c     = float(trade.get("net_credit_per_spread", 0))
                                pnl_close = net_c * trade.get("num_contracts", 1) * trade.get("multiplier", 100)
                                ext_reason = "expired"
                        else:
                            net_c     = float(trade.get("net_credit_per_spread", 0))
                            pnl_close = net_c * trade.get("num_contracts", 1) * trade.get("multiplier", 100)
                            ext_reason = "expired"
                    else:
                        pnl_close  = 0.0
                        ext_reason = "manually_closed"

                    close_trade(stock_code, ext_reason, pnl_close)
                    if ext_reason != "assigned":
                        print(f"  {stock_code:<14} {trade['strategy']:<22} "
                              f"{'—':>5} {pnl_close:>+10.2f} {'EXT. CLOSED':<14} "
                              f"not in positions — {ext_reason}")
                    results.append({"stock_code": stock_code, "strategy": trade["strategy"],
                                     "action": ext_reason, "pnl": pnl_close,
                                     "dte": dte, "is_partial": False,
                                     "reason": f"Position gone ({ext_reason})"})
                    continue

        # ── Evaluate P&L ───────────────────────────────────────────────────────
        market_open = (
            is_hk_market_open() if trade.get("market") == "HK"
            else is_us_market_open()
        )

        eval_result = evaluate_trade(trade, fill_details, pos_pnl_map=pos_pnl_map,
                                     snap_map=snap_map)
        action  = eval_result["action"]
        pnl     = eval_result["pnl"]
        dte     = eval_result["dte"]
        reason  = eval_result["reason"]

        update_trade(stock_code, {"unrealized_pnl": pnl})

        # ── Daily thesis check ─────────────────────────────────────────────────
        if action == "hold" and fill_details.get("any_filled") and market_open:
            thesis = _run_thesis_check(trade, eval_pnl=pnl)
            if thesis:
                action = thesis["action"]
                reason = thesis["reason"]
                eval_result["action"] = action
                eval_result["reason"] = reason
                time.sleep(0.3)

        # ── Intraday momentum overlay ──────────────────────────────────────────
        if action == "hold" and fill_details.get("any_filled") and market_open:
            intraday = _run_intraday_check(trade, snap_map, eval_pnl=pnl)
            if intraday:
                action = intraday["action"]
                reason = intraday["reason"]
                eval_result["action"] = action
                eval_result["reason"] = reason

        dte_str = "—" if dte == 999 else str(dte)
        print(f"  {stock_code:<14} {trade['strategy']:<22} "
              f"{dte_str:>5} {pnl:>+10.2f} {action:<14} {reason[:60]}")

        if (action == "hold" and trade.get("trade_type") == "stock"
                and eval_result.get("trail_active")):
            th  = eval_result["trail_high"]
            ts  = eval_result["trail_stop"]
            pct = (th - ts) / th * 100 if th > 0 else 0
            print(f"    [trail] active — peak={th:.3f}  stop={ts:.3f}  ({pct:.1f}% drop to trigger)")

        if action in ("take_profit", "cut_loss", "trail_stop", "theta_exit", "thesis_exit"):
            if not market_open:
                print(f"    US market closed — '{action}' deferred to next cycle.")
            else:
                update_trade(stock_code, {
                    "_pending_close_reason": action,
                    "_pending_close_pnl":    round(pnl, 2),
                    "_pending_close_date":   datetime.now().date().isoformat(),
                })
                if trade.get("trade_type") == "stock":
                    ok, close_oid = close_stock_trade(trade, action)
                    if ok:
                        update_trade(stock_code, {"close_order_id": close_oid})
                        print(f"    -> Close order placed — pending fill confirmation")
                else:
                    with _close_threads_lock:
                        already = (stock_code in _active_close_threads
                                   and _active_close_threads[stock_code].is_alive())
                    if not already:
                        t = threading.Thread(
                            target=_close_spread_thread,
                            args=(dict(trade), action),
                            name=f"close-{stock_code}",
                            daemon=True,
                        )
                        with _close_threads_lock:
                            _active_close_threads[stock_code] = t
                        t.start()
                        print(f"    -> Close thread started for {stock_code} ({action})")
                    else:
                        print(f"    -> Close thread already running for {stock_code}")

        eval_result["stock_code"] = stock_code
        eval_result["strategy"]   = trade["strategy"]
        results.append(eval_result)

    return results
