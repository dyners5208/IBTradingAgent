"""
Russell 2000 universe builder and pre-filter.

Approximates the Russell 2000 (small-cap US stocks, positions 1001-3000
by market cap) as the S&P 600 small-cap index via Wikipedia scrape.

Universe is cached for the calendar day so repeated intra-day calls
(e.g. on re-scan cycles) skip the Wikipedia round-trips.

Pre-filter (prefilter_russell) narrows the universe to ~25 options-eligible
candidates using recent daily bar data before the more expensive
kline-fetch + scoring stage.
"""

import io
import json
import os
import time
from datetime import datetime, timezone, timedelta, date as _date
import requests
import pandas as pd

_UNIVERSE_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "russell2000_universe_cache.json",
)

WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Pre-filter thresholds — tuned for small-cap characteristics
PRICE_MIN  = 5.0        # Exclude sub-$5 micro-caps (usually no options)
PRICE_MAX  = 500.0
MIN_VOLUME = 200_000    # Small caps trade lower volume; options check gates liquidity

_CACHED_UNIVERSE: list[str] | None = None
_CACHED_DATE:    str | None = None


def _fetch_sp600_wiki() -> list[str]:
    """Scrape S&P 600 small-cap constituents from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))

    for df in tables:
        cols_lower = [str(c).lower() for c in df.columns]
        ticker_col = next(
            (df.columns[i] for i, c in enumerate(cols_lower)
             if "ticker" in c or "symbol" in c),
            None,
        )
        if ticker_col is not None:
            tickers = df[ticker_col].dropna().astype(str).str.strip().tolist()
            valid = [
                t.replace(".", "-") for t in tickers
                if 1 <= len(t) <= 6 and t.replace("-", "").isalpha()
            ]
            if len(valid) >= 100:
                return valid

    return []


def get_russell2000_universe() -> list[str]:
    """
    Return Russell 2000 approximation as bare ticker strings.

    Uses the S&P 600 small-cap index (~600 names) as the primary source.
    Result is cached for the calendar day.
    """
    global _CACHED_UNIVERSE, _CACHED_DATE
    today = datetime.now().date().isoformat()

    if _CACHED_UNIVERSE is not None and _CACHED_DATE == today:
        return list(_CACHED_UNIVERSE)

    if os.path.exists(_UNIVERSE_CACHE_FILE):
        try:
            with open(_UNIVERSE_CACHE_FILE) as f:
                cached_disk = json.load(f)
            if cached_disk.get("date") == today:
                _CACHED_UNIVERSE = cached_disk["universe"]
                _CACHED_DATE     = today
                print(f"  [Russell 2000] Universe loaded from disk cache "
                      f"({len(_CACHED_UNIVERSE)} tickers).")
                return list(_CACHED_UNIVERSE)
        except Exception:
            pass

    print("  [Russell 2000] Building universe (S&P 600 small-cap)...")

    sp600: list[str] = []
    try:
        print("  [Russell 2000] Fetching S&P 600 small-caps from Wikipedia...")
        sp600 = _fetch_sp600_wiki()
        if sp600:
            print(f"    Found {len(sp600)} S&P 600 tickers.")
        else:
            raise ValueError("empty result")
    except Exception as e:
        print(f"    S&P 600 scrape failed ({e}) — using curated small-cap fallback.")
        sp600 = _SP600_FALLBACK

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in sp600:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    print(f"  [Russell 2000] Universe ready: {len(unique)} unique tickers.")
    _CACHED_UNIVERSE = unique
    _CACHED_DATE     = today

    try:
        with open(_UNIVERSE_CACHE_FILE, "w") as f:
            json.dump({"date": today, "universe": unique}, f)
    except Exception:
        pass

    return list(_CACHED_UNIVERSE)


def prefilter_russell(tickers: list[str], ctx=None, target: int = 25) -> list[str]:
    """
    Stage 1 pre-filter: narrow the Russell 2000 universe to `target`
    options-eligible candidates using recent daily bar data only.

    Pipeline:
      1. Batch Alpaca bar fetch (last 5 days) → keep tickers where latest
         close is in [PRICE_MIN, PRICE_MAX] and average volume >= MIN_VOLUME.
      2. Sort survivors by descending recent average volume.
      3. Walk the top-60 by volume and keep those with a listed options chain
         until we have `target` candidates.

    ctx is accepted for API compatibility but ignored.
    """
    from agent.data_fetcher import fetch_batch
    from ib_insync import Stock
    from agent.ibkr_client import get_ib, ibkr_lock

    by_volume: list[tuple[str, float]] = []
    chunk_size = 50  # IBKR pacing: fetch_batch handles sequentially with sleep

    print(f"  [Russell 2000] Pre-filtering {len(tickers)} candidates "
          f"(price ${PRICE_MIN:.0f}-${PRICE_MAX:.0f}, "
          f"volume >={MIN_VOLUME:,})...")

    # Step 1: Fetch 10-day bars via IBKR fetch_batch (sequential, cached)
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        bars_data = fetch_batch(chunk, days=10)
        for ticker, df in bars_data.items():
            if df is None or df.empty:
                continue
            latest_close = float(df["close"].iloc[-1])
            avg_vol      = float(df["volume"].mean())
            if PRICE_MIN <= latest_close <= PRICE_MAX and avg_vol >= MIN_VOLUME:
                by_volume.append((ticker, avg_vol))

    by_volume.sort(key=lambda x: x[1], reverse=True)
    print(f"    {len(by_volume)} pass price/volume filter.")

    # Step 2: Options availability check via reqSecDefOptParams on top-60 by volume
    options_ok: list[str] = []
    today_d = _date.today()
    min_exp = today_d + timedelta(days=14)
    max_exp = today_d + timedelta(days=90)

    for ticker, _ in by_volume[:60]:
        try:
            stock_con = Stock(ticker, "SMART", "USD")
            with ibkr_lock:
                ib = get_ib()
                ib.qualifyContracts(stock_con)
                chains = ib.reqSecDefOptParams(ticker, "", "STK", stock_con.conId)
            if chains:
                chain_info = next((c for c in chains if c.exchange == "SMART"), None) or chains[0]
                # Check if any expiry falls in the target window
                has_exp = any(
                    min_exp <= _date(int(e[:4]), int(e[4:6]), int(e[6:8])) <= max_exp
                    for e in chain_info.expirations
                )
                if has_exp:
                    options_ok.append(ticker)
                    if len(options_ok) >= target:
                        break
        except Exception:
            pass
        time.sleep(0.35)

    print(f"    {len(options_ok)} have listed options "
          f"— proceeding with {len(options_ok)} for scoring.")
    return options_ok


# ── Curated S&P 600 small-cap fallback ────────────────────────────────────────

_SP600_FALLBACK = [
    # Technology / Software
    "HUBS", "DOMO", "ALRM", "QLYS", "EVBG", "SPSC", "QTWO", "PAYO",
    "CWAN", "LPSN", "CSGS", "VIAV", "POWI", "DIOD", "SMTC", "AOSL",
    # Healthcare / Biotech
    "AMED", "ACAD", "NVCR", "INCY", "HALO", "PRGO", "ENSG", "MMSI",
    "AGIO", "KRTX", "STTK", "URGN", "RCUS", "ARWR", "DNLI",
    # Financials / Insurance
    "WSFS", "BRKL", "COLB", "HOPE", "CATY", "CVBF", "BANF", "FFIN",
    "TRMK", "SFBS", "HTLF", "BOKF", "SBCF", "HMST",
    # Consumer Discretionary
    "PLNT", "PLAY", "JACK", "CAKE", "TXRH", "DENN", "BJRI", "FRPT",
    "LEVI", "PRGS", "YELP", "RDFN", "RCII",
    # Consumer Staples
    "UNFI", "SPTN", "PFGC", "MGPI", "SENEA",
    # Energy
    "CIVI", "MTDR", "CRGY", "NOG", "VTLE", "REX", "SM", "CEIX",
    "DINO", "PARR",
    # Industrials / Transport
    "TREX", "KTOS", "IRDM", "ESAB", "GTES", "DOOR",
    "MATX", "WERN", "HUBG", "LSTR", "SAIA", "MRTN",
    # Real Estate
    "IIPR", "JBGS", "PDM", "EPRT", "NXRT", "STWD",
    # Materials
    "ARCH", "CSTM", "NGVT", "AVNT", "IOSP",
    # Utilities
    "MGEE", "AVA", "NWE", "SPKE",
]
