"""
Gem scanner — scores the active gem_universe list and returns plan items.

Called every loop cycle when the US market is open.
The gem universe is persistent; no "daily candidate caching" is needed.

Returns a list of plan dicts ready for _execute_plan(), or [] when:
  - gem_universe is empty
  - GEM_MAX_POSITIONS already open
  - No gem reaches GEM_MIN_SCORE
  - No qualifying gem has budget headroom
"""

import time
from datetime import date

from agent.data_fetcher import fetch_batch
from agent.scorer import score_stock
from agent.position_manager import has_open_trade, _get_conn
from agent.gem_manager import update_gem_score
from agent.order_executor import compute_shares
from agent.constants import (
    GEM_US_ALLOCATION_PCT,
    GEM_MAX_POSITIONS, GEM_MIN_SCORE,
    SCORE_WEIGHT_STEEPNESS,
)


def _count_open_gem_trades() -> int:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='open' AND scan_source='gem'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _load_active_gems() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT stock_code, name, conviction FROM gem_universe "
            "WHERE status='active' ORDER BY conviction DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _weighted_alloc(gems_scored: list[dict], total_budget: float,
                    currency: str) -> list[dict]:
    n = len(gems_scored)
    if n == 0:
        return []
    if n == 1:
        return [{"total_budget": round(total_budget, 2),
                 "per_trade":    round(total_budget, 2),
                 "currency":     currency, "weight_pct": 100.0}]
    weights = [1.0 + (SCORE_WEIGHT_STEEPNESS - 1.0) * (1.0 - i / (n - 1))
               for i in range(n)]
    total_w = sum(weights)
    allocs = []
    for w in weights:
        per = total_budget * w / total_w
        allocs.append({"total_budget": round(total_budget, 2),
                       "per_trade":    round(per, 2),
                       "currency":     currency,
                       "weight_pct":   round(w / total_w * 100, 1)})
    return allocs


