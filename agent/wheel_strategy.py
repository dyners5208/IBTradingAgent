"""
Wheel Strategy: Cash-Secured Puts and Covered Calls on a pre-approved watchlist.

CSP eligibility (in priority order):
  1. Already has an open trade         → skip
  2. Stock composite score < threshold → skip (bearish/neutral signal)
  3. Cash covers collateral            → Cash-Secured Put (sell OTM put, ~0.30 delta)
  4. Budget too small                  → skip

CSP sizing uses the same score-weighted allocation as the main scan:
  highest-scoring eligible stock gets SCORE_WEIGHT_STEEPNESS× the budget
  of the lowest-scoring one, with the total capped at WHEEL_CSP_TOTAL_BUDGET_PCT
  of available cash.

Covered Calls are placed for any stock where ≥100 shares are held regardless
of score (you already own it; the CC just generates income and does not imply
a new directional bet).
"""

import time

from agent.options_analyzer import get_options_data
from agent.order_executor import select_spread_legs
from agent.position_manager import has_open_trade, get_last_assigned_csp
from agent.risk_manager import margin_safe_to_trade
from agent.constants import (
    MAX_CONTRACTS, WHEEL_CSP_TOTAL_BUDGET_PCT,
    CSP_MIN_COMPOSITE_SCORE, SCORE_WEIGHT_STEEPNESS, WHEEL_UNIVERSE,
)


def _get_us_holdings() -> dict[str, int]:
    """Return {ticker: shares_held} for all long US equity stock positions."""
    from agent.ibkr_client import get_ib, ibkr_lock

    try:
        with ibkr_lock:
            ib        = get_ib()
            positions = ib.positions()
    except Exception as exc:
        print(f"  [wheel] get_positions failed: {exc}")
        return {}

    holdings: dict[str, int] = {}
    for pos in positions:
        con = pos.contract
        if getattr(con, "secType", "") != "STK":
            continue
        qty = int(float(pos.position or 0))
        if qty > 0:
            holdings[con.symbol] = qty
    return holdings


def _score_wheel_universe(tickers: list[str]) -> list[dict]:
    """
    Score WHEEL_UNIVERSE stocks with the same indicators as the main scan.
    Returns eligible stocks (composite_score >= CSP_MIN_COMPOSITE_SCORE) sorted
    by score descending.
    """
    from agent.data_fetcher import fetch_batch
    from agent.scorer import score_stock

    print(f"\n  Scoring {len(tickers)} wheel stocks for CSP eligibility "
          f"(min score={CSP_MIN_COMPOSITE_SCORE:+.2f})...")
    kline_data = fetch_batch(tickers, days=260, delay=0.3)

    eligible = []
    for ticker in tickers:
        if ticker not in kline_data:
            print(f"    {ticker:<8}: no kline data — skip")
            continue
        s = score_stock(kline_data[ticker])
        if s is None:
            print(f"    {ticker:<8}: insufficient history — skip")
            continue
        score = s["composite_score"]
        if score >= CSP_MIN_COMPOSITE_SCORE:
            eligible.append({
                "code":            ticker,
                "composite_score": score,
                "direction_score": s["direction_score"],
            })
            print(f"    {ticker:<8}: score={score:+.3f}  ✓ eligible")
        else:
            print(f"    {ticker:<8}: score={score:+.3f}  below threshold — skip")

    eligible.sort(key=lambda x: x["composite_score"], reverse=True)
    return eligible


