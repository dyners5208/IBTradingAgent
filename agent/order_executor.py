"""
Order executor — IBKR version.

Multi-leg spreads are placed as a single BAG/combo contract order via ib_insync.
IBKR charges spread max-loss as margin (not naked-short collateral) when legs are
submitted as a combo. Single-leg strategies (CSP, CC) use a plain LimitOrder
against the Option contract directly.

Order IDs are IBKR integers (not UUID strings). Stored in trade records as int.
Backwards-compatible with legacy UUID strings from pre-IBKR records.

Thread safety: every IB call is inside `with ibkr_lock`.
"""

import time
from datetime import datetime as _dt

import pandas as pd

from agent.constants import (
    MAX_CONTRACTS, ORDER_MID_NUDGE, ORDER_RETRY_SECONDS,
    ORDER_TIMEOUT_MINS_SINGLE, ORDER_TIMEOUT_MINS_SPREAD,
    OPT_MAX_BID_ASK_SPREAD_PCT, OPT_MIN_OPEN_INTEREST,
    MIN_MARGIN_BUFFER_PCT,
)
from agent.risk_manager import margin_safe_to_trade
from agent.alert_log import log_alert

_IBKR_FILLED_STATUS   = {"Filled"}
_IBKR_TERMINAL_STATUS = {"Cancelled", "ApiCancelled", "Inactive"}
_IBKR_PENDING_STATUS  = {"Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2


def _limit_price(bid: float, ask: float, side: str) -> float:
    """Limit price nudged slightly toward aggression from mid."""
    mid = _mid(bid, ask)
    if side == "BUY":
        return round(mid + ORDER_MID_NUDGE * (ask - mid), 2)
    else:
        return round(mid - ORDER_MID_NUDGE * (mid - bid), 2)


def _occ_to_ibkr_contract(occ_symbol: str, conid: int = 0):
    """Convert an OCC option symbol to an ib_insync Option contract.

    If conid > 0 it is pre-set, avoiding a qualifyContracts round-trip.
    """
    from ib_insync import Option
    from agent.options_analyzer import _parse_occ
    parsed = _parse_occ(occ_symbol)
    if not parsed:
        raise ValueError(f"Cannot parse OCC symbol: {occ_symbol!r}")
    exp_str = parsed["exp_date"].strftime("%Y%m%d")
    con = Option(
        symbol=parsed["ticker"],
        lastTradeDateOrContractMonth=exp_str,
        strike=parsed["strike"],
        right=parsed["call_or_put"],
        exchange="SMART",
    )
    if conid:
        con.conId = conid
    return con


def _get_open_trade_by_id(order_id: int):
    """Look up an ib_insync Trade object from open orders by orderId. Returns None if gone."""
    from agent.ibkr_client import get_ib, ibkr_lock
    with ibkr_lock:
        return next(
            (t for t in get_ib().openTrades() if t.order.orderId == order_id),
            None,
        )


def _check_fills_by_order_id(order_id: int) -> tuple[bool, float]:
    """Check ib.fills() for a completed fill matching order_id."""
    from agent.ibkr_client import get_ib, ibkr_lock
    with ibkr_lock:
        fills = get_ib().fills()
    matching = [f for f in fills if f.execution.orderId == order_id]
    if matching:
        avg_price = sum(f.execution.price * f.execution.shares for f in matching) / sum(
            f.execution.shares for f in matching
        )
        return True, float(avg_price)
    return False, 0.0


# ── Options contract selection ─────────────────────────────────────────────────

def select_spread_legs(chain: pd.DataFrame, strategy: str,
                       current_price: float) -> list[dict] | None:
    """Select spread legs from an IBKR options chain DataFrame.

    The chain uses columns: code, strike_price, call_or_put (C/P),
    bid, ask, delta, theta, implied_volatility, _ibkr_conid.

    Returns list of leg dicts or None if chain is insufficient.
    Each leg dict includes a 'conid' key for IBKR BAG order construction.
    """
    if chain is None or chain.empty:
        return None

    chain = chain.copy()
    chain.columns = [c.lower() for c in chain.columns]

    for col in ["bid", "ask", "strike_price", "delta", "theta", "implied_volatility"]:
        if col in chain.columns:
            chain[col] = pd.to_numeric(chain[col], errors="coerce")

    if "bid" in chain.columns:
        chain = chain[chain["bid"] > 0].copy()
    if chain.empty:
        return None

    if "bid" in chain.columns and "ask" in chain.columns:
        mid_p      = (chain["bid"] + chain["ask"]) / 2
        spread_pct = (chain["ask"] - chain["bid"]) / mid_p.replace(0, float("nan")) * 100
        chain = chain[spread_pct <= OPT_MAX_BID_ASK_SPREAD_PCT].copy()

    if OPT_MIN_OPEN_INTEREST > 0 and "open_interest" in chain.columns:
        chain["open_interest"] = pd.to_numeric(chain["open_interest"], errors="coerce").fillna(0)
        chain = chain[chain["open_interest"] >= OPT_MIN_OPEN_INTEREST].copy()

    if chain.empty:
        return None

    cop_col = next(
        (c for c in chain.columns if "call_or_put" in c or c == "option_type"), None
    )
    if cop_col is None:
        return None

    puts  = chain[chain[cop_col].str.upper().isin(["P", "PUT"])].copy()
    calls = chain[chain[cop_col].str.upper().isin(["C", "CALL"])].copy()

    def _find_by_delta(sub: pd.DataFrame, target_delta: float,
                       side: str) -> pd.Series | None:
        if "delta" not in sub.columns:
            return None
        sub = sub[sub["delta"].abs() > 0.001].dropna(subset=["delta"])
        if sub.empty:
            return None
        sub = sub.copy()
        sub["_d_diff"] = (sub["delta"].abs() - abs(target_delta)).abs()
        return sub.nsmallest(1, "_d_diff").iloc[0]

    def _find_by_strike_pct(sub: pd.DataFrame, pct_otm: float,
                             direction: str) -> pd.Series | None:
        if sub.empty:
            return None
        sub = sub.copy()
        target = (current_price * (1 + pct_otm) if direction == "above"
                  else current_price * (1 - pct_otm))
        sub["_s_diff"] = (sub["strike_price"] - target).abs()
        return sub.nsmallest(1, "_s_diff").iloc[0]

    def _pick(a, b):
        return a if a is not None else b

    def _make_leg(row: pd.Series, trd_side: str) -> dict:
        return {
            "code":        str(row["code"]),
            "conid":       int(row.get("_ibkr_conid", 0)),   # IBKR conId for BAG order
            "strike":      float(row["strike_price"]),
            "call_or_put": str(row.get(cop_col, "")),
            "side":        trd_side,
            "delta":       float(row.get("delta", 0)),
            "bid":         float(row.get("bid", 0)),
            "ask":         float(row.get("ask", 0)),
            "theta":       float(row.get("theta", 0)),
            "iv":          float(row.get("implied_volatility", 0)),
        }

    if strategy == "Bull Put Spread":
        short = _pick(_find_by_delta(puts, 0.30, "SELL"), _find_by_strike_pct(puts, 0.06, "below"))
        long  = _pick(_find_by_delta(puts, 0.15, "BUY"),  _find_by_strike_pct(puts, 0.12, "below"))
        if short is None or long is None:
            return None
        if float(short["strike_price"]) <= float(long["strike_price"]):
            return None
        return [_make_leg(short, "SELL"), _make_leg(long, "BUY")]

    elif strategy == "Bull Call Spread":
        long  = _pick(_find_by_delta(calls, 0.50, "BUY"),  _find_by_strike_pct(calls, 0.00, "above"))
        short = _pick(_find_by_delta(calls, 0.25, "SELL"), _find_by_strike_pct(calls, 0.07, "above"))
        if long is None or short is None:
            return None
        if float(short["strike_price"]) <= float(long["strike_price"]):
            return None
        return [_make_leg(long, "BUY"), _make_leg(short, "SELL")]

    elif strategy == "Bear Put Spread":
        long  = _pick(_find_by_delta(puts, 0.50, "BUY"),  _find_by_strike_pct(puts, 0.00, "below"))
        short = _pick(_find_by_delta(puts, 0.25, "SELL"), _find_by_strike_pct(puts, 0.07, "below"))
        if long is None or short is None:
            return None
        if float(long["strike_price"]) <= float(short["strike_price"]):
            return None
        return [_make_leg(long, "BUY"), _make_leg(short, "SELL")]

    elif strategy == "Bear Call Spread":
        short = _pick(_find_by_delta(calls, 0.30, "SELL"), _find_by_strike_pct(calls, 0.06, "above"))
        long  = _pick(_find_by_delta(calls, 0.15, "BUY"),  _find_by_strike_pct(calls, 0.12, "above"))
        if short is None or long is None:
            return None
        if float(long["strike_price"]) <= float(short["strike_price"]):
            return None
        return [_make_leg(short, "SELL"), _make_leg(long, "BUY")]

    elif strategy == "Iron Condor":
        sc = _pick(_find_by_delta(calls, 0.25, "SELL"), _find_by_strike_pct(calls, 0.05, "above"))
        lc = _pick(_find_by_delta(calls, 0.10, "BUY"),  _find_by_strike_pct(calls, 0.10, "above"))
        sp = _pick(_find_by_delta(puts,  0.25, "SELL"), _find_by_strike_pct(puts,  0.05, "below"))
        lp = _pick(_find_by_delta(puts,  0.10, "BUY"),  _find_by_strike_pct(puts,  0.10, "below"))
        if any(x is None for x in [sc, lc, sp, lp]):
            return None
        return [_make_leg(sc, "SELL"), _make_leg(lc, "BUY"),
                _make_leg(sp, "SELL"), _make_leg(lp, "BUY")]

    elif strategy == "Long Straddle":
        atm_call = _pick(_find_by_delta(calls, 0.50, "BUY"), _find_by_strike_pct(calls, 0.00, "above"))
        atm_put  = _pick(_find_by_delta(puts,  0.50, "BUY"), _find_by_strike_pct(puts,  0.00, "below"))
        if atm_call is None or atm_put is None:
            return None
        return [_make_leg(atm_call, "BUY"), _make_leg(atm_put, "BUY")]

    elif strategy == "Cash-Secured Put":
        short = _pick(_find_by_delta(puts, 0.30, "SELL"), _find_by_strike_pct(puts, 0.06, "below"))
        if short is None:
            return None
        return [_make_leg(short, "SELL")]

    elif strategy == "Covered Call":
        short = _pick(_find_by_delta(calls, 0.25, "SELL"), _find_by_strike_pct(calls, 0.06, "above"))
        if short is None:
            return None
        return [_make_leg(short, "SELL")]

    return None


# ── Position sizing ────────────────────────────────────────────────────────────

def compute_contracts(legs: list[dict], budget: float, strategy: str) -> int:
    """Calculate contract count given the per-trade budget."""
    mult = 100

    buy_legs  = [l for l in legs if l["side"] == "BUY"]
    sell_legs = [l for l in legs if l["side"] == "SELL"]

    buy_cost  = sum(_mid(l["bid"], l["ask"]) for l in buy_legs)
    sell_cost = sum(_mid(l["bid"], l["ask"]) for l in sell_legs)
    net_cost  = buy_cost - sell_cost   # positive = debit, negative = credit

    if net_cost > 0:
        cost_per_spread = net_cost * mult

    elif strategy == "Covered Call":
        cost_per_spread = 0.01

    elif not buy_legs:
        # CSP — cash-secured by full strike minus premium
        strike = sell_legs[0].get("strike", 0) if sell_legs else 10.0
        net_credit = abs(net_cost)
        cost_per_spread = max((strike - net_credit) * mult, 0.01)

    else:
        # Multi-leg credit spread — IBKR charges spread max-loss for combo orders
        net_credit = abs(net_cost)
        if len(sell_legs) == 1 and len(buy_legs) == 1:
            spread_width = abs(sell_legs[0]["strike"] - buy_legs[0]["strike"])
        else:
            cop = lambda l: l.get("call_or_put", "").upper()
            puts_s  = [l for l in sell_legs if cop(l) in ("P", "PUT")]
            puts_b  = [l for l in buy_legs  if cop(l) in ("P", "PUT")]
            calls_s = [l for l in sell_legs if cop(l) in ("C", "CALL")]
            calls_b = [l for l in buy_legs  if cop(l) in ("C", "CALL")]
            put_w  = abs(puts_s[0]["strike"]  - puts_b[0]["strike"])  if puts_s and puts_b else 0.0
            call_w = abs(calls_b[0]["strike"] - calls_s[0]["strike"]) if calls_s and calls_b else 0.0
            spread_width = max(put_w, call_w)
        cost_per_spread = max((spread_width - net_credit) * mult, 0.01)

    if cost_per_spread <= 0:
        return 0

    n = int(budget / cost_per_spread)
    return min(n, MAX_CONTRACTS)


def compute_shares(current_price: float, budget: float, lot_size: int = 1) -> int:
    """Return share qty rounded down to the nearest board lot."""
    if current_price <= 0:
        return 0
    raw = int(budget / current_price)
    if lot_size > 1:
        raw = (raw // lot_size) * lot_size
    return raw


# ── Order lifecycle ────────────────────────────────────────────────────────────

def wait_for_fill(order_ref, timeout_mins: int,
                  retry_secs: int | None = None) -> tuple[bool, float]:
    """Poll until the order is filled or the timeout expires.

    order_ref: ib_insync Trade object (fresh from placeOrder) OR integer orderId
               (restored from DB). Cancels on timeout.

    Returns (filled, avg_fill_price).
    """
    from agent.ibkr_client import get_ib, ibkr_lock

    if retry_secs is None:
        retry_secs = ORDER_RETRY_SECONDS

    # Resolve integer orderId to Trade object if needed
    if isinstance(order_ref, (int, str)):
        try:
            oid = int(order_ref)
        except (TypeError, ValueError):
            return False, 0.0
        # Check fills first (may already be done)
        filled, fp = _check_fills_by_order_id(oid)
        if filled:
            return True, fp
        trade = _get_open_trade_by_id(oid)
        if trade is None:
            print(f"    [wait_for_fill] orderId={oid} not in open orders or fills — assuming gone")
            return False, 0.0
    else:
        trade = order_ref
        oid   = trade.order.orderId

    deadline = _dt.now().timestamp() + timeout_mins * 60

    while _dt.now().timestamp() < deadline:
        with ibkr_lock:
            ib = get_ib()
            ib.sleep(0)   # process pending events
            status   = trade.orderStatus.status
            fill_qty = trade.orderStatus.filled
            fill_px  = trade.orderStatus.avgFillPrice

        if status in _IBKR_FILLED_STATUS:
            return True, float(fill_px or 0)
        if status in _IBKR_TERMINAL_STATUS:
            print(f"    [orderId={oid}] status={status} — aborting wait.")
            return False, 0.0

        print(f"    [orderId={oid}] {status}  filled={fill_qty} — waiting {retry_secs}s...")
        time.sleep(retry_secs)

    print(f"    [orderId={oid}] not filled after {timeout_mins}m — cancelling.")
    cancel_order(trade)
    return False, 0.0


def cancel_order(order_ref) -> bool:
    """Cancel an order. order_ref is either a Trade object or an integer orderId."""
    from agent.ibkr_client import get_ib, ibkr_lock
    try:
        if isinstance(order_ref, (int, str)):
            try:
                oid = int(order_ref)
            except (TypeError, ValueError):
                return False
            trade = _get_open_trade_by_id(oid)
            if trade is None:
                return True   # already gone
        else:
            trade = order_ref
        with ibkr_lock:
            get_ib().cancelOrder(trade.order)
        return True
    except Exception as exc:
        print(f"    cancel_order failed: {exc}")
        return False


# ── Spread placement ───────────────────────────────────────────────────────────

def place_spread(stock_code: str, legs: list[dict], num_contracts: int,
                 account: dict, strategy: str, exp_date: str) -> dict | None:
    """Place a multi-leg options spread as a single IBKR BAG/combo order.

    For multi-leg strategies (spreads, IC, straddle): submits a BAG contract
    with ComboLegs. Each leg must have a 'conid' key (IBKR contract ID).

    For single-leg strategies (CSP, CC): submits a plain LimitOrder against
    the Option contract, logged as pending_fill for the monitor to track.

    Returns a trade record dict or None on failure.
    """
    from ib_insync import Contract, ComboLeg, LimitOrder, Option
    from agent.ibkr_client import get_ib, ibkr_lock

    mult      = 100
    buy_legs  = [l for l in legs if l["side"] == "BUY"]
    sell_legs = [l for l in legs if l["side"] == "SELL"]

    buy_debit   = sum(_mid(l["bid"], l["ask"]) for l in buy_legs)
    sell_credit = sum(_mid(l["bid"], l["ask"]) for l in sell_legs)
    net_credit  = sell_credit - buy_debit   # positive = credit spread

    # ── Margin safety check ────────────────────────────────────────────────────
    if net_credit > 0 and buy_legs:
        cop = lambda l: l.get("call_or_put", "").upper()
        if len(sell_legs) == 1 and len(buy_legs) == 1:
            spread_width = abs(sell_legs[0]["strike"] - buy_legs[0]["strike"])
        else:
            puts_s  = [l for l in sell_legs if cop(l) in ("P", "PUT")]
            puts_b  = [l for l in buy_legs  if cop(l) in ("P", "PUT")]
            calls_s = [l for l in sell_legs if cop(l) in ("C", "CALL")]
            calls_b = [l for l in buy_legs  if cop(l) in ("C", "CALL")]
            put_w  = abs(puts_s[0]["strike"]  - puts_b[0]["strike"])  if puts_s and puts_b else 0.0
            call_w = abs(calls_b[0]["strike"] - calls_s[0]["strike"]) if calls_s and calls_b else 0.0
            spread_width = max(put_w, call_w)
        net_order_cost = max((spread_width - net_credit) * mult * num_contracts, 0.0)
    elif strategy == "Covered Call":
        net_order_cost = 0.0
    elif not buy_legs:
        # CSP — cash-secured by full strike minus premium
        strike = sell_legs[0].get("strike", 0) if sell_legs else 0.0
        net_order_cost = max((strike - net_credit) * mult * num_contracts, 0.0)
    else:
        net_order_cost = abs(net_credit) * mult * num_contracts

    ok, reason = margin_safe_to_trade(account, net_order_cost)
    if not ok:
        bp         = account.get("buying_power", 0)
        max_budget = bp if bp > 0 else (
            account.get("total_assets", 0) or account.get("cash", 0)
        )
        if max_budget > 0 and num_contracts > 1:
            cost_per  = net_order_cost / num_contracts
            reduced_n = int(max_budget / cost_per) if cost_per > 0 else 0
            if reduced_n >= 1:
                ok2, _ = margin_safe_to_trade(account, cost_per * reduced_n)
                if ok2:
                    print(f"    [{stock_code}] Margin-adjusted: "
                          f"{num_contracts} → {reduced_n} contract(s)")
                    num_contracts = reduced_n
                else:
                    print(f"    SKIP {stock_code}: {reason}")
                    return None
            else:
                print(f"    SKIP {stock_code}: {reason}")
                return None
        else:
            print(f"    SKIP {stock_code}: {reason}")
            return None

    # ── Build and submit order ─────────────────────────────────────────────────
    is_single    = len(legs) == 1
    timeout_mins = ORDER_TIMEOUT_MINS_SINGLE if is_single else ORDER_TIMEOUT_MINS_SPREAD

    if is_single:
        # CSP / CC — plain option order
        leg = legs[0]
        lim = max(_limit_price(leg["bid"], leg["ask"], leg["side"]), 0.01)
        try:
            option_con = _occ_to_ibkr_contract(leg["code"], conid=leg.get("conid", 0))
            ibkr_order = LimitOrder(
                action="SELL",
                totalQuantity=num_contracts,
                lmtPrice=round(lim, 2),
                tif="DAY",
            )
            with ibkr_lock:
                ib = get_ib()
                if not option_con.conId:
                    ib.qualifyContracts(option_con)
                trade_obj = ib.placeOrder(option_con, ibkr_order)
        except Exception as exc:
            print(f"    [{stock_code}] placeOrder failed: {exc}")
            log_alert("CRITICAL",
                      f"Single-leg order failed for {stock_code}: {exc}",
                      {"strategy": strategy, "error": str(exc)})
            return None

        order_id = trade_obj.order.orderId
        print(f"    [{stock_code}] {strategy} order submitted — "
              f"orderId={order_id}, {num_contracts}x @ lim {lim:.2f}")

        nc = net_credit
        from agent.constants import TP_CREDIT_REMAINING, CL_CREDIT_MULT
        tp_value = nc * (1 - TP_CREDIT_REMAINING) * num_contracts * mult
        cl_value = -nc * (CL_CREDIT_MULT - 1) * num_contracts * mult
        return {
            "stock_code":            stock_code,
            "strategy":              strategy,
            "market":                "US",
            "trade_type":            "options",
            "legs":                  [{
                "order_id":        order_id,
                "conid":           leg.get("conid", 0),
                "limit_price":     round(lim, 2),
                "avg_fill_price":  0.0,
                "qty":             num_contracts,
                **{k: v for k, v in leg.items() if k != "conid"},
            }],
            "num_contracts":         num_contracts,
            "multiplier":            mult,
            "net_credit_per_spread": round(nc, 4),
            "tp_value":              round(tp_value, 2),
            "cl_value":              round(cl_value, 2),
            "exp_date":              exp_date,
            "placement_status":      "pending_fill",
        }

    # ── Multi-leg: BAG/combo contract ─────────────────────────────────────────
    try:
        from ib_insync import TagValue
        bag          = Contract()
        bag.symbol   = stock_code
        bag.secType  = "BAG"
        bag.currency = "USD"
        bag.exchange = "SMART"

        # Build ComboLegs from leg dicts; qualify contracts if conid is missing
        combo_legs = []
        for leg in legs:
            conid = int(leg.get("conid", 0))
            if conid == 0:
                # Fall back to qualifying via OCC symbol
                opt_con = _occ_to_ibkr_contract(leg["code"])
                with ibkr_lock:
                    get_ib().qualifyContracts(opt_con)
                conid = opt_con.conId
            cl          = ComboLeg()
            cl.conId    = conid
            cl.ratio    = 1
            cl.action   = "SELL" if leg["side"] == "SELL" else "BUY"
            cl.exchange = "SMART"
            combo_legs.append(cl)

        bag.comboLegs = combo_legs

        # Credit spread → SELL the combo; debit → BUY the combo.
        # Nudge limit toward taker by ORDER_MID_NUDGE to improve fill rate
        # when working off slightly stale chain prices.
        outer_action = "SELL" if net_credit > 0 else "BUY"
        raw_limit    = abs(net_credit)
        if net_credit > 0:  # credit: accept slightly less to fill faster
            raw_limit = raw_limit * (1 - ORDER_MID_NUDGE)
        else:                # debit: pay slightly more to fill faster
            raw_limit = raw_limit * (1 + ORDER_MID_NUDGE)
        limit_price  = round(max(raw_limit, 0.01), 2)

        ibkr_order = LimitOrder(
            action=outer_action,
            totalQuantity=num_contracts,
            lmtPrice=limit_price,
            tif="DAY",
        )

        with ibkr_lock:
            trade_obj = get_ib().placeOrder(bag, ibkr_order)

    except Exception as exc:
        print(f"    [{stock_code}] BAG placeOrder failed: {exc}")
        log_alert("CRITICAL",
                  f"Combo order submission failed for {stock_code}: {exc}",
                  {"strategy": strategy, "error": str(exc)})
        return None

    order_id = trade_obj.order.orderId
    print(f"    [{stock_code}] {strategy} BAG order submitted — "
          f"orderId={order_id}, {num_contracts}x combo @ lim {limit_price:.2f}")

    # ── Error 201 retry (paper trading only) ──────────────────────────────
    # TWS paper trading rejects BAG combo orders with Error 201 ~7-11 s after
    # submission. advancedErrorOverride only works as a POST-rejection retry —
    # TWS ignores it when set preemptively on the first submission.
    # In live trading Error 201 never fires; is_paper() gates the wait so live
    # orders add zero overhead.
    from agent.ibkr_client import is_paper as _is_paper
    if _is_paper():
        import time as _t
        _t.sleep(10)                     # wait outside lock (safe — same pattern as wait_for_fill)
        with ibkr_lock:
            get_ib().sleep(0)            # flush pending ib_insync events

        _status = trade_obj.orderStatus.status.lower()
        if _status in ("cancelled", "canceled", "inactive"):
            _err201 = next(
                (lg for lg in reversed(trade_obj.log) if lg.errorCode == 201),
                None,
            )
            if _err201 and "8229=COMBOPAYOUT" in (_err201.message or ""):
                print(f"    [{stock_code}] Error 201 — retrying with advancedErrorOverride...")
                retry_order = LimitOrder(
                    action=outer_action,
                    totalQuantity=num_contracts,
                    lmtPrice=limit_price,
                    tif="DAY",
                )
                try:
                    retry_order.advancedErrorOverride = "8229=COMBOPAYOUT"
                except AttributeError:
                    pass
                with ibkr_lock:
                    trade_obj = get_ib().placeOrder(bag, retry_order)
                order_id = trade_obj.order.orderId
                print(f"    [{stock_code}] Retry submitted — orderId={order_id}")
                _t.sleep(4)              # brief settle check
                with ibkr_lock:
                    get_ib().sleep(0)
                if trade_obj.orderStatus.status.lower() in ("cancelled", "canceled", "inactive"):
                    print(f"    [{stock_code}] Retry also rejected — order cannot be placed.")
                    return None
            else:
                reason = (trade_obj.log[-1].message[:100]
                          if trade_obj.log else "unknown cancel reason")
                print(f"    [{stock_code}] Order cancelled (non-201): {reason}")
                return None
    # ── End retry block ───────────────────────────────────────────────────

    # Return immediately — monitor tracks fill via _get_fill_details().
    # TP/CL use estimated net_credit from chain mid prices; monitor updates
    # net_credit_per_spread / tp_value / cl_value on first confirmed fill.
    nc = net_credit

    from agent.constants import (
        TP_CREDIT_REMAINING, TP_DEBIT_MULT, TP_STRADDLE_MULT,
        CL_CREDIT_MULT, CL_DEBIT_REMAINING, CL_STRADDLE_REMAINING,
    )
    if strategy in ("Bull Put Spread", "Bear Call Spread", "Iron Condor",
                    "Cash-Secured Put", "Covered Call"):
        tp_value = nc * (1 - TP_CREDIT_REMAINING) * num_contracts * mult
        cl_value = -nc * (CL_CREDIT_MULT - 1) * num_contracts * mult
    elif strategy in ("Bull Call Spread", "Bear Put Spread"):
        debit    = abs(nc)
        tp_value =  debit * (TP_DEBIT_MULT - 1)      * num_contracts * mult
        cl_value = -debit * (1 - CL_DEBIT_REMAINING) * num_contracts * mult
    else:  # Long Straddle
        debit    = abs(nc)
        tp_value =  debit * (TP_STRADDLE_MULT - 1)      * num_contracts * mult
        cl_value = -debit * (1 - CL_STRADDLE_REMAINING) * num_contracts * mult

    pending_legs = [
        {
            "order_id":       order_id,
            "conid":          leg.get("conid", 0),
            "limit_price":    round(_limit_price(leg["bid"], leg["ask"], leg["side"]), 2),
            "avg_fill_price": 0.0,
            "qty":            num_contracts,
            **{k: v for k, v in leg.items() if k != "conid"},
        }
        for leg in legs
    ]

    return {
        "stock_code":            stock_code,
        "strategy":              strategy,
        "market":                "US",
        "trade_type":            "options",
        "legs":                  pending_legs,
        "num_contracts":         num_contracts,
        "multiplier":            mult,
        "net_credit_per_spread": round(nc, 4),
        "tp_value":              round(tp_value, 2),
        "cl_value":              round(cl_value, 2),
        "exp_date":              exp_date,
        "placement_status":      "pending_fill",
    }


# ── Stock order placement ──────────────────────────────────────────────────────

def place_stock_order(stock_code: str, side: str, qty: int,
                      current_price: float, account: dict,
                      order_type: str = "LIMIT") -> dict | None:
    """Place a stock buy or sell order via IBKR.

    order_type: 'LIMIT' (default) or 'MARKET'.
    Returns a trade record dict or None on failure.
    """
    from ib_insync import LimitOrder, MarketOrder as IBMarketOrder
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.data_fetcher import _to_ibkr_stock

    # Get live mid price for limit orders
    mid_price = round(current_price, 3)
    try:
        stock_con = _to_ibkr_stock(stock_code)
        with ibkr_lock:
            ib    = get_ib()
            ib.qualifyContracts(stock_con)
            tkrs  = ib.reqTickers(stock_con)
            ib.sleep(1)
        if tkrs:
            t   = tkrs[0]
            bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
            ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
            if bid > 0 and ask > 0:
                mid_price = _limit_price(bid, ask, side)
        with ibkr_lock:
            get_ib().cancelMktData(stock_con)
    except Exception:
        pass   # fall back to caller-supplied current_price

    order_cost = mid_price * qty
    ok, reason = margin_safe_to_trade(account, order_cost)
    if not ok:
        bp         = account.get("buying_power", 0)
        max_budget = bp if bp > 0 else (
            account.get("total_assets", 0) or account.get("cash", 0)
        )
        if max_budget > 0 and mid_price > 0 and qty > 1:
            reduced_qty = int(max_budget / mid_price)
            if reduced_qty >= 1:
                ok2, _ = margin_safe_to_trade(account, mid_price * reduced_qty)
                if ok2:
                    print(f"    [{stock_code}] Margin-adjusted: "
                          f"{qty} → {reduced_qty} share(s)")
                    qty        = reduced_qty
                    order_cost = mid_price * reduced_qty
                else:
                    print(f"    SKIP {stock_code}: {reason}")
                    return None
            else:
                print(f"    SKIP {stock_code}: {reason}")
                return None
        else:
            print(f"    SKIP {stock_code}: {reason}")
            return None

    try:
        stock_con = _to_ibkr_stock(stock_code)
        if order_type == "MARKET":
            ibkr_order = IBMarketOrder(
                action=side,
                totalQuantity=qty,
                tif="DAY",
            )
        else:
            ibkr_order = LimitOrder(
                action=side,
                totalQuantity=qty,
                lmtPrice=round(mid_price, 2),
                tif="DAY",
            )
        with ibkr_lock:
            ib = get_ib()
            ib.qualifyContracts(stock_con)
            trade_obj = ib.placeOrder(stock_con, ibkr_order)
    except Exception as exc:
        print(f"    Order FAILED for {stock_code}: {exc}")
        return None

    order_id       = trade_obj.order.orderId
    order_type_lbl = "MKT" if order_type == "MARKET" else f"LMT@{mid_price:.3f}"
    print(f"    Placed {side} {qty}x {stock_code} {order_type_lbl}  orderId={order_id}")

    return {
        "stock_code":       stock_code,
        "strategy":         "Stock Buy" if side == "BUY" else "Stock Short",
        "market":           "HK" if stock_code.startswith("HK.") else "US",
        "trade_type":       "stock",
        "side":             side,
        "qty":              qty,
        "limit_price":      mid_price,
        "order_id":         order_id,
        "cost":             round(mid_price * qty, 2),
        "placement_status": "pending_fill",
    }
