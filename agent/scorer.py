import pandas as pd
from agent.indicators import (
    calc_mfi, calc_cmf, calc_obv_slope, calc_vwap_pct,
    calc_rsi, calc_macd, calc_hv, calc_bollinger,
)


def _clamp(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def score_stock(df: pd.DataFrame) -> dict | None:
    """Compute composite money-flow + momentum score for a single stock.

    Returns None if data is insufficient.
    Composite score in [-1, +1]: positive = institutional accumulation + bullish momentum.
    """
    if df is None or len(df) < 30:
        return None

    close = df["close"]

    # ── Money Flow (60% weight) ────────────────────────────────────────────────
    mfi_val   = calc_mfi(df, 14)
    cmf_val   = calc_cmf(df, 20)
    obv_slope = calc_obv_slope(df, 20)
    vwap_pct  = calc_vwap_pct(df, 20)

    if mfi_val is None or cmf_val is None:
        return None

    mfi_score  = _clamp((mfi_val - 50) / 50)        # 50 = neutral
    cmf_score  = _clamp(cmf_val * 2)                 # CMF typically [-0.5, +0.5]
    obv_score  = _clamp(obv_slope * 10)              # normalised slope
    vwap_score = _clamp((vwap_pct or 0) / 5)        # ±5% = full signal

    money_flow_score = (
        mfi_score  * 0.30 +
        cmf_score  * 0.30 +
        obv_score  * 0.20 +
        vwap_score * 0.20
    )

    # ── Momentum / Trend (40% weight) ─────────────────────────────────────────
    rsi_val  = calc_rsi(close, 14)
    macd_res = calc_macd(close)
    ma50     = close.rolling(50).mean().iloc[-1]  if len(close) >= 50  else None
    ma200    = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None

    rsi_score  = _clamp((rsi_val - 50) / 50)  if rsi_val  else 0.0
    macd_hist  = macd_res[2]                   if macd_res else 0.0
    price_ref  = float(close.iloc[-1])
    macd_score = _clamp(macd_hist / price_ref * 200) if price_ref else 0.0
    ma_score   = _clamp((ma50 - ma200) / ma200 * 5) if (ma50 is not None and ma200 is not None and ma200 != 0) else 0.0

    direction_score = (
        rsi_score  * 0.35 +
        macd_score * 0.35 +
        ma_score   * 0.30
    )

    composite = money_flow_score * 0.60 + direction_score * 0.40

    hv       = calc_hv(close, 20)
    bb       = calc_bollinger(close)
    bb_pos   = bb["bb_position"] if bb else None

    return {
        # Raw indicators
        "mfi":         round(mfi_val, 2),
        "cmf":         round(cmf_val, 4),
        "obv_slope":   round(obv_slope, 6),
        "vwap_pct":    round(vwap_pct or 0, 2),
        "rsi":         round(rsi_val, 2) if rsi_val else None,
        "macd_hist":   round(macd_hist, 4),
        "hv_20d":      hv,
        "bb_position": round(bb_pos, 4) if bb_pos is not None else None,
        # Scores
        "money_flow_score": round(money_flow_score, 4),
        "direction_score":  round(direction_score, 4),
        "composite_score":  round(composite, 4),
        "current_price":    round(price_ref, 4),
    }


def rank_universe(data: dict[str, pd.DataFrame], top_n: int = 5) -> pd.DataFrame:
    """Score all stocks and return top_n sorted by composite_score descending."""
    rows = []
    for code, df in data.items():
        s = score_stock(df)
        if s:
            s["code"] = code
            rows.append(s)

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values("composite_score", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
