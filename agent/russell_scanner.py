"""
Russell 2000 opportunistic scanner.

Two-stage scan:
  Stage 1 (fast, ~30 s): pre-filter ~600 S&P 600 small-cap names down
    to ~25 by price, volume, and options availability using Alpaca bar data.
  Stage 2 (full, ~15 s): fetch klines for the 25, score each, walk
    highest-scoring candidates until one is tradeable.

Rules:
  - Only spreads — Bull Put Spread, Bear Put Spread, Bear Call Spread,
    Iron Condor.  CSP and Covered Call are excluded because these are
    not pre-approved stocks for ownership.
  - Exactly one active Russell trade at a time (enforced via scan_source
    tag on the trade record).
  - Allocation: RUSSELL_ALLOCATION_PCT (5%) of total cash balance,
    flat (not score-weighted).

Returns a 0-or-1 item plan list ready for _execute_plan().
"""

import time
from agent.russell_universe import get_russell2000_universe, prefilter_russell
from agent.data_fetcher import fetch_batch, get_stock_names
from agent.scorer import rank_universe
from agent.options_analyzer import get_options_data
from agent.order_executor import select_spread_legs, compute_contracts
from agent.position_manager import has_open_trade, has_open_russell_trade
from agent.constants import RUSSELL_ALLOCATION_PCT, RUSSELL_MIN_SCORE


def _strategy_for_russell(direction_score: float,
                           avg_iv: float | None,
                           hv_20d: float | None) -> str:
    """Map bias + volatility to a spread strategy (no CSP/CC)."""
    iv_pct   = avg_iv if avg_iv is not None else (hv_20d or 0)
    high_vol = iv_pct > 30.0

    if direction_score >= 0.15:
        return "Bull Put Spread" if high_vol else "Bull Call Spread"
    elif direction_score <= -0.15:
        return "Bear Call Spread" if high_vol else "Bear Put Spread"
    else:
        return "Iron Condor"


def run_russell_scan(account_us: dict) -> list[dict]:
    """
    Execute two-stage Russell 2000 scan and return a 0-or-1 item plan.

    Returns [] when:
    - A Russell 2000 trade is already open (1-trade limit).
    - No candidate reaches RUSSELL_MIN_SCORE after scoring.
    - No tradeable options chain can be found for qualifying candidates.
    """
    if has_open_russell_trade():
        print("  [Russell 2000] Active trade already open — scan skipped.")
        return []

    universe = get_russell2000_universe()
    if not universe:
        print("  [Russell 2000] Could not build universe — scan skipped.")
        return []

    # ── Stage 1: Pre-filter ───────────────────────────────────────────────
    filtered = prefilter_russell(universe, target=25)
    if not filtered:
        print("  [Russell 2000] No candidates survived pre-filter.")
        return []

    # ── Stage 2: Score ────────────────────────────────────────────────────
    print(f"  [Russell 2000] Scoring {len(filtered)} pre-filtered candidates...")
    kline_data = fetch_batch(filtered, days=260, delay=0.3)
    scored_df  = rank_universe(kline_data, top_n=len(filtered))

    if scored_df.empty:
        print("  [Russell 2000] Scoring returned no results.")
        return []

    names = get_stock_names(scored_df["code"].tolist())

    for _, row in scored_df.iterrows():
        code  = row["code"]
        score = float(row["composite_score"])
        name  = names.get(code, code)

        if score < RUSSELL_MIN_SCORE:
            print(f"  [Russell 2000] Top remaining candidate {code} score "
                  f"{score:+.3f} < threshold ({RUSSELL_MIN_SCORE:+.2f}) "
                  f"— no qualifying candidate.")
            break

        if has_open_trade(code):
            print(f"  [Russell 2000] {code}: existing open trade — trying next.")
            continue

        print(f"  [Russell 2000] Best candidate: {code} ({name})  "
              f"score={score:+.3f}  direction={row['direction_score']:+.3f}")

        opts          = get_options_data(code)
        chain         = opts.get("chain")
        current_price = opts.get("current_price", float(row.get("current_price", 0)))
        exp_date      = opts.get("exp_date", "N/A")
        avg_iv        = opts.get("avg_iv")

        if chain is None or chain.empty:
            print(f"  [Russell 2000] {code}: no options chain data — trying next.")
            continue

        strategy = _strategy_for_russell(
            float(row["direction_score"]), avg_iv, row.get("hv_20d")
        )

        legs = select_spread_legs(chain, strategy, current_price)
        if legs is None:
            print(f"  [Russell 2000] {code}: could not select {strategy} legs "
                  f"— trying next.")
            continue

        budget        = account_us["cash"] * RUSSELL_ALLOCATION_PCT
        num_contracts = compute_contracts(legs, budget, strategy)

        if num_contracts < 1:
            print(f"  [Russell 2000] {code}: budget USD {budget:,.0f} too small "
                  f"for 1 contract — trying next.")
            continue

        print(f"  [Russell 2000] Plan confirmed: {strategy} on {code}  "
              f"x{num_contracts} contracts  budget=USD {budget:,.0f}")

        return [{
            "trade_type":      "options",
            "stock_code":      code,
            "strategy":        strategy,
            "market":          "US",
            "exp_date":        exp_date,
            "legs":            legs,
            "num_contracts":   num_contracts,
            "account":         account_us,
            "alloc": {
                "per_trade":    round(budget, 2),
                "total_budget": round(budget, 2),
                "currency":     "USD",
                "weight_pct":   100.0,
            },
            "current_price":   current_price,
            "composite_score": score,
            "scan_source":     "russell",
        }]

    print("  [Russell 2000] No qualifying candidate found this cycle.")
    return []
