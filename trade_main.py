"""
trade_main.py — Entry point for the trading agent.

Modes:
  python trade_main.py run          — Autonomous loop: scan + trade + monitor (default)
  python trade_main.py scan         — One-shot scan + execute
  python trade_main.py monitor      — Monitor open trades on a 30-min loop
  python trade_main.py monitor_once — Single monitor check
  python trade_main.py status       — Print open trades and account summary
"""

import sys
import threading
import time
import os
import logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.trading_agent import run_agent, scan_hk
from agent.risk_manager import get_account_info, compute_allocation, print_account_summary
from agent.position_manager import (
    has_open_trade, add_trade, get_open_trades, print_open_trades,
    has_open_russell_trade, is_in_session_cooldown,
)
from agent.russell_scanner import run_russell_scan
from agent.politician_scanner import run_politician_scan
from agent.category_tracker import compute_category_stats
from agent.category_report import print_category_summary, generate_category_report
from agent.order_executor import (
    select_spread_legs, compute_contracts, place_spread,
    compute_shares, place_stock_order,
)
from agent.options_analyzer import get_options_data
from agent.trade_monitor import run_monitor
from agent.wheel_strategy import run_wheel_scan
from agent.trade_roller import run_roll_checks
from agent.report import save_report, print_summary
from agent.daily_report import generate_daily_report
from agent.market_hours import (
    is_us_market_open, is_hk_market_open, is_past_open_buffer, market_today,
    market_status_line, next_scan_trigger,
)
from agent.constants import (
    MONITOR_INTERVAL_MINUTES, AGENT_LOOP_INTERVAL_MINS, OPEN_BUFFER_MINS,
    RESCAN_MIN_SCORE, US_SPREAD_MIN_SIGNAL, STOCK_MIN_ENTRY_SCORE,
    TRADE_US_SPREADS_ENABLED,
    TRADE_WHEEL_CSP_ENABLED, TRADE_WHEEL_CC_ENABLED,
    TRADE_RUSSELL_ENABLED, TRADE_POLITICIAN_ENABLED,
    TRADE_GEM_ENABLED, TRADE_HK_ENABLED,
)

# ── Scan-state persistence ─────────────────────────────────────────────────────

_SCAN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "scan_state.json")


def _load_scan_state() -> dict:
    import json
    state: dict = {"US": None, "HK": None}
    if not os.path.exists(_SCAN_STATE_FILE):
        return state
    try:
        with open(_SCAN_STATE_FILE, "r") as f:
            saved = json.load(f)
        saved_us = saved.get("US")
        if saved_us and str(market_today("US")) == saved_us:
            state["US"] = market_today("US")
            print(f"  [scan-state] US already scanned today ({saved_us}) — skip.")
        saved_hk = saved.get("HK")
        if saved_hk and str(market_today("HK")) == saved_hk:
            state["HK"] = market_today("HK")
            print(f"  [scan-state] HK already scanned today ({saved_hk}) — skip.")
    except Exception as exc:
        print(f"  [scan-state] Could not load scan_state.json: {exc}")
    return state


def _save_scan_state(last_scanned: dict) -> None:
    import json
    tmp = _SCAN_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({k: str(v) if v is not None else None
                       for k, v in last_scanned.items()}, f, indent=2)
        os.replace(tmp, _SCAN_STATE_FILE)
    except Exception as exc:
        print(f"  [scan-state] Could not save scan_state.json: {exc}")


# ── Daily scan-results cache ───────────────────────────────────────────────────

_SCAN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "scan_results_cache.json")

_CANDIDATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "daily_candidates.json")


def _utc_today() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).date().isoformat()


def _load_scan_cache() -> list[dict]:
    import json
    if not os.path.exists(_SCAN_CACHE_FILE):
        return []
    try:
        with open(_SCAN_CACHE_FILE, "r") as f:
            cached = json.load(f)
        if cached.get("date") == _utc_today():
            return cached.get("results", [])
    except Exception as exc:
        print(f"  [scan-cache] Could not load cache: {exc}")
    return []


def _save_scan_cache(results: list[dict]) -> None:
    import json
    tmp = _SCAN_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"date": _utc_today(), "results": results},
                      f, indent=2, default=str)
        os.replace(tmp, _SCAN_CACHE_FILE)
    except Exception as exc:
        print(f"  [scan-cache] Could not save cache: {exc}")


def _save_daily_candidates(candidates: list[dict]) -> None:
    import json
    tmp = _CANDIDATES_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"date": _utc_today(), "candidates": candidates},
                      f, indent=2, default=str)
        os.replace(tmp, _CANDIDATES_FILE)
    except Exception as exc:
        print(f"  [candidates] Could not save daily_candidates.json: {exc}")


