"""
Account info, margin checks, and allocation helpers — IBKR version.

Connects to TWS via ib_insync. All account data is fetched via ib.accountValues().
US-only for options; IBKR also supports HK stocks but that is handled separately.
"""

from __future__ import annotations

from agent.constants import (
    US_ALLOCATION_PCT,
    TOP_N_STOCKS,
    SCORE_WEIGHT_STEEPNESS,
    MIN_MARGIN_BUFFER_PCT,
)


def get_account_info(market=None) -> dict | None:
    """Return account info dict for the IBKR trading account.

    market="HK" → returns HKD balance values; currency="HKD", supports_options=False.
    market=None / "US" → returns USD values; currency="USD", supports_options=True.

    Both share a single IBKR account; ib.accountValues() rows are per-currency.
    """
    from agent.ibkr_client import get_ib, ibkr_lock

    try:
        with ibkr_lock:
            ib = get_ib()
            ib.sleep(0)   # flush any pending account-value events from TWS
            vals = ib.accountValues()
    except Exception as exc:
        print(f"  [risk] get_account_info failed: {exc}")
        return None

    ccy = "HKD" if market == "HK" else "USD"

    def _tag(tag: str) -> float:
        for v in vals:
            if v.tag == tag and v.currency == ccy:
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    pass
        return 0.0

    cash    = _tag("CashBalance")
    nlv     = _tag("NetLiquidation")
    bp      = _tag("BuyingPower")
    opt_bp  = _tag("OptionBuyingPower")
    avail   = _tag("AvailableFunds")
    maint_m = _tag("MaintMarginReq")
    gross   = _tag("GrossPositionValue")
    unreal  = _tag("UnrealizedPnL")
    real    = _tag("RealizedPnL")

    # If HKD account shows zero cash, estimate from USD balance using the exchange rate
    if market == "HK" and cash == 0:
        usd_cash = next(
            (float(v.value) for v in vals
             if v.tag == "CashBalance" and v.currency == "USD"),
            0.0,
        )
        if usd_cash > 0:
            from agent.constants import HKD_USD_RATE
            cash = usd_cash * HKD_USD_RATE

    # OptionBuyingPower is more accurate for options margin; fall back to BuyingPower
    buying_power = opt_bp if (opt_bp > 0 and market != "HK") else bp

    acc_id = next((v.account for v in vals if v.account), "IBKR")

    return {
        "acc_id":             acc_id,
        "cash":               cash,
        "total_assets":       nlv,
        "market_val":         gross,
        "buying_power":       buying_power,
        "avail_margin":       avail,
        "maintenance_margin": maint_m,
        "frozen_funds":       0.0,
        "unrealised_pl":      unreal,
        "realised_pl":        real,
        "currency":           ccy,
        "supports_options":   False if market == "HK" else True,
    }


def margin_safe_to_trade(account: dict, order_cost: float) -> tuple[bool, str]:
    """Return (safe, reason). True if account has enough buying power for order_cost.

    For multi-leg options spreads placed atomically via Alpaca, order_cost should be
    the spread's max-loss (spread_width x multiplier x contracts) because Alpaca
    charges spread margin correctly — not the naked short strike.
    """
    if order_cost <= 0:
        return True, ""

    bp = account.get("buying_power", 0)
    if bp > 0:
        if bp >= order_cost:
            return True, ""
        return False, (
            f"Margin safety check FAILED: buying_power={bp:.2f}, "
            f"order_cost={order_cost:.2f} (exceeds available buying power)"
        )

    avail = (account.get("total_assets", 0)
             or account.get("avail_margin", 0)
             or account.get("cash", 0))
    maint = account.get("maintenance_margin", 0)
    required_min = maint * (1 + MIN_MARGIN_BUFFER_PCT) if maint > 0 else 0.0
    headroom = avail - order_cost

    if headroom >= required_min:
        return True, ""
    return False, (
        f"Margin safety check FAILED: total_assets={avail:.2f}, "
        f"order_cost={order_cost:.2f}, required_min={required_min:.2f}"
    )


def compute_allocation(
    results: list[dict],
    account: dict,
    allocation_pct: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """Compute score-weighted per-trade budget, capped at allocation_pct x cash."""
    if not results:
        return results

    pct   = allocation_pct if allocation_pct is not None else US_ALLOCATION_PCT
    n     = top_n or TOP_N_STOCKS
    cash  = account.get("cash", 0)
    total = cash * pct
    ccy   = account.get("currency", "USD")

    top = results[:n]
    num = len(top)
    if num == 1:
        weights = [1.0]
    else:
        weights = [
            1.0 + (SCORE_WEIGHT_STEEPNESS - 1.0) * (1.0 - i / (num - 1))
            for i in range(num)
        ]
    total_w = sum(weights)

    for item, w in zip(top, weights):
        per_trade = round(total * w / total_w, 2)
        item["alloc"] = {
            "per_trade":    per_trade,
            "total_budget": total,
            "currency":     ccy,
            "weight_pct":   round(w / total_w * 100, 1),
        }

    return top


def compute_weighted_allocations(results, account, allocation_pct=None, top_n=None):
    """Alias kept for API compatibility with trade_main.py callers."""
    return compute_allocation(results, account, allocation_pct, top_n)


def print_account_summary(account: dict, label: str = "US") -> None:
    if not account:
        return
    ccy  = account.get("currency", "USD")
    cash = account.get("cash", 0)
    bp   = account.get("buying_power", 0)
    mv   = account.get("market_val", 0)
    mm   = account.get("maintenance_margin", 0)
    ta   = account.get("total_assets", 0)

    budget_pct = US_ALLOCATION_PCT
    budget     = cash * budget_pct
    n          = TOP_N_STOCKS
    steepness  = SCORE_WEIGHT_STEEPNESS
    per_lo     = budget / (1 + (steepness - 1) * (n - 1) / n) / n if n > 1 else budget
    per_hi     = budget * steepness / (1 + steepness) if n > 1 else budget

    print(f"\n  Account [{label}] acc_id={account.get('acc_id', '?')}  "
          f"options={'YES' if account.get('supports_options') else 'NO'}")
    print(f"    Cash            : {ccy}  {cash:>14,.2f}")
    print(f"    Market value    : {ccy}  {mv:>14,.2f}")
    print(f"    Buying power    : {ccy}  {bp:>14,.2f}")
    print(f"    Maint. margin   : {ccy}  {mm:>14,.2f}")
    print(f"    Portfolio value : {ccy}  {ta:>14,.2f}")
    print(f"    Trade budget    : {ccy}  {budget:>14,.2f}  ({budget_pct*100:.0f}% of cash)")
    print(f"    Per-trade budget: {ccy}  {per_lo:,.2f} – {per_hi:,.2f}  "
          f"(score-weighted, {steepness:.0f}x steepness)")
