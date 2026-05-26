"""
Trade roller — IBKR version.

Rolling logic:
  1. Close the current spread (buy-to-close short legs, sell-to-close long legs).
  2. Open the same strategy at the next available expiry with same or adjusted strikes.
  3. Only proceed if the roll can be done for net credit or within ROLL_MAX_DEBIT_PCT
     of the original credit received.

IBKR changes vs Alpaca:
- _next_expiry(): uses reqSecDefOptParams() to enumerate available expirations.
- _get_live_prices(): uses reqTickers() instead of OptionSnapshotRequest.
- evaluate_roll(): full 2-step IBKR chain fetch (reqSecDefOptParams + reqTickers)
  for the specific next expiry date.
"""

import time
import pandas as pd
from datetime import date as _date, timedelta

from agent.constants import ROLL_MAX_DEBIT_PCT, ROLL_MIN_DTE, IBKR_OPT_TICKER_TIMEOUT
from agent.options_analyzer import _parse_occ, _to_occ_symbol
from agent.order_executor import select_spread_legs, place_spread
from agent.position_manager import close_trade, add_trade, update_trade
from agent.trade_monitor import close_spread
from agent.market_hours import is_us_market_open


def _next_expiry(current_exp: str, ticker: str) -> str | None:
    """Return the nearest available expiry after current_exp via IBKR reqSecDefOptParams."""
    from ib_insync import Stock
    from agent.ibkr_client import get_ib, ibkr_lock

    current_date = _date.fromisoformat(current_exp[:10])
    next_min     = current_date + timedelta(days=1)
    next_max     = current_date + timedelta(days=91)

    try:
        stock_con = Stock(ticker, "SMART", "USD")
        with ibkr_lock:
            ib = get_ib()
            ib.qualifyContracts(stock_con)
            chains = ib.reqSecDefOptParams(ticker, "", "STK", stock_con.conId)
    except Exception as exc:
        print(f"  [roller] _next_expiry {ticker}: {exc}")
        return None

    if not chains:
        return None

    chain_info = next((c for c in chains if c.exchange == "SMART"), None) or chains[0]

    def _parse_exp(e: str) -> _date:
        return _date(int(e[:4]), int(e[4:6]), int(e[6:8]))

    valid_exps = sorted([
        _parse_exp(e) for e in chain_info.expirations
        if next_min <= _parse_exp(e) <= next_max
    ])

    return valid_exps[0].isoformat() if valid_exps else None


def _get_live_prices(legs: list[dict]) -> dict[str, tuple[float, float]]:
    """Return {occ_symbol: (bid, ask)} using IBKR reqTickers."""
    from ib_insync import Option
    from agent.ibkr_client import get_ib, ibkr_lock

    codes = [l["code"] for l in legs if l.get("code")]
    if not codes:
        return {}

    opt_cons:    list[Option] = []
    valid_codes: list[str]   = []
    for code in codes:
        parsed = _parse_occ(code)
        if not parsed:
            continue
        exp_str = parsed["exp_date"].strftime("%Y%m%d")
        con     = Option(parsed["ticker"], exp_str,
                         parsed["strike"], parsed["call_or_put"], "SMART")
        # Use stored conid when available to skip re-qualification
        cid = next((int(l.get("conid", 0) or 0) for l in legs if l.get("code") == code), 0)
        if cid > 0:
            con.conId = cid
        opt_cons.append(con)
        valid_codes.append(code)

    if not opt_cons:
        return {}

    try:
        with ibkr_lock:
            ib         = get_ib()
            ib.qualifyContracts(*opt_cons)
            tik_result = ib.reqTickers(*opt_cons)
            ib.sleep(IBKR_OPT_TICKER_TIMEOUT)
        with ibkr_lock:
            ib2 = get_ib()
            for con in opt_cons:
                try:
                    ib2.cancelMktData(con)
                except Exception:
                    pass
    except Exception as exc:
        print(f"  [roller] _get_live_prices: {exc}")
        return {}

    cid_to_code = {con.conId: code for code, con in zip(valid_codes, opt_cons)}
    result: dict[str, tuple[float, float]] = {}
    for t in tik_result:
        if not t.contract:
            continue
        code = cid_to_code.get(t.contract.conId)
        if code is None:
            continue
        bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
        ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
        result[code] = (bid, ask)

    return result