def run_wheel_scan(account_us: dict,
                   csp_enabled: bool = True, cc_enabled: bool = True) -> list[dict]:
    """
    Scan WHEEL_UNIVERSE and return a list of trade-plan dicts ready for
    _print_plan_summary() / _execute_plan() in trade_main.py.

    Two passes:
      1. Covered Calls  — any WHEEL_UNIVERSE stock where ≥100 shares are held
         (score not required; CC does not increase directional exposure)
      2. Cash-Secured Puts — eligible stocks only (score ≥ threshold), processed
         in descending score order with score-weighted collateral allocation
    """
    plans: list[dict] = []

    try:
        holdings = _get_us_holdings()
        cash     = account_us.get("cash", 0.0)
        ccy      = account_us.get("currency", "USD")

        # Stocks with ≥100 shares held — computed always (CSP skip logic needs this
        # even when CC is disabled, to avoid stacking CSPs on top of held stock)
        cc_codes = {
            t for t in WHEEL_UNIVERSE
            if holdings.get(t, 0) >= 100 and not has_open_trade(t)
        }

        # ── Score WHEEL_UNIVERSE for CSP eligibility ───────────────────────────
        eligible_csp:    list[dict] = []
        csp_total_budget             = cash * WHEEL_CSP_TOTAL_BUDGET_PCT
        per_stock_budget: dict[str, float] = {}

        if csp_enabled:
            eligible_csp = _score_wheel_universe(WHEEL_UNIVERSE)

            if eligible_csp:
                n = len(eligible_csp)
                weights = [1.0] if n == 1 else [
                    1.0 + (SCORE_WEIGHT_STEEPNESS - 1.0) * (1.0 - i / (n - 1))
                    for i in range(n)
                ]
                total_w = sum(weights)
                per_stock_budget = {
                    s["code"]: round(csp_total_budget * w / total_w, 2)
                    for s, w in zip(eligible_csp, weights)
                }

                print(f"\n  CSP allocation — budget: {ccy} {csp_total_budget:,.0f} "
                      f"({WHEEL_CSP_TOTAL_BUDGET_PCT*100:.0f}% of cash)  "
                      f"steepness: {SCORE_WEIGHT_STEEPNESS:.0f}×")
                for s, w in zip(eligible_csp, weights):
                    pct    = w / total_w * 100
                    budget = per_stock_budget[s["code"]]
                    print(f"    {s['code']:<6}  "
                          f"score={s['composite_score']:+.3f}  "
                          f"weight={pct:.1f}%  budget={ccy} {budget:,.0f}")
            else:
                print("\n  No wheel stocks meet CSP score threshold — CSPs skipped.")
        else:
            print("\n  CSP channel disabled — skipping.")

        cc_count = len(cc_codes) if cc_enabled else 0
        print(f"\n  Wheel universe: {len(WHEEL_UNIVERSE)} stocks  |  "
              f"Cash: {ccy} {cash:,.0f}  |  "
              f"CC candidates: {cc_count}  |  "
              f"CSP eligible: {len(eligible_csp)}")

        # ── Pass 1: Covered Calls ──────────────────────────────────────────────
        if not cc_enabled:
            print("  CC channel disabled — skipping covered calls.")
        for ticker in WHEEL_UNIVERSE:
            if not cc_enabled:
                break
            if ticker not in cc_codes:
                continue
            shares_held = holdings[ticker]
            n_cc        = min(shares_held // 100, MAX_CONTRACTS)

            print(f"  {ticker}: fetching options chain for CC...")
            opts = get_options_data(ticker)
            if not opts or opts.get("chain") is None or opts["chain"].empty:
                print(f"    No options data — skip.")
                time.sleep(0.3)
                continue

            chain         = opts["chain"]
            current_price = opts.get("current_price", 0.0)
            exp_date      = opts.get("exp_date", "")

            if current_price <= 0:
                print(f"    Invalid price — skip.")
                continue

            legs = select_spread_legs(chain, "Covered Call", current_price)
            if legs:
                leg     = legs[0]
                mid     = (leg["bid"] + leg["ask"]) / 2
                premium = round(mid * 100 * n_cc, 2)

                prior_csp    = get_last_assigned_csp(ticker)
                wheel_fields: dict = {}
                if prior_csp and prior_csp.get("wheel_assignment_price"):
                    assignment_px  = float(prior_csp["wheel_assignment_price"])
                    accum_premium  = float(prior_csp.get("wheel_premium_accumulated", 0.0))
                    wheel_fields = {
                        "wheel_assignment_price":    assignment_px,
                        "wheel_premium_accumulated": accum_premium,
                    }
                    net_cost_basis = assignment_px - accum_premium / (n_cc * 100)
                    cc_strike      = leg["strike"]
                    basis_note     = (
                        f"  cost_basis=${net_cost_basis:.2f}/sh"
                        + (" ⚠ STRIKE BELOW BASIS" if cc_strike < net_cost_basis else "")
                    )
                    if cc_strike < net_cost_basis:
                        print(f"    [wheel] WARNING: CC strike ${cc_strike:.2f} is below "
                              f"net cost basis ${net_cost_basis:.2f} "
                              f"(assignment=${assignment_px:.2f}, premiums=${accum_premium:.2f}). "
                              f"Stock needs to recover before wheel cycle can fully close.")
                else:
                    basis_note = ""

                print(f"    CC  → {n_cc}× SELL CALL  K={leg['strike']:.2f}  "
                      f"δ={leg['delta']:.2f}  mid={mid:.2f}  "
                      f"est. premium={ccy} {premium:,.0f}  "
                      f"(covers {shares_held} shares){basis_note}")
                plans.append({
                    "trade_type":      "options",
                    "stock_code":      ticker,
                    "strategy":        "Covered Call",
                    "market":          "US",
                    "exp_date":        exp_date,
                    "legs":            legs,
                    "num_contracts":   n_cc,
                    "account":         account_us,
                    "alloc": {
                        "per_trade":    premium,
                        "total_budget": cash,
                        "currency":     ccy,
                        "weight_pct":   0.0,
                    },
                    "current_price":   current_price,
                    "composite_score": f"CC / {shares_held} shares held",
                    "wheel_type":      "CC",
                    **wheel_fields,
                })
            else:
                print(f"    CC: no suitable CALL leg — skip.")
            time.sleep(0.5)

        # ── Pass 2: Cash-Secured Puts (score order) ────────────────────────────
        if not csp_enabled or not eligible_csp:
            return plans

        csp_committed = 0.0

        for stock_info in eligible_csp:
            ticker = stock_info["code"]
            score  = stock_info["composite_score"]

            # Skip stocks being handled as CC this session
            if ticker in cc_codes:
                print(f"  SKIP {ticker} CSP: ≥100 shares held — CC placed instead.")
                continue

            if has_open_trade(ticker, trade_type="options"):
                print(f"  SKIP {ticker}: open options trade already exists.")
                continue

            print(f"  {ticker} (score={score:+.3f}): fetching options chain for CSP...")
            opts = get_options_data(ticker)
            if not opts or opts.get("chain") is None or opts["chain"].empty:
                print(f"    No options data — skip.")
                time.sleep(0.3)
                continue

            chain         = opts["chain"]
            current_price = opts.get("current_price", 0.0)
            exp_date      = opts.get("exp_date", "")
            avg_iv        = opts.get("avg_iv")

            if current_price <= 0:
                print(f"    Invalid price — skip.")
                continue

            legs = select_spread_legs(chain, "Cash-Secured Put", current_price)
            if not legs:
                print(f"    CSP: no suitable PUT leg — skip.")
                time.sleep(0.3)
                continue

            leg    = legs[0]
            strike = leg["strike"]

            # Per-stock budget: score-weighted share, capped at remaining pool
            csp_remaining   = csp_total_budget - csp_committed
            budget_for_this = min(per_stock_budget[ticker], csp_remaining)

            if budget_for_this <= 0:
                print(f"    CSP: wheel budget exhausted "
                      f"({ccy} {csp_total_budget:,.0f} fully committed) — skip.")
                time.sleep(0.3)
                continue

            n_csp = min(int(budget_for_this / (strike * 100)), MAX_CONTRACTS)
            if n_csp < 1:
                print(f"    CSP: per-stock budget {ccy} {budget_for_this:,.0f} too small "
                      f"(need {ccy} {strike * 100:,.0f}/contract) — skip.")
                time.sleep(0.3)
                continue

            collateral = strike * 100 * n_csp

            # Hard cash ceiling: cumulative collateral must never exceed cash
            if csp_committed + collateral > cash:
                n_csp = int((cash - csp_committed) / (strike * 100))
                if n_csp < 1:
                    print(f"    CSP: insufficient cash after prior commitments — skip.")
                    time.sleep(0.3)
                    continue
                collateral = strike * 100 * n_csp

            # Margin check against uncommitted cash
            ok, msg = margin_safe_to_trade(
                {**account_us, "cash": cash - csp_committed, "avail_margin": 0},
                collateral,
            )
            if not ok:
                print(f"    CSP: margin check FAILED — {msg}")
                time.sleep(0.3)
                continue

            mid        = (leg["bid"] + leg["ask"]) / 2
            premium    = round(mid * 100 * n_csp, 2)
            weight_pct = round(budget_for_this / csp_total_budget * 100, 1)
            iv_str     = f"  IV={avg_iv:.1f}%" if avg_iv else ""
            print(f"    CSP → {n_csp}× SELL PUT   K={strike:.2f}  "
                  f"δ={leg['delta']:.2f}  mid={mid:.2f}  "
                  f"collateral={ccy} {collateral:,.0f}  "
                  f"est. premium={ccy} {premium:,.0f}  "
                  f"score={score:+.3f}  weight={weight_pct:.1f}%{iv_str}")

            csp_committed += collateral
            plans.append({
                "trade_type":      "options",
                "stock_code":      ticker,
                "strategy":        "Cash-Secured Put",
                "market":          "US",
                "exp_date":        exp_date,
                "legs":            legs,
                "num_contracts":   n_csp,
                "account":         account_us,
                "alloc": {
                    "per_trade":    collateral,
                    "total_budget": csp_total_budget,
                    "currency":     ccy,
                    "weight_pct":   round(collateral / csp_total_budget * 100, 1),
                },
                "current_price":   current_price,
                "composite_score": (f"CSP / score={score:+.3f}"
                                    + (f" IV={avg_iv:.1f}%" if avg_iv else "")),
                "wheel_type":      "CSP",
            })
            time.sleep(0.5)

    except Exception as exc:
        print(f"  [wheel] Error during scan: {exc}")
        import traceback; traceback.print_exc()

    return plans
