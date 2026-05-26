"""
Politician copy-trade scanner.

Flow:
  1. Fetch all STOCK Act buy disclosures from the past year (cached daily).
  2. Rank politicians by risk-adjusted win rate -> top POLITICIAN_TOP_N.
  3. Pull their most recent filings (past 30 days).
  4. For each unique ticker, run 6-component technical validation score.
  5. Return up to POLITICIAN_MAX_TRADES plan items (market orders, stock buys).
"""
import logging
import datetime
import time

from agent.capitol_trades_fetcher import (
    fetch_all_trades, rank_politicians, get_recent_trades_for, AMOUNT_MIDPOINTS,
    load_seen_filings, save_seen_filings, make_filing_key,
)
from agent.constants import (
    POLITICIAN_ALLOCATION_PCT, POLITICIAN_MAX_TRADES, POLITICIAN_MAX_ACTIVE,
    POLITICIAN_MIN_SCORE, POLITICIAN_MIN_TRADE_COUNT, POLITICIAN_TOP_N,
    POLITICIAN_MAX_RUN_PCT, POLITICIAN_MAX_DRAWDOWN_PCT,
)
from agent.position_manager import (
    has_open_politician_trade, count_open_politician_trades, is_in_session_cooldown,
)
from agent.data_fetcher import fetch_kline
from agent.indicators import calc_rsi, calc_macd

log = logging.getLogger(__name__)

_SECTOR_ETF: dict[str, str] = {
    "NVDA": "XLK", "AAPL": "XLK", "MSFT": "XLK", "META": "XLK",
    "GOOGL": "XLK", "GOOG": "XLK", "INTC": "XLK", "AMD": "XLK",
    "ORCL": "XLK", "CRM": "XLK", "NOW": "XLK", "ADBE": "XLK",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY",
    "NKE": "XLY", "TGT": "XLY", "SBUX": "XLY",
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    "WFC": "XLF", "C": "XLF", "BLK": "XLF", "AXP": "XLF",
    "JNJ": "XLV", "PFE": "XLV", "UNH": "XLV", "ABBV": "XLV",
    "MRK": "XLV", "LLY": "XLV", "TMO": "XLV", "DHR": "XLV",
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
    "OXY": "XLE", "VLO": "XLE",
    "LMT": "XLI", "RTX": "XLI", "NOC": "XLI", "BA": "XLI",
    "GE": "XLI", "HON": "XLI", "CAT": "XLI", "UPS": "XLI",
    "FCX": "XLB", "NEM": "XLB", "APD": "XLB", "LIN": "XLB",
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "AEP": "XLU",
    "AMT": "XLRE", "PLD": "XLRE", "CCI": "XLRE", "EQIX": "XLRE",
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "WMT": "XLP",
    "COST": "XLP", "PM": "XLP",
    "NFLX": "XLC", "DIS": "XLC", "T": "XLC", "VZ": "XLC",
    "CMCSA": "XLC", "CHTR": "XLC",
}
_DEFAULT_SECTOR_ETF = "SPY"


def _amount_midpoint(range_str: str) -> float:
    for key, mid in AMOUNT_MIDPOINTS.items():
        if key in range_str:
            return float(mid)
    return 8_000.0


