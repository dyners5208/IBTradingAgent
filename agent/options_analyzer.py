"""
Options chain fetching and strategy selection — IBKR version.

Two-step chain fetch via ib_insync:
  Step 1: ib.reqSecDefOptParams() → available strikes + expirations (fast, no market data)
  Step 2: ib.reqTickers(*option_contracts) → live bid/ask + greeks per contract

OCC symbol format: AAPL251219P00185000
  = AAPL, Dec 19 2025, Put, Strike $185.000
"""

import logging
import re
import time
from datetime import date as _date, timedelta

import pandas as pd

# Suppress expected "no security definition" / "unknown contract" noise that
# ib_insync logs at ERROR level when qualifying option contracts.  These are
# normal for far-OTM strikes on weekly expirations and are filtered AFTER
# qualifyContracts so only truly-qualified contracts are passed to reqTickers.
class _SuppressChainNoise(logging.Filter):
    _PATTERNS = ("No security definition", "Unknown contract")
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self._PATTERNS)

_CHAIN_FILTER = _SuppressChainNoise()
_IB_LOGGERS = [logging.getLogger("ib_insync.wrapper"), logging.getLogger("ib_insync.ib")]

MIN_DTE        = 14
TARGET_DTE_MIN = 21
TARGET_DTE_MAX = 60

# Matches both padded (AAPL  251219C00185000) and compact (AAPL251219C00185000) OCC symbols.
_OCC_RE = re.compile(r'^([A-Z]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$')


def _parse_occ(symbol: str) -> dict:
    """Parse an OCC option symbol into components.

    Handles both the space-padded standard form (21 chars) and the
    compact unpadded form that some API responses use.
    """
    try:
        m = _OCC_RE.match(symbol.strip())
        if not m:
            return {}
        root, yy, mm, dd, cp, s_int = m.groups()
        exp_date = _date(2000 + int(yy), int(mm), int(dd))
        strike   = int(s_int) / 1000.0
        return {"ticker": root, "exp_date": exp_date, "call_or_put": cp, "strike": strike}
    except Exception:
        return {}


def _to_occ_symbol(ticker: str, exp_date: str | _date,
                   call_or_put: str, strike: float) -> str:
    """Build an OCC option symbol from components."""
    if isinstance(exp_date, str):
        exp_date = _date.fromisoformat(exp_date)
    root  = ticker.ljust(6)[:6]
    yy    = exp_date.strftime("%y")
    mm    = exp_date.strftime("%m")
    dd    = exp_date.strftime("%d")
    cp    = call_or_put[0].upper()
    s_int = int(round(strike * 1000))
    return f"{root}{yy}{mm}{dd}{cp}{s_int:08d}"