def _load_daily_candidates() -> list[dict]:
    import json
    if not os.path.exists(_CANDIDATES_FILE):
        return []
    try:
        with open(_CANDIDATES_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == _utc_today():
            return data.get("candidates", [])
    except Exception as exc:
        print(f"  [candidates] Could not load daily_candidates.json: {exc}")
    return []


def _banner(msg: str) -> None:
    print(f"\n{'=' * 60}\n  {msg}\n{'=' * 60}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_weighted_alloc_map(scan_results: list[dict],
                              account_us: dict | None) -> dict[str, dict]:
    """Pre-compute score-weighted budgets for every candidate, keyed by stock code."""
    from agent.risk_manager import compute_weighted_allocations

    alloc_map: dict[str, dict] = {}
    if account_us is not None:
        mkt_results = sorted(
            [r for r in scan_results if r.get("Market") == "US"],
            key=lambda r: r.get("Composite Score", 0),
            reverse=True,
        )
        compute_weighted_allocations(mkt_results, account_us)
        for result in mkt_results:
            if "alloc" in result:
                alloc_map[result["Code"]] = result["alloc"]
    return alloc_map


def _build_trade_plan(scan_results: list[dict],
                      account_us: dict | None) -> list[dict]:

    alloc_map = _build_weighted_alloc_map(scan_results, account_us)

    plan = []
    for result in scan_results:
        stock_code      = result["Code"]
        strategy        = result["Strategy"]
        market          = result["Market"]
        bias            = result.get("Bias", "Bullish")
        exp_date        = result.get("Exp Date", "")
        composite_score = float(result.get("Composite Score", 0))

        if market == "US" and not TRADE_US_SPREADS_ENABLED:
            print(f"  SKIP {stock_code}: US Spreads channel disabled.")
            continue

        if market != "US":
            print(f"  SKIP {stock_code}: non-US market ({market}) not supported.")
            continue

        _acct = account_us
        _will_options = bool(_acct and _acct.get("supports_options"))
        if has_open_trade(stock_code, trade_type="options" if _will_options else "stock"):
            print(f"  SKIP {stock_code}: existing open trade.")
            continue

        _NEUTRAL_STRATEGIES = {"Iron Condor", "Long Straddle"}
        if strategy not in _NEUTRAL_STRATEGIES:
            if abs(composite_score) < US_SPREAD_MIN_SIGNAL:
                print(f"  SKIP {stock_code}: signal {composite_score:+.4f} below "
                      f"US_SPREAD_MIN_SIGNAL ({US_SPREAD_MIN_SIGNAL:+.2f}) "
                      f"for {strategy}.")
                continue

        account = account_us
        if account is None:
            print(f"  SKIP {stock_code}: could not fetch account info.")
            continue

        alloc = alloc_map.get(stock_code)
        if not alloc:
            print(f"  SKIP {stock_code}: no allocation computed (budget unavailable).")
            continue

        # ── Options trade ─────────────────────────────────────────────────────
        if account.get("supports_options", False):
            opts = get_options_data(stock_code)
            chain         = opts.get("chain")
            current_price = opts.get("current_price", result.get("Price", 0))
            exp           = opts.get("exp_date", exp_date)

            if chain is None or chain.empty:
                print(f"  SKIP {stock_code}: no options chain data.")
                continue

            legs = select_spread_legs(chain, strategy, current_price)
            if legs is None:
                print(f"  SKIP {stock_code}: could not select legs for {strategy}.")
                continue

            num_contracts = compute_contracts(legs, alloc["per_trade"], strategy)
            if num_contracts < 1:
                # Diagnose: compute approximate cost per spread for the log
                _mid  = lambda b, a: (b + a) / 2
                _buy  = [l for l in legs if l["side"] == "BUY"]
                _sell = [l for l in legs if l["side"] == "SELL"]
                _nc   = sum(_mid(l["bid"], l["ask"]) for l in _buy) - \
                        sum(_mid(l["bid"], l["ask"]) for l in _sell)
                _sw   = (abs(_sell[0]["strike"] - _buy[0]["strike"])
                         if _sell and _buy else 0)
                _cost = round(max((_sw - abs(_nc)) * 100, 0.01) if _nc < 0 else abs(_nc) * 100, 2)
                print(f"  SKIP {stock_code}: budget too small for 1 contract "
                      f"(per_trade={alloc['per_trade']:,.2f}  "
                      f"min_cost≈{_cost:,.2f}  spread_width={_sw:.2f}  net={_nc:.3f}).")
                continue

            plan.append({
                "trade_type":      "options",
                "stock_code":      stock_code,
                "strategy":        strategy,
                "market":          market,
                "exp_date":        exp,
                "legs":            legs,
                "num_contracts":   num_contracts,
                "account":         account,
                "alloc":           alloc,
                "current_price":   current_price,
                "composite_score": result.get("Composite Score", "N/A"),
            })

        # ── Stock trade ────────────────────────────────────────────────────────
        else:
            if bias == "Neutral":
                print(f"  SKIP {stock_code}: neutral bias, no clear stock direction.")
                continue

            if abs(composite_score) < STOCK_MIN_ENTRY_SCORE:
                print(f"  SKIP {stock_code}: score {composite_score:.3f} < "
                      f"STOCK_MIN_ENTRY_SCORE ({STOCK_MIN_ENTRY_SCORE})")
                continue

            side = "BUY" if bias == "Bullish" else "SELL"
            if side == "SELL" and account.get("acc_type", "CASH") == "CASH":
                print(f"  SKIP {stock_code}: bearish signal but cash account cannot short-sell.")
                continue

            current_price = float(result.get("Price", 0))
            if current_price <= 0:
                print(f"  SKIP {stock_code}: invalid price.")
                continue

            qty = compute_shares(current_price, alloc["per_trade"], lot_size=1)
            if qty < 1:
                print(f"  SKIP {stock_code}: budget too small for 1 share.")
                continue

            plan.append({
                "trade_type":      "stock",
                "stock_code":      stock_code,
                "strategy":        f"Stock {side.capitalize()}",
                "market":          market,
                "side":            side,
                "qty":             qty,
                "lot_size":        1,
                "account":         account,
                "alloc":           alloc,
                "current_price":   current_price,
                "composite_score": result.get("Composite Score", "N/A"),
            })

        time.sleep(0.3)

    return plan


def _print_plan_summary(plan: list[dict]) -> None:
    _banner(f"Trade Plan  ({len(plan)} trades)")
    for i, t in enumerate(plan, 1):
        ccy        = t["alloc"]["currency"]
        weight_pct = t["alloc"].get("weight_pct", 0.0)
        weight_str = f"  ({weight_pct:.1f}% of budget)" if weight_pct > 0 else ""

        wheel_type   = t.get("wheel_type", "")
        budget_label = ("Collateral" if wheel_type == "CSP"
                        else "Est. Premium" if wheel_type == "CC"
                        else "Budget")

        print(f"\n  [{i}] {t['stock_code']}  |  {t['strategy']}  |  {t['market']}")
        print(f"       Score     : {t.get('composite_score', t.get('Composite Score', 'N/A'))}")
        print(f"       Price     : {t['current_price']:.3f}")
        print(f"       {budget_label:<10}: {ccy} {t['alloc']['per_trade']:,.2f}{weight_str}")
        if t["trade_type"] == "options":
            print(f"       Expiry    : {t['exp_date']}  x{t['num_contracts']} contracts")
            for leg in t["legs"]:
                print(f"         {leg['side']:4s}  {leg['call_or_put']}  "
                      f"K={leg['strike']:.2f}  delta={leg['delta']:.2f}  "
                      f"bid={leg['bid']:.2f}  ask={leg['ask']:.2f}")
        else:
            print(f"       {t['side']}  {t['qty']:,} shares  "
                  f"est. {ccy} {t['current_price'] * t['qty']:,.2f}")


def _execute_plan(plan: list[dict]) -> None:
    """Place trades sequentially, refreshing account info before each trade.

    Sequential execution ensures each margin check sees actual post-booking
    buying power rather than a stale snapshot shared across concurrent trades.
    """
    if not plan:
        return

    print(f"\n  Executing {len(plan)} trade(s) sequentially "
          f"(account refreshed before each trade)...")

    for t in plan:
        try:
            fresh_acct = get_account_info(
                market="HK" if t.get("market") == "HK" else "US"
            )
            if fresh_acct:
                t = {**t, "account": fresh_acct}

            print(f"\n  Placing {t['strategy']} on {t['stock_code']}  "
                  f"[bp={t['account'].get('buying_power', 0):,.0f}]...")

            if t["trade_type"] == "options":
                record = place_spread(
                    stock_code=t["stock_code"],
                    legs=t["legs"],
                    num_contracts=t["num_contracts"],
                    account=t["account"],
                    strategy=t["strategy"],
                    exp_date=t["exp_date"],
                )
            else:
                _ot = "MARKET" if t.get("order_type") == "MARKET" else "LIMIT"
                record = place_stock_order(
                    stock_code=t["stock_code"],
                    side=t["side"],
                    qty=t["qty"],
                    current_price=t["current_price"],
                    account=t["account"],
                    order_type=_ot,
                )

            if record:
                if "scan_source" in t:
                    record["scan_source"] = t["scan_source"]
                for _wf in ("wheel_assignment_price", "wheel_premium_accumulated",
                            "_politician", "_pol_score", "_filing_date", "_amount_range"):
                    if _wf in t:
                        record[_wf] = t[_wf]
                _cs = t.get("composite_score")
                if _cs is not None and _cs != "N/A":
                    try:
                        record["entry_score"] = round(float(_cs), 4)
                    except (TypeError, ValueError):
                        pass
                if record.get("legs"):
                    _iv_vals = [l.get("iv") for l in record["legs"] if l.get("iv")]
                    if _iv_vals:
                        record["entry_iv"] = round(sum(_iv_vals) / len(_iv_vals), 4)
                add_trade(record)
                if t["trade_type"] == "options":
                    print(f"    Logged: {t['stock_code']}  "
                          f"net_credit={record['net_credit_per_spread']:.4f}  "
                          f"TP={record['tp_value']:.2f}  CL={record['cl_value']:.2f}")
                else:
                    print(f"    Logged: {t['stock_code']}  {t['side']} {t['qty']} shares "
                          f"@ {record['limit_price']:.3f}  "
                          f"cost={record['cost']:,.2f}")
            else:
                print(f"    FAILED: {t['stock_code']}")
        except Exception as exc:
            print(f"    ERROR placing {t['stock_code']}: {exc}")


# ── Intra-day re-scan ─────────────────────────────────────────────────────────

def _rescan_candidates(prior_results: list[dict],
                       account_us: dict | None) -> None:
    """Re-score the day's top candidates and place trades for any still qualifying
    without an open position."""
    from agent.data_fetcher import fetch_batch, fetch_intraday_kline
    from agent.indicators import check_intraday_momentum
    from agent.scorer import score_stock

    cooled = [r["Code"] for r in prior_results if is_in_session_cooldown(r["Code"])]
    if cooled:
        print(f"  Re-scan: skipping {cooled} "
              f"(session cooldown — closed via trail_stop/cut_loss this session)")

    unfilled = [
        r for r in prior_results
        if not has_open_trade(r["Code"], trade_type="options")
        and not is_in_session_cooldown(r["Code"])
    ]
    if not unfilled:
        if not prior_results:
            print("  Re-scan: no candidates to re-score.")
        else:
            print("  Re-scan: all candidates already have open trades or are on cooldown — nothing to do.")
        return

    print(f"  Re-scan: {len(unfilled)} unfilled candidate(s) — refreshing scores...")

    # ── US spreads: build plan and execute immediately (pricing goes stale fast) ──
    executed_any = False
    if TRADE_US_SPREADS_ENABLED and is_us_market_open() and account_us:
        us_candidates = [r for r in unfilled if r["Market"] == "US"]
        if us_candidates:
            codes = [r["Code"] for r in us_candidates]
            print(f"\n  Re-scoring {len(codes)} US candidate(s): {codes}")

            kline_data = fetch_batch(codes, days=260, delay=0.3)

            fresh_results = []
            for r in us_candidates:
                code = r["Code"]
                if code not in kline_data:
                    print(f"    {code}: no kline data — skip")
                    continue
                s = score_stock(kline_data[code])
                if s is None:
                    print(f"    {code}: insufficient history — skip")
                    continue
                score = s.get("Composite Score", s.get("composite_score", 0))
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                if abs(score) < RESCAN_MIN_SCORE:
                    print(f"    {code}: score {score:+.3f} below threshold "
                          f"({RESCAN_MIN_SCORE:+.2f}) — skip")
                    continue
                print(f"    {code}: score {score:+.3f} ✓ still qualifies")

                intraday_df          = fetch_intraday_kline(code)
                gate_ok, gate_reason = check_intraday_momentum(
                    intraday_df, r.get("Strategy", ""))
                print(f"    {code}: {gate_reason}")
                if not gate_ok:
                    continue

                fresh_results.append({**r, "Composite Score": score})

            if fresh_results:
                plan = _build_trade_plan(fresh_results, account_us)
                if plan:
                    _print_plan_summary(plan)
                    # Execute immediately — options pricing goes stale within minutes
                    _banner(f"Executing {len(plan)} US spread trade(s)")
                    for t in plan:
                        t["account"] = account_us
                        _execute_plan([t])
                        _fresh = get_account_info()
                        if _fresh:
                            account_us = _fresh
                    executed_any = True
                else:
                    print("  No new US trades (all already open or budget too small).")
            else:
                print("  No US candidates still qualify after re-scoring.")

    # ── Secondary scans: run after US spreads are placed ─────────────────────────
    pol_plan:     list[dict] = []
    russell_plan: list[dict] = []
    wheel_plan:   list[dict] = []

    if TRADE_POLITICIAN_ENABLED and is_us_market_open() and account_us:
        _banner("Politician Copy-Trade — Re-scan")
        try:
            _pol = run_politician_scan(account_us)
        except Exception as _pol_exc:
            print(f"  Politician scan error: {_pol_exc}")
            _pol = []
        if _pol:
            _print_plan_summary(_pol)
            pol_plan.extend(_pol)
        else:
            print("  No politician copy trades to execute this cycle.")

    if ((TRADE_WHEEL_CSP_ENABLED or TRADE_WHEEL_CC_ENABLED)
            and is_us_market_open() and account_us
            and account_us.get("supports_options")):
        _banner("Wheel Strategy — Re-scan")
        _wh = run_wheel_scan(
            account_us,
            csp_enabled=TRADE_WHEEL_CSP_ENABLED,
            cc_enabled=TRADE_WHEEL_CC_ENABLED,
        )
        if _wh:
            wheel_plan.extend(_wh)
        else:
            print("  Wheel re-scan: no new CSP/CC trades this cycle.")

    if (TRADE_RUSSELL_ENABLED and is_us_market_open() and account_us
            and account_us.get("supports_options")
            and not has_open_russell_trade()):
        _banner("Russell 2000 — Re-scan (Opportunistic)")
        _russ = run_russell_scan(account_us)
        if _russ:
            _print_plan_summary(_russ)
            russell_plan.extend(_russ)
        else:
            print("  Russell 2000 re-scan: no qualifying candidate.")

    if pol_plan:
        _execute_plan(pol_plan)
        executed_any = True

    if russell_plan:
        _execute_plan(russell_plan)
        executed_any = True

    if wheel_plan:
        _print_plan_summary(wheel_plan)
        _execute_plan(wheel_plan)
        executed_any = True

    if not executed_any:
        print("  No re-scan trades to execute this cycle.")


# ── Scan + execute (one-shot) ─────────────────────────────────────────────────

def phase_scan_and_execute(account_us=None) -> list[dict]:
    if account_us is None:
        account_us = get_account_info()

    scan_results = run_agent(top_n=5)

    if not scan_results:
        print("  No candidates found.")
        return []

    print_summary(scan_results)

    cached = _load_scan_cache()
    new_codes = {r["Code"] for r in scan_results}
    merged = [r for r in cached if r["Code"] not in new_codes] + scan_results
    _save_scan_cache(merged)

    report_name = f"TradingReport_{_utc_today()}.xlsx"
    save_report(merged, filename=report_name)

    _pre_exec_saved = _load_daily_candidates()
    _pre_exec_codes = {r["Code"] for r in _pre_exec_saved}
    _save_daily_candidates(
        _pre_exec_saved + [r for r in scan_results if r["Code"] not in _pre_exec_codes]
    )

    main_plan = _build_trade_plan(scan_results, account_us)
    if main_plan:
        _print_plan_summary(main_plan)
    else:
        print("  No executable trades found.")

    from concurrent.futures import ThreadPoolExecutor as _TPE
    _secondary: dict[str, object] = {}
    _sec_pool = _TPE(max_workers=3)

    if is_us_market_open() and account_us and (TRADE_WHEEL_CSP_ENABLED or TRADE_WHEEL_CC_ENABLED):
        _banner("Wheel Strategy — Cash-Secured Puts & Covered Calls")
        _secondary["wheel"] = _sec_pool.submit(
            run_wheel_scan, account_us,
            csp_enabled=TRADE_WHEEL_CSP_ENABLED,
            cc_enabled=TRADE_WHEEL_CC_ENABLED,
        )

    if (TRADE_RUSSELL_ENABLED and is_us_market_open()
            and account_us and account_us.get("supports_options")
            and not has_open_russell_trade()):
        _banner("Russell 2000 — Opportunistic Single Trade (5% allocation)")
        _secondary["russell"] = _sec_pool.submit(run_russell_scan, account_us)

    if TRADE_POLITICIAN_ENABLED and is_us_market_open() and account_us:
        _banner("Politician Copy-Trade Scanner (5% allocation)")
        _secondary["politician"] = _sec_pool.submit(run_politician_scan, account_us)

    _sec_pool.shutdown(wait=True)

    if main_plan:
        _banner(f"Executing {len(main_plan)} main spread trade(s)")
        _execute_plan(main_plan)
        _fresh = get_account_info()
        if _fresh:
            account_us = _fresh
    else:
        print("  No main spread trades to execute.")

    for label, future in _secondary.items():
        try:
            cat_plan = future.result() or []
            if cat_plan:
                for _p in cat_plan:
                    if _p.get("market") == "US" and account_us:
                        _p["account"] = account_us
                _print_plan_summary(cat_plan)
                _banner(f"Executing {len(cat_plan)} {label} trade(s)")
                _execute_plan(cat_plan)
            else:
                print(f"  No {label} trades to execute.")
        except Exception as exc:
            print(f"  [{label}] scan error: {exc}")

    return scan_results


# ── Monitor ───────────────────────────────────────────────────────────────────

def phase_monitor_once(account_us=None) -> None:
    _banner("Monitor — Checking Open Trades")

    if account_us is None:
        account_us = get_account_info()

    if account_us:
        print_account_summary(account_us)

    monitor_results = run_monitor(account_us)

    open_trades = get_open_trades()
    for trade in open_trades:
        for mr in monitor_results:
            if mr.get("stock_code") == trade["stock_code"] and mr["action"] == "roll":
                trade["_pending_action"] = "roll"

    run_roll_checks(open_trades, account_us)

    _banner("Category P&L Summary")
    cat_stats = compute_category_stats(monitor_results=monitor_results)
    print_category_summary(cat_stats)


def phase_monitor_loop() -> None:
    interval = MONITOR_INTERVAL_MINUTES * 60
    print(f"\nMonitor loop — checking every {MONITOR_INTERVAL_MINUTES} min. Ctrl+C to stop.\n")
    while True:
        try:
            phase_monitor_once()
            print(f"\n  Next check in {MONITOR_INTERVAL_MINUTES} min...")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break


_PREWARM_KLINES_LOCK = threading.Lock()


def _prewarm_klines_us() -> None:
    """Fetch all US universe klines via yfinance and populate the disk cache."""
    if not _PREWARM_KLINES_LOCK.acquire(blocking=False):
        return
    try:
        from agent.universe import get_us_universe
        from agent.data_fetcher import fetch_batch
        try:
            universe = get_us_universe()
            print(f"  [pre-warm] US klines: fetching {len(universe)} stocks...")
            result = fetch_batch(universe, days=260)
            print(f"  [pre-warm] US klines: {len(result)} stocks cached.")
        except Exception as exc:
            print(f"  [pre-warm] US klines error: {exc}")
    finally:
        _PREWARM_KLINES_LOCK.release()


def _prewarm_background_data() -> None:
    """Fire daemon threads to warm all expensive caches before the next scan."""
    from agent.universe import get_us_universe
    from agent.russell_universe import get_russell2000_universe
    from agent.capitol_trades_fetcher import fetch_all_trades

    for name, fn in [
        ("US universe",      get_us_universe),
        ("Russell universe", get_russell2000_universe),
        ("Capitol Trades",   lambda: fetch_all_trades(365)),
    ]:
        threading.Thread(target=fn, name=f"prewarm-{name}", daemon=True).start()
        print(f"  [pre-warm] {name} thread started")

    threading.Thread(target=_prewarm_klines_us, name="prewarm-klines-US", daemon=True).start()
    print(f"  [pre-warm] US klines thread started")


# ── Autonomous loop (run mode) ────────────────────────────────────────────────

def phase_autonomous_loop() -> None:
    """
    Runs indefinitely:
      - Full universe scan once per trading day, starting OPEN_BUFFER_MINS after open.
      - Every subsequent 30-min loop: re-scores the day's top candidates and retries
        any unfilled slots if scores remain qualifying (>= RESCAN_MIN_SCORE).
      - Monitors open positions every AGENT_LOOP_INTERVAL_MINS minutes.
    """
    _banner("Autonomous Agent Started")
    print(f"  Scan trigger : {OPEN_BUFFER_MINS} min after market open (full scan, once per session).")
    print(f"  Re-scan      : every {AGENT_LOOP_INTERVAL_MINS} min for unfilled top-N slots.")
    print(f"  Loop interval: every {AGENT_LOOP_INTERVAL_MINS} min.")
    print("  Press Ctrl+C to stop.\n")

    last_scanned = _load_scan_state()
    daily_candidates: list[dict] = _load_daily_candidates()
    if daily_candidates:
        print(f"  [candidates] Restored {len(daily_candidates)} candidate(s) from previous session.")
    last_report_date: object = None

    _prewarm_background_data()

    if not is_us_market_open():
        last_report_date = market_today("US")

    while True:
        try:
            loop_start = datetime.now()
            _banner(f"Agent Loop — {loop_start.strftime('%Y-%m-%d %H:%M:%S')}")

            account_us = get_account_info()

            print(f"  {market_status_line('US')}")

            if last_scanned["US"] != market_today("US"):
                daily_candidates[:] = [
                    r for r in daily_candidates if r["Market"] != "US"
                ]
                _save_daily_candidates(daily_candidates)

            needs_scan = (
                is_us_market_open()
                and is_past_open_buffer("US", OPEN_BUFFER_MINS)
                and last_scanned["US"] != market_today("US")
            )

            _us_opts = (account_us is not None
                        and account_us.get("supports_options", False))
            needs_rescan = (
                is_us_market_open()
                and is_past_open_buffer("US", OPEN_BUFFER_MINS)
                and last_scanned["US"] == market_today("US")
                and bool(daily_candidates)
                and any(r["Market"] == "US" and not has_open_trade(r["Code"], trade_type="options")
                        for r in daily_candidates)
            )

            if needs_scan:
                # Mark first so a crash mid-scan doesn't re-trigger on restart.
                last_scanned["US"] = market_today("US")
                _save_scan_state(last_scanned)

                _banner("Scanning US Market")
                new_candidates = phase_scan_and_execute(account_us)

                if new_candidates:
                    print(f"  Scan marked for US ({last_scanned['US']})")
                    existing_codes = {r["Code"] for r in daily_candidates}
                    daily_candidates.extend(
                        r for r in new_candidates if r["Code"] not in existing_codes
                    )
                    _save_daily_candidates(daily_candidates)
                else:
                    # Scan produced no candidates (e.g. account not yet funded,
                    # or no qualifying stocks today). Clear the marker so the
                    # scan retries on the next loop instead of being skipped all day.
                    last_scanned["US"] = None
                    _save_scan_state(last_scanned)
                    print("  Scan returned no candidates — will retry next loop.")

            elif needs_rescan:
                _banner("Re-scan — Refreshing top candidates")
                rescan_pool = [r for r in daily_candidates if r["Market"] == "US"]
                _rescan_candidates(rescan_pool, account_us)

            else:
                if not is_us_market_open():
                    reason = "US closed"
                elif not is_past_open_buffer("US", OPEN_BUFFER_MINS):
                    trigger = next_scan_trigger("US", OPEN_BUFFER_MINS)
                    reason = f"US in open buffer — scan at {trigger.strftime('%H:%M %Z')}"
                elif not daily_candidates:
                    reason = "scan ran but no qualifying candidates — retrying next loop"
                    last_scanned["US"] = None
                    _save_scan_state(last_scanned)
                else:
                    reason = "all top-N candidates have open trades"
                print(f"  No scan needed — {reason}")

            # ── Russell retry (once per loop if no active trade and market open) ─
            # Skip when needs_rescan: _rescan_candidates already ran Russell.
            if (TRADE_RUSSELL_ENABLED and _us_opts and is_us_market_open()
                    and is_past_open_buffer("US", OPEN_BUFFER_MINS)
                    and last_scanned["US"] == market_today("US")
                    and not needs_rescan
                    and not has_open_russell_trade() and account_us):
                russell_plan = run_russell_scan(account_us)
                if russell_plan:
                    _execute_plan(russell_plan)

            # ── HK Market scan (once per HK trading day) ──────────────────────
            if TRADE_HK_ENABLED:
                print(f"  {market_status_line('HK')}")
                needs_hk_scan = (
                    is_hk_market_open()
                    and is_past_open_buffer("HK", OPEN_BUFFER_MINS)
                    and last_scanned.get("HK") != market_today("HK")
                )
                if needs_hk_scan:
                    last_scanned["HK"] = market_today("HK")
                    _save_scan_state(last_scanned)
                    account_hk = get_account_info(market="HK")
                    if account_hk:
                        _banner("Scanning HK Market")
                        hk_plan = scan_hk(account_hk)
                        if hk_plan:
                            _print_plan_summary(hk_plan)
                            _execute_plan(hk_plan)
                        else:
                            print("  No HK trades to execute.")

            # ── Gem scan (every cycle, independent of main scan state) ────────
            if TRADE_GEM_ENABLED:
                if is_us_market_open() and account_us:
                    from agent.gem_scanner import run_gem_scan
                    gem_plan = run_gem_scan(account_us)
                    if gem_plan:
                        _execute_plan(gem_plan)
                if TRADE_HK_ENABLED and is_hk_market_open():
                    from agent.gem_scanner import run_hk_gem_scan
                    account_hk = get_account_info(market="HK")
                    if account_hk:
                        hk_gem_plan = run_hk_gem_scan(account_hk)
                        if hk_gem_plan:
                            _execute_plan(hk_gem_plan)

            from datetime import timezone as _tz

            market_closed = not is_us_market_open()

            if not market_closed:
                phase_monitor_once(account_us)
            else:
                print("  US market closed — skipping monitor.")
                today_local = market_today("US")
                if last_report_date != today_local:
                    last_report_date = today_local
                    _acct_hk_report = get_account_info(market="HK") if TRADE_HK_ENABLED else None
                    generate_daily_report(account_us, _acct_hk_report, today_local)
                    generate_category_report(account_us, _acct_hk_report,
                                             report_date=today_local)
                    _prewarm_background_data()

            now_utc      = datetime.now(_tz.utc)
            elapsed_secs = (datetime.now() - loop_start).total_seconds()

            if market_closed:
                sleep_secs = 7 * 24 * 3600
                wake_label = None
                trigger   = next_scan_trigger("US", OPEN_BUFFER_MINS)
                secs_away = (trigger.astimezone(_tz.utc) - now_utc).total_seconds()
                if secs_away > 0:
                    sleep_secs = secs_away
                    wake_label = trigger.strftime("%a %Y-%m-%d %H:%M %Z")
            else:
                sleep_secs = max(60, AGENT_LOOP_INTERVAL_MINS * 60 - elapsed_secs)
                wake_label = None
                if last_scanned["US"] != market_today("US"):
                    trigger   = next_scan_trigger("US", OPEN_BUFFER_MINS)
                    secs_away = (trigger.astimezone(_tz.utc) - now_utc).total_seconds()
                    if 0 < secs_away < sleep_secs:
                        sleep_secs = secs_away
                        wake_label = trigger.strftime("%a %H:%M %Z")

                # Wake immediately if the buffer threshold was crossed mid-loop
                if (last_scanned["US"] != market_today("US")
                        and is_us_market_open()
                        and is_past_open_buffer("US", OPEN_BUFFER_MINS)):
                    sleep_secs = 10
                    wake_label = "immediate (US scan ready)"

                # Also consider HK scan trigger
                if TRADE_HK_ENABLED and last_scanned.get("HK") != market_today("HK"):
                    hk_trigger  = next_scan_trigger("HK", OPEN_BUFFER_MINS)
                    hk_secs     = (hk_trigger.astimezone(_tz.utc) - now_utc).total_seconds()
                    if 0 < hk_secs < sleep_secs:
                        sleep_secs = hk_secs
                        wake_label = hk_trigger.strftime("%a %H:%M %Z (HK)")

            sleep_secs = max(10, int(sleep_secs))
            wake_str   = wake_label if wake_label else (
                datetime.now().replace(microsecond=0) + timedelta(seconds=sleep_secs)
            ).strftime("%H:%M:%S")
            _prewarm_background_data()
            print(f"\n  Sleeping {sleep_secs // 60}m {sleep_secs % 60}s  "
                  f"(next wake: {wake_str})")
            time.sleep(sleep_secs)

        except KeyboardInterrupt:
            print("\n\n  Agent stopped by user.")
            break
        except Exception as e:
            print(f"\n  [ERROR] Unhandled exception: {e}")
            import traceback; traceback.print_exc()
            print("  Continuing in 60 seconds...")
            time.sleep(60)


# ── Status ────────────────────────────────────────────────────────────────────

def phase_category_report() -> None:
    """Generate category P&L report on demand."""
    _banner("Category P&L Report")
    account_us = get_account_info()
    path = generate_category_report(account_us, None)
    if path:
        print(f"  Saved: {path}")


def phase_status() -> None:
    _banner("Account Status")
    account_us = get_account_info()
    if account_us:
        print_account_summary(account_us)
    _banner("Open Trades")
    print_open_trades()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from agent.session_logger import setup_session_log
    from agent.ibkr_client import ensure_connected, disconnect_ib

    _log = setup_session_log()
    try:
        print("Connecting to TWS/IB Gateway...")
        if not ensure_connected():
            print("ERROR: Cannot connect to TWS. "
                  "Ensure TWS is running with API enabled on port 7497 (paper) or 7496 (live).")
            sys.exit(1)
        print("TWS connection established.\n")

        mode = sys.argv[1].lower() if len(sys.argv) > 1 else "run"

        if mode == "run":
            phase_autonomous_loop()
        elif mode == "scan":
            phase_scan_and_execute()
        elif mode == "monitor":
            phase_monitor_loop()
        elif mode == "monitor_once":
            phase_monitor_once()
        elif mode == "status":
            phase_status()
        elif mode == "category_report":
            phase_category_report()
        else:
            print(f"Unknown mode '{mode}'. Use: run | scan | monitor | monitor_once | status | category_report")
    finally:
        disconnect_ib()
        if _log:
            _log.close()


if __name__ == "__main__":
    main()