def _net_roll_credit(current_trade: dict, new_legs: list[dict]) -> tuple[float, float]:
    """Estimate net credit from closing current + opening new position."""
    live_prices = _get_live_prices(current_trade["legs"])

    original_credit = float(current_trade.get("net_credit_per_spread", 0))
    num_contracts   = current_trade.get("num_contracts", 1)
    multiplier      = current_trade.get("multiplier", 100)

    close_cost = 0.0
    for leg in current_trade["legs"]:
        code = leg["code"]
        if code in live_prices:
            b, a = live_prices[code]
            mid  = (b + a) / 2 if b > 0 and a > 0 else (leg.get("bid", 0) + leg.get("ask", 0)) / 2
        else:
            mid = (leg.get("bid", 0) + leg.get("ask", 0)) / 2
        close_cost += mid if leg["side"] == "SELL" else -mid

    close_pnl = (original_credit - close_cost) * num_contracts * multiplier

    open_pnl = 0.0
    for leg in new_legs:
        mid = (leg["bid"] + leg["ask"]) / 2
        open_pnl += -mid if leg["side"] == "BUY" else mid

    net_credit = open_pnl - close_cost
    return net_credit, close_pnl


def evaluate_roll(trade: dict) -> dict:
    """Decide whether to roll a position. Uses IBKR 2-step chain fetch."""
    from ib_insync import Stock, Option
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.data_fetcher import fetch_kline

    stock_code  = trade["stock_code"]
    strategy    = trade["strategy"]
    current_exp = trade.get("exp_date", "")

    _FAIL = lambda reason: {
        "should_roll": False, "reason": reason,
        "new_exp": None, "new_legs": None, "net_credit": 0, "close_pnl": 0.0,
    }

    next_exp = _next_expiry(current_exp, stock_code)
    if next_exp is None:
        return _FAIL("No next expiry available.")

    next_exp_date = _date.fromisoformat(next_exp[:10])
    next_exp_str  = next_exp_date.strftime("%Y%m%d")

    # ── Current stock price ───────────────────────────────────────────────────
    current_price = 0.0
    try:
        stock_con = Stock(stock_code, "SMART", "USD")
        with ibkr_lock:
            ib       = get_ib()
            ib.qualifyContracts(stock_con)
            stk_tiks = ib.reqTickers(stock_con)
            ib.sleep(2)
            try:
                ib.cancelMktData(stock_con)
            except Exception:
                pass
        if stk_tiks:
            t   = stk_tiks[0]
            bid = float(t.bid)  if t.bid  and t.bid  > 0 else 0.0
            ask = float(t.ask)  if t.ask  and t.ask  > 0 else 0.0
            last = float(t.last) if t.last and t.last > 0 else 0.0
            if bid > 0 and ask > 0:
                current_price = (bid + ask) / 2
            elif last > 0:
                current_price = last
    except Exception as exc:
        print(f"  [roller] stock price fetch: {exc}")

    if current_price <= 0:
        df = fetch_kline(stock_code, days=5)
        if df is not None and not df.empty:
            current_price = float(df["close"].iloc[-1])

    if current_price <= 0:
        return _FAIL("Could not fetch current price.")

    # ── Chain metadata for next expiry ────────────────────────────────────────
    try:
        stock_con = Stock(stock_code, "SMART", "USD")
        with ibkr_lock:
            ib     = get_ib()
            ib.qualifyContracts(stock_con)
            chains = ib.reqSecDefOptParams(stock_code, "", "STK", stock_con.conId)
    except Exception as exc:
        return _FAIL(f"reqSecDefOptParams error: {exc}")

    if not chains:
        return _FAIL("No chain info from IBKR.")

    chain_info = next((c for c in chains if c.exchange == "SMART"), None) or chains[0]

    if next_exp_str not in chain_info.expirations:
        return _FAIL(f"Expiry {next_exp} not available in IBKR chain.")

    strike_lo = current_price * 0.60
    strike_hi = current_price * 1.40
    valid_strikes = sorted([s for s in chain_info.strikes if strike_lo <= s <= strike_hi])

    if not valid_strikes:
        return _FAIL("No strikes in ±40% range.")

    # ── Build option contracts + fetch live greeks ────────────────────────────
    option_contracts = []
    for right in ("C", "P"):
        for strike in valid_strikes:
            con = Option(stock_code, next_exp_str, strike, right, "SMART",
                         tradingClass=chain_info.tradingClass)
            option_contracts.append(con)

    try:
        with ibkr_lock:
            ib         = get_ib()
            ib.qualifyContracts(*option_contracts)
            opt_tickers = ib.reqTickers(*option_contracts)
            ib.sleep(IBKR_OPT_TICKER_TIMEOUT)
        with ibkr_lock:
            ib2 = get_ib()
            for con in option_contracts:
                try:
                    ib2.cancelMktData(con)
                except Exception:
                    pass
    except Exception as exc:
        return _FAIL(f"Chain data fetch error: {exc}")

    rows = []
    for t in opt_tickers:
        con = t.contract
        if not con or not hasattr(con, "right"):
            continue
        bid = float(t.bid) if t.bid and t.bid > 0 else 0.0
        ask = float(t.ask) if t.ask and t.ask > 0 else 0.0
        g   = t.modelGreeks
        delta = float(g.delta)      if g and g.delta      is not None else 0.0
        theta = float(g.theta)      if g and g.theta      is not None else 0.0
        iv    = float(g.impliedVol) * 100 if g and g.impliedVol is not None else 0.0
        exp_s = con.lastTradeDateOrContractMonth
        exp_d = _date(int(exp_s[:4]), int(exp_s[4:6]), int(exp_s[6:8]))
        rows.append({
            "code":               _to_occ_symbol(stock_code, exp_d, con.right, float(con.strike)),
            "strike_price":       float(con.strike),
            "call_or_put":        con.right,
            "exp_date":           exp_d.isoformat(),
            "bid":                bid,
            "ask":                ask,
            "delta":              delta,
            "theta":              theta,
            "implied_volatility": iv,
            "_ibkr_conid":        int(con.conId) if con.conId else 0,
        })

    if not rows:
        return _FAIL("Next-expiry chain produced 0 rows.")

    chain = pd.DataFrame(rows)
    chain["strike_price"] = pd.to_numeric(chain["strike_price"], errors="coerce")
    chain = chain[chain["exp_date"] == next_exp].copy()

    new_legs = select_spread_legs(chain, strategy, current_price)
    if new_legs is None:
        return _FAIL("Could not select legs for next expiry.")

    net_credit, close_pnl = _net_roll_credit(trade, new_legs)
    original_credit       = abs(trade.get("net_credit_per_spread", 0))
    max_acceptable_debit  = -original_credit * ROLL_MAX_DEBIT_PCT

    if net_credit >= max_acceptable_debit:
        label   = (f"net credit={net_credit:.4f}" if net_credit >= 0
                   else f"net debit={abs(net_credit):.4f} (within {ROLL_MAX_DEBIT_PCT*100:.0f}% limit)")
        roll_ok = {"should_roll": True,
                   "reason":      f"Roll to {next_exp[:10]}: {label}",
                   "new_exp":     next_exp, "new_legs": new_legs,
                   "net_credit":  net_credit, "close_pnl": close_pnl}
    else:
        roll_ok = None

    # ── Wheel cost basis check (Covered Call only) ─────────────────────────────
    if strategy == "Covered Call" and "wheel_assignment_price" in trade:
        assignment_price = float(trade["wheel_assignment_price"])
        premium_accum    = float(trade.get("wheel_premium_accumulated", 0.0))
        num_c            = trade.get("num_contracts", 1)
        mult             = trade.get("multiplier", 100)

        net_cost_basis = assignment_price - premium_accum / (num_c * mult)

        new_cc_strike = next(
            (l["strike"] for l in new_legs if l["side"] == "SELL"), None
        )
        if new_cc_strike is not None and new_cc_strike < net_cost_basis:
            new_cc_premium = next(
                ((l["bid"] + l["ask"]) / 2 for l in new_legs if l["side"] == "SELL"),
                0.0,
            )
            effective_basis_after_roll = net_cost_basis - new_cc_premium
            if new_cc_strike >= effective_basis_after_roll:
                print(f"    [wheel] Roll strike ${new_cc_strike:.2f} < cost basis "
                      f"${net_cost_basis:.2f}, but new CC premium ${new_cc_premium:.4f}/sh "
                      f"covers the gap (effective basis after roll: "
                      f"${effective_basis_after_roll:.2f}) — proceeding.")
                if roll_ok is None:
                    roll_ok = {"should_roll": True,
                               "reason": (f"Roll to {next_exp[:10]}: strike below cost basis but "
                                          f"premium bridges gap — effective basis "
                                          f"${effective_basis_after_roll:.2f}"),
                               "new_exp": next_exp, "new_legs": new_legs,
                               "net_credit": net_credit, "close_pnl": close_pnl}
            else:
                return {
                    "should_roll": False,
                    "reason": (
                        f"[wheel] Roll to ${new_cc_strike:.2f} is below net cost basis "
                        f"${net_cost_basis:.2f} (assignment=${assignment_price:.2f} "
                        f"- premiums=${premium_accum:.2f}/{num_c*mult}sh). "
                        f"New CC premium ${new_cc_premium:.4f}/sh won't bridge the gap "
                        f"(effective basis after roll: ${effective_basis_after_roll:.2f}). "
                        f"Closing to limit further loss."
                    ),
                    "new_exp":    next_exp, "new_legs": new_legs,
                    "net_credit": net_credit, "close_pnl": close_pnl,
                }

    if roll_ok is not None:
        return roll_ok

    return {"should_roll": False,
            "reason":      (f"Roll debit={abs(net_credit):.4f} exceeds "
                            f"limit={abs(max_acceptable_debit):.4f}. Close instead."),
            "new_exp":     next_exp, "new_legs": new_legs,
            "net_credit":  net_credit, "close_pnl": close_pnl}