def _score_ticker(ticker: str, disclosure_price: float,
                  transaction_price: float,
                  spy_df=None, sector_df=None) -> tuple[float, str]:
    """
    6-component technical validation score for a politician copy trade.
    Returns (score, detail_string).
    """
    df = fetch_kline(ticker, days=260)
    time.sleep(0.3)
    if df is None or len(df) < 50:
        return 0.0, "insufficient kline data"

    close   = df["close"]
    current = float(close.iloc[-1])

    rsi_raw = calc_rsi(close, 14)
    if rsi_raw is not None and rsi_raw > 78:
        return -1.0, f"RSI={rsi_raw:.1f} overbought — hard block"

    if transaction_price > 0:
        run_pct = (current - transaction_price) / transaction_price
        if run_pct > POLITICIAN_MAX_RUN_PCT:
            return -1.0, (f"ran +{run_pct*100:.1f}% from transaction price"
                          f" — hard block (>{POLITICIAN_MAX_RUN_PCT*100:.0f}%)")

    if disclosure_price > 0:
        dd_pct = (current - disclosure_price) / disclosure_price
        if dd_pct < -POLITICIAN_MAX_DRAWDOWN_PCT:
            return -1.0, (f"fell {dd_pct*100:.1f}% since disclosure"
                          f" — thesis broken (>{POLITICIAN_MAX_DRAWDOWN_PCT*100:.0f}%)")

    score = 0.0
    parts = []

    sma50 = float(close.rolling(50).mean().iloc[-1])
    if current > sma50:
        score += 0.25
        parts.append("50SMA+")
    else:
        parts.append("50SMA-")

    macd_result = calc_macd(close)
    if macd_result is not None:
        hist = macd_result[2]
        if hist > 0:
            score += 0.20
            parts.append(f"MACD+{hist:+.3f}")
        else:
            score -= 0.10
            parts.append(f"MACD{hist:+.3f}")

    if rsi_raw is not None:
        if 40 <= rsi_raw <= 65:
            score += 0.20
            parts.append(f"RSI={rsi_raw:.0f}ok")
        elif rsi_raw > 65:
            parts.append(f"RSI={rsi_raw:.0f}")
        else:
            score -= 0.20
            parts.append(f"RSI={rsi_raw:.0f}low")

    sector_etf = _SECTOR_ETF.get(ticker, _DEFAULT_SECTOR_ETF)
    _spy_df    = spy_df    if spy_df    is not None else fetch_kline("SPY",        days=260)
    _sector_df = sector_df if sector_df is not None else fetch_kline(sector_etf,  days=260)
    if (_spy_df is not None and len(_spy_df) >= 20
            and _sector_df is not None and len(_sector_df) >= 20):
        spy_ret    = float(_spy_df["close"].iloc[-1] / _spy_df["close"].iloc[-20] - 1)
        sector_ret = float(_sector_df["close"].iloc[-1] / _sector_df["close"].iloc[-20] - 1)
        sector_rs  = sector_ret - spy_ret
        component  = max(min(sector_rs * 10, 0.15), -0.15)
        score     += component
        parts.append(f"secRS={sector_rs:+.2f}")
    else:
        parts.append("secRS=n/a")

    if disclosure_price > 0:
        dd = (current - disclosure_price) / disclosure_price
        if dd < -0.10:
            score -= 0.10
            parts.append(f"dd={dd:+.1%}low")
        elif dd > 0:
            score += 0.05
            parts.append(f"dd={dd:+.1%}ok")
        else:
            parts.append(f"dd={dd:+.1%}")

    if _spy_df is not None and len(_spy_df) >= 20 and len(df) >= 20:
        spy_ret = float(_spy_df["close"].iloc[-1] / _spy_df["close"].iloc[-20] - 1)
        stk_ret = float(close.iloc[-1] / close.iloc[-20] - 1)
        if stk_ret > spy_ret:
            score += 0.10
            parts.append("RS>SPY")
        else:
            score -= 0.10
            parts.append("RS<SPY")

    detail = f"score={score:+.2f}  " + "  ".join(parts)
    return round(score, 4), detail


