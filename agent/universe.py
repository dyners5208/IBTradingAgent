"""
Dynamic stock universe builder.

US: S&P 500 + Nasdaq 100 from Wikipedia (deduplicated), returned as bare tickers.
"""

import io
import json
import os
import threading
from datetime import date as _date
import requests
import pandas as pd

WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_US_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "us_universe_cache.json",
)
_US_LOCK = threading.Lock()

# ── US Universe ────────────────────────────────────────────────────────────────

def _fetch_sp500() -> list[str]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    ticker_col = next(
        (c for c in df.columns if str(c).lower() in ("symbol", "ticker", "ticker symbol")),
        df.columns[0],
    )
    tickers = df[ticker_col].dropna().astype(str).str.strip().tolist()
    return [t.replace(".", "-") for t in tickers if 1 <= len(t) <= 6]


def _fetch_nasdaq100() -> list[str]:
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))

    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        ticker_col = next(
            (df.columns[i] for i, c in enumerate(cols) if "ticker" in c or "symbol" in c),
            None,
        )
        if ticker_col is None and not isinstance(df.columns[0], str):
            first_row = df.iloc[0].astype(str).str.lower().tolist()
            if any("ticker" in v or "symbol" in v for v in first_row):
                df.columns = df.iloc[0]
                df = df.iloc[1:].reset_index(drop=True)
                ticker_col = next(
                    (c for c in df.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower()),
                    None,
                )

        if ticker_col is not None:
            tickers = df[ticker_col].dropna().astype(str).str.strip().tolist()
            valid = [t for t in tickers if 1 <= len(t) <= 7 and t.replace(".", "").isalpha()]
            if len(valid) >= 80:
                return valid

    return []


def get_us_universe() -> list[str]:
    """Return deduplicated S&P 500 + Nasdaq 100 tickers as bare ticker strings."""
    today = _date.today().isoformat()
    with _US_LOCK:
        try:
            if os.path.exists(_US_CACHE_FILE):
                with open(_US_CACHE_FILE) as f:
                    cached = json.load(f)
                if cached.get("date") == today and cached.get("universe"):
                    return cached["universe"]
        except Exception:
            pass

        sp500: list[str] = []
        ndx100: list[str] = []
        try:
            print("  Fetching S&P 500 constituents from Wikipedia...")
            sp500 = _fetch_sp500()
            print(f"    Found {len(sp500)} S&P 500 tickers.")
        except Exception as e:
            print(f"    S&P 500 scrape failed ({e}) — will use fallback.")
        try:
            print("  Fetching Nasdaq 100 constituents from Wikipedia...")
            ndx100 = _fetch_nasdaq100()
            print(f"    Found {len(ndx100)} Nasdaq 100 tickers.")
        except Exception as e:
            print(f"    Nasdaq 100 scrape failed ({e}).")

        combined = list(dict.fromkeys(sp500 + ndx100))
        if not combined:
            print("    Both US scrapes failed — using curated fallback list.")
            combined = _US_FALLBACK
        print(f"  Combined US universe: {len(combined)} unique tickers.")

        try:
            tmp = _US_CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"date": today, "universe": combined}, f)
            os.replace(tmp, _US_CACHE_FILE)
        except Exception:
            pass
        return combined


# ── HK Universe ───────────────────────────────────────────────────────────────

_HK_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hk_universe_cache.json",
)
_HK_LOCK = threading.Lock()


def _fetch_hsi_wiki() -> list[str]:
    url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        code_col = next(
            (df.columns[i] for i, c in enumerate(cols)
             if "ticker" in c or "code" in c or "symbol" in c),
            None,
        )
        if code_col is not None:
            raw = df[code_col].dropna().astype(str).str.strip().tolist()
            result = []
            for r in raw:
                code = r.upper().replace(".HK", "").strip()
                if code.isdigit():
                    result.append(f"HK.{code.zfill(4)}")
            if len(result) >= 30:
                return result
    return []


def get_hk_universe() -> list[str]:
    """Return HSI constituent tickers as HK.XXXX strings. Cached for the calendar day."""
    today = _date.today().isoformat()
    with _HK_LOCK:
        try:
            if os.path.exists(_HK_CACHE_FILE):
                with open(_HK_CACHE_FILE) as f:
                    cached = json.load(f)
                if cached.get("date") == today and cached.get("universe"):
                    return cached["universe"]
        except Exception:
            pass

        try:
            print("  Fetching HSI constituents from Wikipedia...")
            universe = _fetch_hsi_wiki()
            print(f"    Found {len(universe)} HSI tickers.")
        except Exception as e:
            print(f"    HSI scrape failed ({e}) — using fallback.")
            universe = []

        if not universe:
            universe = _HK_FALLBACK
            print(f"  Using HK fallback list ({len(universe)} tickers).")

        try:
            tmp = _HK_CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"date": today, "universe": universe}, f)
            os.replace(tmp, _HK_CACHE_FILE)
        except Exception:
            pass

        return universe


# ── Curated US fallback (large-cap liquid names; used only if both scrapes fail) ─

_US_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B",
    "JPM", "UNH", "V", "XOM", "LLY", "AVGO", "PG", "MA", "HD", "JNJ",
    "MRK", "COST", "ABBV", "CVX", "CRM", "BAC", "NFLX", "KO", "PEP",
    "TMO", "MCD", "WMT", "ADBE", "CSCO", "ABT", "ORCL", "ACN", "LIN",
    "AMD", "INTC", "QCOM", "TXN", "AMGN", "DHR", "VZ", "NEE", "PM",
    "RTX", "CAT", "GE", "HON",
]

# ── Curated HK fallback (core HSI large-caps; used only if Wikipedia scrape fails) ─

_HK_FALLBACK = [
    "HK.0700", "HK.9988", "HK.3690", "HK.9999", "HK.1299",
    "HK.0939", "HK.1398", "HK.3988", "HK.0388", "HK.2318",
    "HK.1211", "HK.2020", "HK.0005", "HK.0883", "HK.1109",
    "HK.2382", "HK.6098", "HK.0016", "HK.0027", "HK.0011",
    "HK.0066", "HK.1810", "HK.9618", "HK.6862", "HK.1024",
    "HK.0175", "HK.0002", "HK.0003", "HK.0006", "HK.2628",
    "HK.0386", "HK.0857", "HK.0762", "HK.0688", "HK.0823",
    "HK.1038", "HK.0012", "HK.0017", "HK.0083", "HK.0101",
]