def execute_roll(trade: dict, new_legs: list[dict], new_exp: str,
                 account: dict, net_credit: float, close_pnl: float = 0.0) -> bool:
    """Close the current spread and open the rolled position. Returns True on success."""
    stock_code = trade["stock_code"]
    strategy   = trade["strategy"]

    print(f"    Rolling {stock_code} {strategy} → expiry {new_exp[:10]}")

    ok = close_spread(trade, f"roll to {new_exp[:10]}")
    if not ok:
        print(f"    Roll FAILED: could not close current position.")
        return False

    close_trade(stock_code, "rolled", close_pnl)
    time.sleep(1.0)

    num_contracts = trade.get("num_contracts", 1)

    new_trade = place_spread(
        stock_code=stock_code,
        legs=new_legs,
        num_contracts=num_contracts,
        account=account,
        strategy=strategy,
        exp_date=new_exp,
    )

    if new_trade is None:
        print(f"    Roll FAILED: could not open new position.")
        return False

    new_trade["rolled_from"] = trade.get("exp_date", "")
    new_trade["roll_credit"] = round(net_credit, 4)

    if "wheel_assignment_price" in trade:
        num_c = trade.get("num_contracts", 1)
        mult  = trade.get("multiplier", 100)
        new_trade["wheel_assignment_price"]    = trade["wheel_assignment_price"]
        new_trade["wheel_premium_accumulated"] = round(
            float(trade.get("wheel_premium_accumulated", 0.0))
            + net_credit * num_c * mult,
            2,
        )

    add_trade(new_trade)
    print(f"    Roll SUCCESS: opened {strategy} expiring {new_exp[:10]}")
    return True


