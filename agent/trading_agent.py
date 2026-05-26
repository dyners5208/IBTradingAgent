import time
import pandas as pd

from agent.universe import get_us_universe
from agent.data_fetcher import fetch_batch, get_stock_names
from agent.scorer import rank_universe
from agent.options_analyzer import get_options_data, select_strategy, select_stock_strategy
from agent.market_hours import is_us_market_open, market_status_line


def _banner(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def scan_and_rank(universe: list[str], market: str,
                  top_n: int = 5) -> pd.DataFrame:
    """Fetch data, score all stocks, return top_n by composite score."""
    print(f"\nFetching historical data for {len(universe)} {market} stocks...")
    kline_data = fetch_batch(universe, days=260, delay=0.3)
    print(f"  Retrieved data for {len(kline_data)} stocks.")

    print(f"Scoring and ranking by money-flow + momentum...")
    top_df = rank_universe(kline_data, top_n=top_n)

    if top_df.empty:
        print(f"  No qualifying stocks found for {market}.")
        return pd.DataFrame()

    print(f"  Top {len(top_df)} candidates by score.")
    return top_df


def _build_result(row, code: str, name: str, market: str,
                  strategy_info: dict, avg_iv=None, exp_date: str = "N/A") -> dict:
    return {
        "Market":           market,
        "Code":             code,
        "Name":             name,
        "Price":            row["current_price"],
        "MFI":              row["mfi"],
        "CMF":              row["cmf"],
        "OBV Slope":        row["obv_slope"],
        "VWAP %":           row["vwap_pct"],
        "RSI":              row["rsi"],
        "MACD Hist":        row["macd_hist"],
        "HV 20d %":         row["hv_20d"],
        "BB Position":      row["bb_position"],
        "Money Flow Score": row["money_flow_score"],
        "Direction Score":  row["direction_score"],
        "Composite Score":  row["composite_score"],
        "Avg IV %":         round(avg_iv, 2) if avg_iv else "N/A",
        "Exp Date":         exp_date,
        "Bias":             strategy_info["bias"],
        "Vol Regime":       strategy_info["vol_regime"],
        "Strategy":         strategy_info["strategy"],
        "Rationale":        strategy_info["rationale"],
        "Legs":             "\n".join(strategy_info["legs"]),
    }


def enrich_with_options(top_df: pd.DataFrame, market: str,
                        names: dict[str, str]) -> list[dict]:
    results = []
    for _, row in top_df.iterrows():
        code = row["code"]
        name = names.get(code, code)
        print(f"  Analysing options chain for {code} ({name})...")
        opts = get_options_data(code)
        strategy_info = select_strategy(
            direction_score=float(row["direction_score"]),
            hv_20d=row.get("hv_20d"),
            avg_iv=opts.get("avg_iv"),
        )
        results.append(_build_result(
            row, code, name, market, strategy_info,
            avg_iv=opts.get("avg_iv"), exp_date=opts.get("exp_date", "N/A"),
        ))
        time.sleep(0.5)
    return results


def enrich_stock_only(top_df: pd.DataFrame, market: str,
                      names: dict[str, str]) -> list[dict]:
    """Build results using stock buy/sell strategy — no options API calls."""
    results = []
    for _, row in top_df.iterrows():
        code = row["code"]
        name = names.get(code, code)
        print(f"  Scoring {code} ({name})...")
        strategy_info = select_stock_strategy(
            direction_score=float(row["direction_score"]),
            hv_20d=row.get("hv_20d"),
        )
        results.append(_build_result(row, code, name, market, strategy_info))
    return results


def scan_hk(acct_hk: dict, top_n: int = 5) -> list[dict]:
    """Score HK universe and return plan dicts for top_n stocks. Stocks only."""
    from agent.universe import get_hk_universe
    from agent.data_fetcher import fetch_batch, get_stock_names, get_stock_lot_sizes
    from agent.scorer import rank_universe
    from agent.position_manager import has_open_trade
    from agent.order_executor import compute_shares
    from agent.constants import (
        HK_ALLOCATION_PCT, STOCK_MIN_ENTRY_SCORE, TOP_N_STOCKS, SCORE_WEIGHT_STEEPNESS,
    )

    _banner("HK Market — Building Universe")
    universe = get_hk_universe()

    _banner(f"HK Market — Fetching {len(universe)} stocks")
    kline_data = fetch_batch(universe, days=260, delay=0.35)
    print(f"  Retrieved data for {len(kline_data)} HK stocks.")

    top_df = rank_universe(kline_data, top_n=top_n or TOP_N_STOCKS)
    if top_df.empty:
        print("  No qualifying HK stocks.")
        return []

    top_codes = top_df["code"].tolist()
    names     = get_stock_names(top_codes)
    lot_sizes = get_stock_lot_sizes(top_codes)

    cash         = float(acct_hk.get("cash", 0))
    total_budget = cash * HK_ALLOCATION_PCT
    n            = len(top_df)
    weights      = [
        1.0 + (SCORE_WEIGHT_STEEPNESS - 1.0) * (1.0 - i / max(n - 1, 1))
        for i in range(n)
    ]
    total_w = sum(weights)

    plan: list[dict] = []
    for i, (_, row) in enumerate(top_df.iterrows()):
        code  = row["code"]
        score = float(row.get("composite_score", 0))

        if abs(score) < STOCK_MIN_ENTRY_SCORE:
            print(f"  SKIP {code}: score {score:+.3f} < STOCK_MIN_ENTRY_SCORE")
            continue
        if has_open_trade(code, trade_type="stock"):
            print(f"  SKIP {code}: existing open trade")
            continue

        per_trade = round(total_budget * weights[i] / total_w, 2)
        side      = "BUY" if score > 0 else "SELL"
        lot_size  = lot_sizes.get(code, 100)
        price     = float(row.get("current_price", 0))
        if price <= 0:
            print(f"  SKIP {code}: invalid price")
            continue

        qty = compute_shares(price, per_trade, lot_size=lot_size)
        if qty < 1:
            print(f"  SKIP {code}: budget too small for 1 board lot")
            continue

        print(f"  {code} ({names.get(code, code)}): score={score:+.3f}  "
              f"{side} {qty}sh  lot={lot_size}  price={price:.3f} HKD")
        plan.append({
            "trade_type":      "stock",
            "stock_code":      code,
            "strategy":        "Stock Buy" if side == "BUY" else "Stock Sell Short",
            "market":          "HK",
            "side":            side,
            "qty":             qty,
            "lot_size":        lot_size,
            "account":         acct_hk,
            "alloc":           {
                "per_trade":    per_trade,
                "total_budget": round(total_budget, 2),
                "currency":     "HKD",
                "weight_pct":   round(weights[i] / total_w * 100, 1),
            },
            "current_price":   price,
            "composite_score": score,
        })
    return plan


def run_agent(top_n: int = 5) -> list[dict]:
    _banner("IBTradingAgent — Money Flow + Options Strategy Scan")

    us_open = is_us_market_open()
    print(f"  {market_status_line('US')}")

    if not us_open:
        print("\n  US market is closed. Scan skipped.")
        return []

    all_results = []

    _banner("US Market — Building Universe")
    us_universe = get_us_universe()

    _banner("US Market — Scanning")
    us_top = scan_and_rank(us_universe, "US", top_n=top_n)

    if not us_top.empty:
        us_names = get_stock_names(us_top["code"].tolist())
        _banner("US Market — Options Analysis")
        all_results.extend(enrich_with_options(us_top, "US", us_names))

    _banner(f"Scan Complete — {len(all_results)} stocks analysed")
    return all_results
