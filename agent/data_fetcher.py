import os
import pickle
import threading
import time
from datetime import datetime, timezone, timedelta

import pandas as pd

# ── Kline disk cache ──────────────────────────────────────────────────────────
# Keyed by ticker; valid for one UTC calendar day.
# Populated by fetch_kline() on API call and by fetch_batch() on scan.

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KLINE_CACHE: dict[str, pd.DataFrame] = {}
_KLINE_CACHE_LOCK = threading.Lock()
_KLINE_CACHE_DATE: str = ""
_KLINE_CACHE_SAVE_LOCK = threading.Lock()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _kline_cache_path() -> str:
    return os.path.join(_PROJECT_ROOT, f"kline_cache_{_today_utc()}.pkl")


def _load_kline_cache() -> None:
    global _KLINE_CACHE_DATE
    today = _today_utc()
    with _KLINE_CACHE_LOCK:
        if _KLINE_CACHE_DATE == today:
            return
    path = _kline_cache_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        with _KLINE_CACHE_LOCK:
            if _KLINE_CACHE_DATE != today:
                _KLINE_CACHE.clear()
                _KLINE_CACHE.update(data)
                _KLINE_CACHE_DATE = today
        print(f"  [kline-cache] Loaded {len(data)} cached entries from disk.")
    except Exception as exc:
        print(f"  [kline-cache] Load failed: {exc}")