def run_roll_checks(open_trades: list[dict], account_us: dict) -> None:
    """Evaluate and execute rolls for all trades flagged for rolling."""
    roll_candidates = [t for t in open_trades if t.get("_pending_action") == "roll"]
    if not roll_candidates:
        return

    import threading
    from agent.trade_monitor import (
        _close_spread_thread, _active_close_threads, _close_threads_lock, _days_to_expiry,
    )
    from agent.position_manager import update_trade as _ut
    from datetime import datetime as _dt

    for trade in roll_candidates:
        if not is_us_market_open():
            print(f"\n  [{trade['stock_code']}] US market closed — roll deferred to next cycle.")
            continue

        sc  = trade["stock_code"]
        acc = account_us

        dte = _days_to_expiry(trade.get("exp_date", ""))
        if dte is not None and dte <= ROLL_MIN_DTE:
            print(f"\n  [{sc}] DTE={dte} <= ROLL_MIN_DTE={ROLL_MIN_DTE}: "
                  f"roll deadline passed — closing position instead of rolling.")
            _ut(sc, {
                "_pending_close_reason": "roll_expired",
                "_pending_close_pnl":    0.0,
                "_pending_close_date":   _dt.now().date().isoformat(),
            })
            with _close_threads_lock:
                already = sc in _active_close_threads and _active_close_threads[sc].is_alive()
            if not already:
                t = threading.Thread(
                    target=_close_spread_thread,
                    args=(trade, "roll_expired"),
                    name=f"close-{sc}",
                    daemon=True,
                )
                with _close_threads_lock:
                    _active_close_threads[sc] = t
                t.start()
                print(f"    Roll-expired close thread started for {sc}.")
            else:
                print(f"    Roll-expired close thread already running for {sc}.")
            time.sleep(0.5)
            continue

        print(f"\n  Evaluating roll for {sc} ({trade['strategy']})...")
        result = evaluate_roll(trade)
        print(f"    {result['reason']}")

        if result["should_roll"]:
            execute_roll(
                trade=trade,
                new_legs=result["new_legs"],
                new_exp=result["new_exp"],
                account=acc,
                net_credit=result["net_credit"],
                close_pnl=result.get("close_pnl", 0.0),
            )
        else:
            _ut(sc, {
                "_pending_close_reason": "roll_declined",
                "_pending_close_pnl":    round(result.get("close_pnl", 0.0), 2),
                "_pending_close_date":   _dt.now().date().isoformat(),
            })

            with _close_threads_lock:
                already = (sc in _active_close_threads
                           and _active_close_threads[sc].is_alive())
            if not already:
                t = threading.Thread(
                    target=_close_spread_thread,
                    args=(trade, "roll_declined"),
                    name=f"close-{sc}",
                    daemon=True,
                )
                with _close_threads_lock:
                    _active_close_threads[sc] = t
                t.start()
                print(f"    Roll declined — async close thread started for {sc}.")
            else:
                print(f"    Roll declined — close thread already running for {sc}.")

        time.sleep(0.5)