def run_gem_scan(acct_us: dict) -> list[dict]:
    """Score active US gems and return plan dicts for qualifying entries."""
    gems = _load_active_gems()
    if not gems:
        return []

    open_count = _count_open_gem_trades()
    if open_count >= GEM_MAX_POSITIONS:
        print(f"  [gem-scan] Max positions reached ({open_count}/{GEM_MAX_POSITIONS}) — skipping.")
        return []

    slots_left = GEM_MAX_POSITIONS - open_count

    # Only US gems (bare tickers, no "HK." prefix)
    us_gems = [g for g in gems if not g["stock_code"].startswith("HK.")]
    if not us_gems:
        return []

    today_s = date.today().isoformat()
    qualifying: list[dict] = []

    codes = [g["stock_code"] for g in us_gems]
    print(f"  [gem-scan] {len(codes)} active US gem(s): {codes}")

    klines = fetch_batch(codes, days=260, delay=0.3)

    for g in us_gems:
        ticker = g["stock_code"]

        if ticker not in klines:
            print(f"    {ticker}: no kline data — skip")
            continue

        result = score_stock(klines[ticker])
        if result is None:
            print(f"    {ticker}: insufficient data — skip")
            continue

        score = result.get("Composite Score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0

        update_gem_score(ticker, score, today_s)

        if has_open_trade(ticker, trade_type="stock"):
            print(f"    {ticker}: score {score:+.3f}  — already open")
            continue

        if abs(score) < GEM_MIN_SCORE:
            print(f"    {ticker}: score {score:+.3f}  below GEM_MIN_SCORE ({GEM_MIN_SCORE})")
            continue

        print(f"    {ticker}: score {score:+.3f}  QUALIFIES  (conv={g['conviction']}/5)")
        qualifying.append({
            "gem":    g,
            "score":  score,
            "result": result,
        })

    time.sleep(0.2)

    if not qualifying:
        return []

    qualifying.sort(key=lambda x: abs(x["score"]), reverse=True)
    qualifying = qualifying[:slots_left]

    cash         = acct_us.get("cash", 0)
    ccy          = acct_us.get("currency", "USD")
    total_budget = cash * GEM_US_ALLOCATION_PCT
    allocs       = _weighted_alloc(qualifying, total_budget, ccy)

    plan: list[dict] = []

    for q, alloc in zip(qualifying, allocs):
        ticker = q["gem"]["stock_code"]

        try:
            df_recent = fetch_batch([ticker], days=5, delay=0).get(ticker)
            if df_recent is None or df_recent.empty:
                raise ValueError("no kline data")
            price = float(df_recent["close"].iloc[-1])
            if price <= 0:
                raise ValueError("zero price")
        except Exception:
            print(f"    {ticker}: price fetch failed — skip")
            continue

        if price <= 0:
            print(f"    {ticker}: invalid price — skip")
            continue

        qty = compute_shares(price, alloc["per_trade"], lot_size=1)
        if qty < 1:
            print(f"    {ticker}: budget too small for 1 share — skip")
            continue

        bias = "Bullish" if q["score"] > 0 else "Bearish"
        side = "BUY" if bias == "Bullish" else "SELL"

        plan.append({
            "trade_type":      "stock",
            "stock_code":      ticker,
            "strategy":        f"Stock {side.capitalize()}",
            "market":          "US",
            "side":            side,
            "qty":             qty,
            "lot_size":        1,
            "account":         acct_us,
            "alloc":           alloc,
            "current_price":   price,
            "composite_score": q["score"],
            "scan_source":     "gem",
        })
        print(f"    {ticker}: {side} {qty} shares @ ~{price:.2f}"
              f"  budget {alloc['per_trade']:.0f} {ccy}")

    return plan


def run_hk_gem_scan(acct_hk: dict) -> list[dict]:
    """Score active HK gems and return plan dicts for qualifying entries."""
    from agent.constants import GEM_HK_ALLOCATION_PCT

    gems    = _load_active_gems()
    hk_gems = [g for g in gems if g["stock_code"].startswith("HK.")]
    if not hk_gems:
        return []

    open_count = _count_open_gem_trades()
    if open_count >= GEM_MAX_POSITIONS:
        print(f"  [hk-gem-scan] Max positions reached ({open_count}/{GEM_MAX_POSITIONS}) — skipping.")
        return []
    slots_left = GEM_MAX_POSITIONS - open_count

    codes   = [g["stock_code"] for g in hk_gems]
    today_s = date.today().isoformat()
    print(f"  [hk-gem-scan] {len(codes)} active HK gem(s): {codes}")

    klines = fetch_batch(codes, days=260, delay=0.35)

    qualifying: list[dict] = []
    for g in hk_gems:
        ticker = g["stock_code"]
        if ticker not in klines:
            print(f"    {ticker}: no kline data — skip")
            continue
        result = score_stock(klines[ticker])
        if result is None:
            print(f"    {ticker}: insufficient data — skip")
            continue
        score = float(result.get("Composite Score", result.get("composite_score", 0.0)))
        update_gem_score(ticker, score, today_s)

        from agent.position_manager import has_open_trade
        if has_open_trade(ticker, trade_type="stock"):
            print(f"    {ticker}: score {score:+.3f}  — already open")
            continue
        if abs(score) < GEM_MIN_SCORE:
            print(f"    {ticker}: score {score:+.3f}  below GEM_MIN_SCORE ({GEM_MIN_SCORE})")
            continue

        print(f"    {ticker}: score {score:+.3f}  QUALIFIES  (conv={g['conviction']}/5)")
        qualifying.append({"gem": g, "score": score, "result": result})

    time.sleep(0.2)
    if not qualifying:
        return []

    qualifying.sort(key=lambda x: abs(x["score"]), reverse=True)
    qualifying = qualifying[:slots_left]

    cash         = float(acct_hk.get("cash", 0))
    total_budget = cash * GEM_HK_ALLOCATION_PCT
    allocs       = _weighted_alloc(qualifying, total_budget, "HKD")

    plan: list[dict] = []
    for q, alloc in zip(qualifying, allocs):
        ticker = q["gem"]["stock_code"]
        try:
            df_recent = fetch_batch([ticker], days=5, delay=0).get(ticker)
            if df_recent is None or df_recent.empty:
                raise ValueError("no kline data")
            price = float(df_recent["close"].iloc[-1])
            if price <= 0:
                raise ValueError("zero price")
        except Exception:
            print(f"    {ticker}: price fetch failed — skip")
            continue

        qty = compute_shares(price, alloc["per_trade"], lot_size=100)
        if qty < 1:
            print(f"    {ticker}: budget too small for 1 board lot — skip")
            continue

        side = "BUY" if q["score"] > 0 else "SELL"
        plan.append({
            "trade_type":      "stock",
            "stock_code":      ticker,
            "strategy":        f"Stock {side.capitalize()}",
            "market":          "HK",
            "side":            side,
            "qty":             qty,
            "lot_size":        100,
            "account":         acct_hk,
            "alloc":           alloc,
            "current_price":   price,
            "composite_score": q["score"],
            "scan_source":     "gem",
        })
        print(f"    {ticker}: {side} {qty} shares @ ~{price:.2f}"
              f"  budget {alloc['per_trade']:.0f} HKD")

    return plan