def _save_kline_cache() -> None:
    if not _KLINE_CACHE_SAVE_LOCK.acquire(blocking=False):
        return
    try:
        path = _kline_cache_path()
        tmp = path + ".tmp"
        today = _today_utc()
        try:
            with _KLINE_CACHE_LOCK:
                data = dict(_KLINE_CACHE)
            with open(tmp, "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
            for fname in os.listdir(_PROJECT_ROOT):
                if fname.startswith("kline_cache_") and fname.endswith(".pkl") and today not in fname:
                    try:
                        os.remove(os.path.join(_PROJECT_ROOT, fname))
                    except Exception:
                        pass
        except Exception as exc:
            print(f"  [kline-cache] Save failed: {exc}")
    finally:
        _KLINE_CACHE_SAVE_LOCK.release()


def _to_ibkr_stock(ticker: str):
    """Return ib_insync Stock for a US bare ticker or HK.XXXX code.
    HK.0700 → Stock('0700', 'SEHK', 'HKD')
    AAPL    → Stock('AAPL', 'SMART', 'USD')
    Does NOT call qualifyContracts — caller is responsible.
    """
    from ib_insync import Stock
    if ticker.startswith("HK."):
        return Stock(ticker[3:], "SEHK", "HKD")
    return Stock(ticker, "SMART", "USD")


def _to_yf_ticker(ticker: str) -> str:
    """Convert internal ticker format to yfinance format.
    HK.0700 → 0700.HK
    BRK.B   → BRK-B   (yfinance uses hyphen, not period)
    AAPL    → AAPL
    """
    if ticker.startswith("HK."):
        return ticker[3:] + ".HK"
    return ticker.replace(".", "-")


def _normalise_yf_df(raw: pd.DataFrame, ticker: str, days: int) -> pd.DataFrame | None:
    """Normalise a yfinance DataFrame to the format scorer/indicators expect.
    Writes result to the in-memory kline cache when days==260.
    """
    global _KLINE_CACHE_DATE

    if raw is None or raw.empty:
        return None

    df = raw.copy()
    df.columns = [str(c).lower() for c in df.columns]

    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if len(keep) < 5:
        return None

    df = df[keep]
    for col in keep:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[df["volume"] > 0].dropna(subset=["open", "high", "low", "close", "volume"])

    # yfinance returns tz-aware DatetimeIndex; strip tz for scorer compatibility
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    df = df.sort_index()

    if len(df) < 30:
        return None

    if days == 260:
        today = _today_utc()
        with _KLINE_CACHE_LOCK:
            if _KLINE_CACHE_DATE != today:
                _KLINE_CACHE.clear()
                _KLINE_CACHE_DATE = today
            _KLINE_CACHE[ticker] = df

    return df


# ── YF_BATCH_CHUNK controls how many tickers go to one yf.download() call.
# yfinance itself parallelises within the chunk (threads=True).
_YF_BATCH_CHUNK = 300


def fetch_kline(ticker: str, days: int = 260, ctx=None) -> pd.DataFrame | None:
    """Fetch daily OHLCV bars via yfinance.

    ctx is accepted but ignored — kept for API compatibility with callers.
    Cache is checked first; yfinance is only called on a miss.
    """
    if days == 260:
        today = _today_utc()
        with _KLINE_CACHE_LOCK:
            if _KLINE_CACHE_DATE == today and ticker in _KLINE_CACHE:
                return _KLINE_CACHE[ticker].copy()

    import yfinance as yf

    yf_ticker = _to_yf_ticker(ticker)
    start = (datetime.now(timezone.utc) - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    try:
        raw = yf.download(yf_ticker, start=start, auto_adjust=True,
                          progress=False, threads=False, multi_level_index=False)
    except Exception as exc:
        print(f"  [data_fetcher] {ticker}: yfinance error — {exc}")
        return None

    return _normalise_yf_df(raw, ticker, days)


def fetch_batch(universe: list[str], days: int = 260, ctx=None,
                delay: float = None) -> dict[str, pd.DataFrame]:
    """Fetch daily bars for a list of tickers via yfinance batch download.

    Splits universe into chunks of _YF_BATCH_CHUNK; each chunk is a single
    yf.download() call with threads=True (parallel within yfinance). Disk
    cache is used aggressively — only cache-missing tickers hit the network.
    """
    import yfinance as yf

    if days == 260:
        _load_kline_cache()

    results: dict[str, pd.DataFrame] = {}
    today = _today_utc()

    to_fetch: list[str] = []
    for ticker in universe:
        if days == 260:
            with _KLINE_CACHE_LOCK:
                cached = _KLINE_CACHE.get(ticker) if _KLINE_CACHE_DATE == today else None
            if cached is not None and len(cached) >= 30:
                results[ticker] = cached.copy()
                continue
        to_fetch.append(ticker)

    print(f"  [data_fetcher] {len(results)} tickers from cache; "
          f"fetching {len(to_fetch)} via yfinance")

    if not to_fetch:
        return results

    start = (datetime.now(timezone.utc) - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    for chunk_start in range(0, len(to_fetch), _YF_BATCH_CHUNK):
        chunk    = to_fetch[chunk_start: chunk_start + _YF_BATCH_CHUNK]
        yf_ticks = [_to_yf_ticker(t) for t in chunk]

        try:
            data = yf.download(yf_ticks, start=start, auto_adjust=True,
                               progress=False, threads=True)
        except Exception as exc:
            print(f"  [data_fetcher] yfinance chunk error: {exc} — falling back to singles")
            for ticker in chunk:
                df = fetch_kline(ticker, days=days)
                if df is not None:
                    results[ticker] = df
            continue

        if data is None or data.empty:
            continue

        has_multi = isinstance(data.columns, pd.MultiIndex)

        for ticker, yf_ticker in zip(chunk, yf_ticks):
            try:
                if has_multi:
                    raw = data.xs(yf_ticker, level=1, axis=1)
                else:
                    raw = data  # single-ticker download is flat
                df = _normalise_yf_df(raw, ticker, days)
                if df is not None:
                    results[ticker] = df
            except Exception:
                pass

    if days == 260:
        _save_kline_cache()

    return results


def fetch_intraday_kline(ticker: str, bars: int = 40, ctx=None) -> pd.DataFrame | None:
    """Fetch recent 15-min intraday bars for a ticker (US or HK)."""
    from ib_insync import util
    from agent.ibkr_client import get_ib, ibkr_lock

    contract = _to_ibkr_stock(ticker)

    try:
        with ibkr_lock:
            ib = get_ib()
            ib.qualifyContracts(contract)
            bar_data = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="15 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
    except Exception as exc:
        print(f"  [data_fetcher] {ticker} intraday: {exc}")
        return None

    if not bar_data:
        return None

    df = util.df(bar_data)
    if df is None or df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date")
    df.index.name = "date"
    df = df.sort_index()

    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep]
    for col in keep:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["volume"] > 0].dropna()

    # Keep only today's US bars
    from agent.market_hours import market_today
    today_d = market_today("US")
    df = df[df.index.date == today_d]

    return df if not df.empty else None


def get_stock_names(tickers: list[str], ctx=None) -> dict[str, str]:
    """Return {ticker: display_name} by querying IBKR contract details."""
    from agent.ibkr_client import get_ib, ibkr_lock

    result: dict[str, str] = {}
    for ticker in tickers:
        try:
            contract = _to_ibkr_stock(ticker)
            with ibkr_lock:
                ib      = get_ib()
                details = ib.reqContractDetails(contract)
            if details:
                result[ticker] = details[0].longName or ticker
            else:
                result[ticker] = ticker
        except Exception:
            result[ticker] = ticker
    return result


def get_stock_lot_sizes(tickers: list[str]) -> dict[str, int]:
    """Return {ticker: board_lot_size}. Falls back to 100 (HK) or 1 (US)."""
    from agent.ibkr_client import get_ib, ibkr_lock

    result: dict[str, int] = {}
    for ticker in tickers:
        default = 100 if ticker.startswith("HK.") else 1
        try:
            contract = _to_ibkr_stock(ticker)
            with ibkr_lock:
                ib      = get_ib()
                details = ib.reqContractDetails(contract)
            if details:
                min_size = getattr(details[0].contract, "minSize", None)
                result[ticker] = int(min_size) if min_size and int(min_size) > 0 else default
            else:
                result[ticker] = default
        except Exception:
            result[ticker] = default
    return result