def run_politician_scan(account_us: dict) -> list[dict]:
    """
    Fetch Capitol Trades data, rank politicians, validate trade signals,
    and return 0-POLITICIAN_MAX_TRADES stock buy plan items.
    """
    active_count = count_open_politician_trades()
    if active_count >= POLITICIAN_MAX_ACTIVE:
        print(f"  Politician scan: {active_count} active trades >= limit ({POLITICIAN_MAX_ACTIVE}) — scan skipped.")
        return []
    remaining_slots = POLITICIAN_MAX_ACTIVE - active_count

    cash = float(account_us.get("cash", 0.0))
    if cash <= 0:
        print("  Politician scan: no cash available")
        return []

    total_budget = round(cash * POLITICIAN_ALLOCATION_PCT, 2)
    if total_budget < 200:
        print(f"  Politician scan: budget ${total_budget:.0f} too small")
        return []

    # Step 1: Fetch all STOCK Act buy trades (past year, cached daily)
    all_trades = fetch_all_trades(lookback_days=365)
    if not all_trades:
        print("  Politician scan: no Capitol Trades data available")
        return []

    # Step 2: Rank politicians by risk-adjusted win rate (ctx ignored in Alpaca)
    ranked = rank_politicians(
        all_trades, None,
        min_trades=POLITICIAN_MIN_TRADE_COUNT,
        top_n=POLITICIAN_TOP_N,
    )
    if not ranked:
        print(f"  Politician scan: 0 politicians met min {POLITICIAN_MIN_TRADE_COUNT} trades "
              f"(total trades in cache: {len(all_trades)})")
        return []

    print(f"  Politician scan: top {len(ranked)} politicians — "
          + ", ".join(f"{p['politician']} ({p['win_rate']:.0%} wr)" for p in ranked))

    # Step 3: Collect fresh candidates from top politicians (past 30 days)
    candidates: list[dict] = []
    seen_tickers: set[str] = set()
    for pol_rec in ranked:
        politician = pol_rec["politician"]
        recent = get_recent_trades_for(politician, all_trades, days=30)
        for t in recent:
            ticker = t.get("ticker", "").strip().replace("/", ".")
            if not ticker or len(ticker) > 6 or ticker in seen_tickers:
                continue
            if has_open_politician_trade(ticker):
                print(f"    {ticker}: open politician trade exists — skip")
                continue
            if is_in_session_cooldown(ticker):
                print(f"    {ticker}: session cooldown — skip")
                continue
            seen_tickers.add(ticker)
            candidates.append({
                "ticker":       ticker,
                "politician":   politician,
                "pol_score":    pol_rec["score"],
                "filing_date":  t.get("filing_date", ""),
                "trans_date":   t.get("transaction_date", ""),
                "amount_range": t.get("amount_range", ""),
                "midpoint":     _amount_midpoint(t.get("amount_range", "")),
                "_filing_key":  make_filing_key(t),
            })

    if not candidates:
        print("  Politician scan: no fresh candidates from top politicians in the past 30 days")
        return []

    # Delta filter: skip filings already evaluated in a prior scan cycle
    seen_filings = load_seen_filings()
    new_candidates = [c for c in candidates if c["_filing_key"] not in seen_filings]
    stale_count = len(candidates) - len(new_candidates)
    if stale_count:
        print(f"  Politician scan: {stale_count} filing(s) skipped (already evaluated in prior scan)")
    candidates = new_candidates

    if not candidates:
        print("  Politician scan: no NEW filings from top politicians — all already evaluated")
        return []

    print(f"  Politician scan: {len(candidates)} new filing(s) to score")

    # ── Pre-validate tickers via yfinance batch ───────────────────────────────
    # Filters out delisted/renamed tickers before scoring. Batch pre-fetch also
    # populates the kline cache so subsequent fetch_kline() calls hit cache only.
    import contextlib as _cl, io as _io
    from agent.data_fetcher import fetch_batch as _fetch_batch
    _cand_tickers = [c["ticker"] for c in candidates]
    _buf = _io.StringIO()
    with _cl.redirect_stdout(_buf), _cl.redirect_stderr(_buf):
        _pre_data = _fetch_batch(_cand_tickers, days=260)
    valid_tickers = set(_pre_data.keys())
    invalid = [t for t in _cand_tickers if t not in valid_tickers]
    if invalid:
        print(f"  Politician scan: skipping {len(invalid)} invalid/delisted ticker(s): "
              + ", ".join(sorted(invalid)))
    candidates = [c for c in candidates if c["ticker"] in valid_tickers]
    if not candidates:
        print("  Politician scan: no valid candidates remain after ticker pre-validation")
        return []

    # Pre-fetch SPY and unique sector ETFs once
    spy_df_cache = fetch_kline("SPY", days=260)
    time.sleep(0.3)
    sector_etf_cache: dict[str, object] = {}
    for _ticker in {c["ticker"] for c in candidates}:
        _etf = _SECTOR_ETF.get(_ticker, _DEFAULT_SECTOR_ETF)
        if _etf not in sector_etf_cache:
            sector_etf_cache[_etf] = fetch_kline(_etf, days=260)
            time.sleep(0.3)

    # Step 4: Approximate historical prices then run technical score
    qualified: list[dict] = []
    for c in candidates:
        disclosure_price  = 0.0
        transaction_price = 0.0
        try:
            fd = datetime.date.fromisoformat(c["filing_date"])
            df = fetch_kline(c["ticker"], days=260)
            time.sleep(0.3)
            if df is not None and not df.empty:
                df = df.copy()
                df["_date"] = [
                    r.date() if hasattr(r, "date")
                    else datetime.date.fromisoformat(str(r)[:10])
                    for r in df.index
                ]
                filing_rows = df[df["_date"] >= fd]
                if not filing_rows.empty:
                    disclosure_price = float(filing_rows.iloc[0]["close"])
                if c.get("trans_date"):
                    td = datetime.date.fromisoformat(c["trans_date"])
                    trans_rows = df[df["_date"] >= td]
                    if not trans_rows.empty:
                        transaction_price = float(trans_rows.iloc[0]["close"])
        except Exception:
            pass

        _etf_key = _SECTOR_ETF.get(c["ticker"], _DEFAULT_SECTOR_ETF)
        score, detail = _score_ticker(
            c["ticker"], disclosure_price, transaction_price,
            spy_df=spy_df_cache,
            sector_df=sector_etf_cache.get(_etf_key),
        )
        print(f"    {c['ticker']} [{c['politician']}]: {detail}")

        if score < POLITICIAN_MIN_SCORE:
            print(f"    {c['ticker']}: score {score:.2f} < min {POLITICIAN_MIN_SCORE:.2f} — skip")
            continue

        c["tech_score"]        = score
        c["disclosure_price"]  = disclosure_price
        c["transaction_price"] = transaction_price
        qualified.append(c)

    seen_filings.update(c["_filing_key"] for c in candidates)
    save_seen_filings(seen_filings)

    if not qualified:
        print("  Politician scan: no candidates passed technical validation")
        return []

    qualified.sort(key=lambda x: (x["tech_score"], x["pol_score"]), reverse=True)
    selected = qualified[:min(POLITICIAN_MAX_TRADES, remaining_slots)]

    # Step 6: Weight allocation by declared trade midpoints
    total_midpoints = sum(c["midpoint"] for c in selected) or 1.0
    plan_items: list[dict] = []

    for c in selected:
        weight    = c["midpoint"] / total_midpoints
        per_trade = round(total_budget * weight, 2)
        per_trade = min(per_trade, round(total_budget * 0.50, 2))

        try:
            df_recent = fetch_kline(c["ticker"], days=5)
            if df_recent is None or df_recent.empty:
                raise ValueError("no kline data")
            current_price = float(df_recent["close"].iloc[-1])
            if current_price <= 0:
                raise ValueError("zero price")
        except Exception:
            print(f"    {c['ticker']}: price fetch failed — skip")
            continue

        if current_price <= 0:
            print(f"    {c['ticker']}: zero current price — skip")
            continue

        qty = max(1, int(per_trade // current_price))

        plan_items.append({
            "trade_type":     "stock",
            "stock_code":     c["ticker"],
            "strategy":       "Stock Buy",
            "market":         "US",
            "side":           "BUY",
            "qty":            qty,
            "lot_size":       1,
            "current_price":  current_price,
            "account":        account_us,
            "alloc": {
                "per_trade":    per_trade,
                "total_budget": total_budget,
                "currency":     "USD",
                "weight_pct":   round(weight * 100, 2),
            },
            "composite_score": c["tech_score"],
            "scan_source":     "politician",
            "order_type":      "MARKET",
            "_politician":  c["politician"],
            "_pol_score":   c["pol_score"],
            "_filing_date": c["filing_date"],
            "_amount_range": c["amount_range"],
        })

    print(f"  Politician scan: {len(plan_items)} plan item(s) generated")
    return plan_items
