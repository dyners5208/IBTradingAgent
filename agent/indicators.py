import numpy as np
import pandas as pd


def _safe_last(series: pd.Series) -> float | None:
    if series.empty or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


# ── Money Flow Indicators ──────────────────────────────────────────────────────

def calc_mfi(df: pd.DataFrame, period: int = 14) -> float | None:
    """Money Flow Index — volume-weighted RSI measuring buying/selling pressure."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]

    pos_mf = rmf.where(tp > tp.shift(1), 0.0)
    neg_mf = rmf.where(tp < tp.shift(1), 0.0)

    pos_sum = pos_mf.rolling(period).sum()
    neg_sum = neg_mf.rolling(period).sum()

    mfr = pos_sum / neg_sum.replace(0, np.nan)
    mfi_series = 100 - (100 / (1 + mfr))
    return _safe_last(mfi_series)


def calc_cmf(df: pd.DataFrame, period: int = 20) -> float | None:
    """Chaikin Money Flow — accumulation/distribution [-1, +1].
    Positive = institutional accumulation; negative = distribution."""
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range
    mfv = mfm * df["volume"]

    cmf_series = mfv.rolling(period).sum() / df["volume"].rolling(period).sum()
    return _safe_last(cmf_series)


def calc_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume cumulative series."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def calc_obv_slope(df: pd.DataFrame, period: int = 20) -> float:
    """Normalised OBV linear-regression slope over `period` days.
    Positive = rising institutional accumulation."""
    obv = calc_obv(df).iloc[-period:]
    if len(obv) < 2:
        return 0.0
    x = np.arange(len(obv))
    slope = np.polyfit(x, obv.values, 1)[0]
    mean_obv = abs(obv.mean())
    return float(slope / mean_obv) if mean_obv != 0 else 0.0


def calc_vwap(df: pd.DataFrame, period: int = 20) -> float | None:
    """Rolling VWAP over `period` days."""
    recent = df.tail(period)
    if recent.empty:
        return None
    tp = (recent["high"] + recent["low"] + recent["close"]) / 3
    vol_sum = recent["volume"].sum()
    return float((tp * recent["volume"]).sum() / vol_sum) if vol_sum > 0 else None


def calc_vwap_pct(df: pd.DataFrame, period: int = 20) -> float | None:
    """(Close - VWAP) / VWAP × 100. Positive = price above VWAP (bullish)."""
    v = calc_vwap(df, period)
    if v is None or v == 0:
        return None
    return float((df["close"].iloc[-1] - v) / v * 100)


# ── Trend / Momentum Indicators ───────────────────────────────────────────────

def calc_rsi(prices: pd.Series, period: int = 14) -> float | None:
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    last_loss = _safe_last(avg_loss)
    if last_loss is None:
        return None
    if last_loss == 0:
        return 100.0
    rs = float(avg_gain.iloc[-1]) / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
              ) -> tuple[float, float, float] | None:
    """Returns (macd_line, signal_line, histogram)."""
    if len(prices) < slow + signal:
        return None
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    last = _safe_last(hist)
    if last is None:
        return None
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(hist.iloc[-1])


def calc_hv(prices: pd.Series, period: int = 20) -> float | None:
    """Annualised historical volatility (20-day) as a percentage."""
    log_ret = np.log(prices / prices.shift(1)).dropna()
    if len(log_ret) < period:
        return None
    hv = log_ret.tail(period).std() * np.sqrt(252)
    return round(float(hv) * 100, 2)


_BULLISH_STRATS_GATE = frozenset({
    "Bull Put Spread", "Bull Call Spread", "Cash-Secured Put", "Stock Buy",
})
_BEARISH_STRATS_GATE = frozenset({
    "Bear Call Spread", "Bear Put Spread", "Stock Sell Short",
})


def check_intraday_momentum(df: pd.DataFrame | None,
                            strategy: str) -> tuple[bool, str]:
    """
    Lightweight intraday momentum gate for 30-min re-scan entries.

    Uses today's 15-min bars to compute true intraday VWAP (resets each
    session) and a fast RSI(9).  Both signals must oppose the trade direction
    before the gate blocks — requiring two confirming signals prevents false
    negatives from short-lived noise.

    Returns (gate_passes, reason_string).
      gate_passes=True  → proceed with the entry attempt
      gate_passes=False → skip this cycle; re-scan will retry in 30 min
    """
    from agent.constants import (
        INTRADAY_GATE_MIN_BARS, INTRADAY_GATE_RSI_PERIOD,
        INTRADAY_GATE_RSI_BULL_FLOOR, INTRADAY_GATE_RSI_BEAR_CEILING,
        INTRADAY_GATE_VWAP_PCT_THRESHOLD,
    )

    # Neutral / income strategies don't depend on intraday direction
    if strategy not in _BULLISH_STRATS_GATE and strategy not in _BEARISH_STRATS_GATE:
        return True, "neutral strategy — gate bypassed"

    n_bars = len(df) if df is not None else 0
    if df is None or n_bars < INTRADAY_GATE_MIN_BARS:
        return True, f"only {n_bars} intraday bar(s) — too early in session, gate bypassed"

    # True intraday VWAP: cumulative (TP × vol) / cumulative vol using today's bars only
    tp      = (df["high"] + df["low"] + df["close"]) / 3
    vol_sum = float(df["volume"].sum())
    vwap    = (float((tp * df["volume"]).sum() / vol_sum)
               if vol_sum > 0 else float(df["close"].iloc[-1]))
    last    = float(df["close"].iloc[-1])
    vwap_pct = (last - vwap) / vwap * 100 if vwap != 0 else 0.0

    # RSI on 15-min closes (skipped when not enough bars — VWAP alone insufficient to block)
    rsi: float | None = None
    if n_bars >= INTRADAY_GATE_RSI_PERIOD + 1:
        rsi = calc_rsi(df["close"], INTRADAY_GATE_RSI_PERIOD)

    rsi_str = f"{rsi:.1f}" if rsi is not None else "n/a"
    detail  = (f"15m-RSI={rsi_str}  VWAP%={vwap_pct:+.2f}%  "
               f"price={last:.2f}  vwap={vwap:.2f}  bars={n_bars}")

    if strategy in _BULLISH_STRATS_GATE:
        if (rsi is not None
                and rsi < INTRADAY_GATE_RSI_BULL_FLOOR
                and vwap_pct < -INTRADAY_GATE_VWAP_PCT_THRESHOLD):
            return False, f"intraday selling pressure — {detail}"
        return True, f"gate passed — {detail}"

    # Bearish strategy
    if (rsi is not None
            and rsi > INTRADAY_GATE_RSI_BEAR_CEILING
            and vwap_pct > INTRADAY_GATE_VWAP_PCT_THRESHOLD):
        return False, f"intraday buying pressure — {detail}"
    return True, f"gate passed — {detail}"


def calc_bollinger(prices: pd.Series, period: int = 20, std_dev: float = 2.0
                   ) -> dict | None:
    """Returns upper/mid/lower bands, bandwidth%, and BB% position [0,1]."""
    if len(prices) < period:
        return None
    sma = prices.rolling(period).mean()
    std = prices.rolling(period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    u, m, l = float(upper.iloc[-1]), float(sma.iloc[-1]), float(lower.iloc[-1])
    bw = (u - l) / m * 100 if m != 0 else 0
    bb_pct = (prices.iloc[-1] - l) / (u - l) if (u - l) != 0 else 0.5
    return {"upper": u, "mid": m, "lower": l, "bandwidth_pct": round(bw, 2),
            "bb_position": round(float(bb_pct), 4)}