def get_options_data(ticker: str, ctx=None) -> dict:
    """Fetch nearest-expiry options chain snapshot for a US stock.

    IBKR two-step process:
      1. reqSecDefOptParams() — fast metadata (strikes + expirations), no market data needed
      2. reqTickers(*contracts) — live bid/ask + greeks for filtered subset

    Returns dict with keys: exp_date, chain (DataFrame), avg_iv, atm_strike, current_price.
    Returns empty dict on failure.

    ctx is accepted for API compatibility but ignored.
    """
    from ib_insync import Stock, Option
    from agent.ibkr_client import get_ib, ibkr_lock
    from agent.constants import IBKR_OPT_TICKER_TIMEOUT

    result: dict = {}

    try:
        # ── Step 1: Current price via IBKR ticker ─────────────────────────────
        stock_contract = Stock(ticker, "SMART", "USD")
        current_price  = 0.0

        with ibkr_lock:
            ib = get_ib()
            ib.qualifyContracts(stock_contract)
            stock_tickers = ib.reqTickers(stock_contract)
            ib.sleep(2)

        if stock_tickers:
            t = stock_tickers[0]
            bid  = float(t.bid)  if t.bid  and t.bid  > 0 else 0.0
            ask  = float(t.ask)  if t.ask  and t.ask  > 0 else 0.0
            last = float(t.last) if t.last and t.last > 0 else 0.0
            if bid > 0 and ask > 0:
                current_price = (bid + ask) / 2
            elif last > 0:
                current_price = last
        # reqTickers handles its own cleanup — no cancelMktData needed

        if current_price <= 0:
            # Fallback to yesterday's close from kline cache
            from agent.data_fetcher import fetch_kline
            df = fetch_kline(ticker, days=5)
            if df is not None and not df.empty:
                current_price = float(df["close"].iloc[-1])

        if current_price <= 0:
            print(f"  [options] {ticker}: cannot determine current price")
            return result

        result["current_price"] = current_price

        # ── Step 2: Option chain metadata ─────────────────────────────────────
        today      = _date.today()
        target_lo  = today + timedelta(days=TARGET_DTE_MIN)
        target_hi  = today + timedelta(days=TARGET_DTE_MAX)
        min_exp    = today + timedelta(days=MIN_DTE)
        target_mid = today + timedelta(days=(TARGET_DTE_MIN + TARGET_DTE_MAX) // 2)

        with ibkr_lock:
            ib     = get_ib()
            chains = ib.reqSecDefOptParams(
                ticker, "", "STK", stock_contract.conId
            )

        if not chains:
            print(f"  [options] {ticker}: reqSecDefOptParams returned nothing")
            return result

        # Prefer SMART exchange; fall back to first available
        chain_info = next((c for c in chains if c.exchange == "SMART"), None) or chains[0]

        # Parse expirations to date objects (IBKR format: YYYYMMDD)
        def _parse_exp(e: str) -> _date:
            return _date(int(e[:4]), int(e[4:6]), int(e[6:8]))

        # Filter to target DTE window
        valid_exps = sorted([
            _parse_exp(e) for e in chain_info.expirations
            if target_lo <= _parse_exp(e) <= target_hi
        ])

        if not valid_exps:
            # Fallback: min_exp to target_hi + 30 days
            valid_exps = sorted([
                _parse_exp(e) for e in chain_info.expirations
                if min_exp <= _parse_exp(e) <= target_hi + timedelta(days=30)
            ])

        if not valid_exps:
            print(f"  [options] {ticker}: no expirations in target DTE window")
            return result

        # Pick expiry closest to target midpoint
        best_exp_date = min(valid_exps, key=lambda d: abs((d - target_mid).days))
        best_exp_str  = best_exp_date.strftime("%Y%m%d")

        # Filter strikes to ±40% of current price to limit reqTickers calls
        strike_lo = current_price * 0.60
        strike_hi = current_price * 1.40
        valid_strikes = sorted([
            s for s in chain_info.strikes
            if strike_lo <= s <= strike_hi
        ])

        if not valid_strikes:
            print(f"  [options] {ticker}: no strikes in ±40% range around {current_price:.2f}")
            return result

        # ── Step 3: Build Option contracts and request market data ─────────────
        option_contracts = []
        for right in ("C", "P"):
            for strike in valid_strikes:
                con = Option(
                    ticker, best_exp_str, strike, right, "SMART",
                    tradingClass=chain_info.tradingClass,
                )
                option_contracts.append(con)

        for lg in _IB_LOGGERS:
            lg.addFilter(_CHAIN_FILTER)
        try:
            with ibkr_lock:
                ib = get_ib()
                ib.qualifyContracts(*option_contracts)
        finally:
            for lg in _IB_LOGGERS:
                lg.removeFilter(_CHAIN_FILTER)

        # Only request market data for contracts that were successfully qualified.
        # qualifyContracts leaves conId=0 on (expiry, strike) combos that don't
        # exist on SMART (e.g. far-OTM strikes on weekly expirations).
        qualified = [c for c in option_contracts if getattr(c, "conId", 0)]
        if not qualified:
            print(f"  [options] {ticker}: no option contracts qualified for {best_exp_str}")
            return result

        with ibkr_lock:
            ib = get_ib()
            opt_tickers = ib.reqTickers(*qualified)
            ib.sleep(IBKR_OPT_TICKER_TIMEOUT)
        # reqTickers handles its own cleanup — no cancelMktData needed

        # ── Step 4: Build DataFrame ────────────────────────────────────────────
        rows = []
        for t in opt_tickers:
            con = t.contract
            if not con or not hasattr(con, "right"):
                continue

            bid  = float(t.bid)  if t.bid  and t.bid  > 0 else 0.0
            ask  = float(t.ask)  if t.ask  and t.ask  > 0 else 0.0

            g     = t.modelGreeks
            delta = float(g.delta)      if g and g.delta      is not None else 0.0
            theta = float(g.theta)      if g and g.theta      is not None else 0.0
            iv    = float(g.impliedVol) * 100 if g and g.impliedVol is not None else 0.0

            exp_str  = con.lastTradeDateOrContractMonth  # YYYYMMDD
            exp_date = _date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
            occ_sym  = _to_occ_symbol(ticker, exp_date, con.right, float(con.strike))

            rows.append({
                "code":               occ_sym,
                "strike_price":       float(con.strike),
                "call_or_put":        con.right,         # "C" or "P"
                "exp_date":           exp_date.isoformat(),
                "bid":                bid,
                "ask":                ask,
                "delta":              delta,
                "theta":              theta,
                "implied_volatility": iv,
                "_ibkr_conid":        int(con.conId) if con.conId else 0,
            })

        if not rows:
            print(f"  [options] {ticker}: chain produced 0 rows after ticker fetch")
            return result

        chain = pd.DataFrame(rows)
        chain["strike_price"] = pd.to_numeric(chain["strike_price"], errors="coerce")

        # Filter to best expiry (should already be the case, but just in case)
        chain = chain[chain["exp_date"] == best_exp_date.isoformat()].copy()
        exp_date_str = best_exp_date.isoformat()

        # ── Step 5: ATM strike and average IV ─────────────────────────────────
        chain["_diff"] = (chain["strike_price"] - current_price).abs()
        atm = chain.nsmallest(6, "_diff")
        result["atm_strike"] = float(atm.iloc[0]["strike_price"]) if not atm.empty else None

        iv_vals = pd.to_numeric(atm["implied_volatility"], errors="coerce").replace(0, pd.NA).dropna()
        result["avg_iv"] = float(iv_vals.mean()) if not iv_vals.empty else None

        result["exp_date"] = exp_date_str
        result["chain"]    = chain

    except Exception as exc:
        print(f"  [options_analyzer] Error fetching data for {ticker}: {exc}")

    return result


def select_strategy(direction_score: float, hv_20d: float | None,
                    avg_iv: float | None) -> dict:
    """Map direction bias + volatility regime to an options strategy."""
    from agent.constants import HIGH_IV_THRESHOLD
    iv_pct   = avg_iv if avg_iv is not None else (hv_20d or 0)
    high_vol = iv_pct > HIGH_IV_THRESHOLD
    vol_label = f"High IV ({iv_pct:.1f}%)" if high_vol else f"Low IV ({iv_pct:.1f}%)"

    if direction_score > 0.25:
        bias = "Bullish"
        if high_vol:
            strategy  = "Bull Put Spread"
            rationale = ("Sell OTM put + buy further OTM put. "
                         "Collect elevated premium while defining max loss.")
            legs = ["SELL 1x OTM Put  (~0.30 delta, ~5-8% below current price)",
                    "BUY  1x OTM Put  (~0.15 delta, ~10-15% below — same expiry)"]
        else:
            strategy  = "Bull Call Spread"
            rationale = "Buy ATM call + sell OTM call. Low-cost directional play."
            legs = ["BUY  1x ATM Call (~0.50 delta)",
                    "SELL 1x OTM Call (~0.25 delta, ~5-8% above current price)"]
    elif direction_score < -0.25:
        bias = "Bearish"
        if high_vol:
            strategy  = "Bear Call Spread"
            rationale = "Sell OTM call + buy further OTM call."
            legs = ["SELL 1x OTM Call (~0.30 delta, ~5-8% above current price)",
                    "BUY  1x OTM Call (~0.15 delta, ~10-15% above — same expiry)"]
        else:
            strategy  = "Bear Put Spread"
            rationale = "Buy ATM put + sell OTM put. Defined-risk bearish play."
            legs = ["BUY  1x ATM Put  (~0.50 delta)",
                    "SELL 1x OTM Put  (~0.25 delta, ~5-8% below current price)"]
    else:
        bias = "Neutral"
        if high_vol:
            strategy  = "Iron Condor"
            rationale = "Sell OTM call spread + sell OTM put spread."
            legs = ["SELL 1x OTM Call (~0.25 delta)", "BUY  1x OTM Call (~0.10 delta)",
                    "SELL 1x OTM Put  (~0.25 delta)", "BUY  1x OTM Put  (~0.10 delta)"]
        else:
            strategy  = "Long Straddle"
            rationale = "Buy ATM call + ATM put. Profits from a large move."
            legs = ["BUY 1x ATM Call (~0.50 delta)", "BUY 1x ATM Put  (~0.50 delta)"]

    return {"bias": bias, "vol_regime": vol_label, "strategy": strategy,
            "rationale": rationale, "legs": legs}


def select_stock_strategy(direction_score: float, hv_20d: float | None) -> dict:
    hv = hv_20d or 0
    vol_label = f"High HV ({hv:.1f}%)" if hv > 30 else f"Normal HV ({hv:.1f}%)"

    if direction_score > 0.25:
        return {"bias": "Bullish", "vol_regime": vol_label, "strategy": "Stock Buy",
                "rationale": "Strong money-flow and bullish momentum signal.",
                "legs": ["BUY shares at limit price (mid bid/ask)"]}
    elif direction_score < -0.25:
        return {"bias": "Bearish", "vol_regime": vol_label, "strategy": "Stock Sell Short",
                "rationale": "Weak money-flow and bearish momentum signal.",
                "legs": ["SELL SHORT shares at limit price (mid bid/ask)"]}
    else:
        return {"bias": "Neutral", "vol_regime": vol_label, "strategy": "No Trade",
                "rationale": "Direction signal is ambiguous.",
                "legs": ["Hold — direction unclear"]}
