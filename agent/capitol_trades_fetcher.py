"""
Fetches and caches US politician trade disclosures from Capitol Trades.
Ranks politicians by risk-adjusted win rate over the trailing 1 year.
"""
import json
import os
import threading
import time
import datetime
import math
import statistics
import logging

import requests
from bs4 import BeautifulSoup

_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "politician_trades_cache.json",
)
_SEEN_FILINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "politician_seen_filings.json",
)
_CACHE_TTL_HOURS = 20
_REQUEST_DELAY = 1.5   # seconds between page requests
_BASE_URL = "https://www.capitoltrades.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# STOCK Act declared amount ranges → midpoints used for trade weighting
AMOUNT_MIDPOINTS = {
    "$1K - $15K":     8_000,
    "$15K - $50K":    32_500,
    "$50K - $100K":   75_000,
    "$100K - $250K":  175_000,
    "$250K - $500K":  375_000,
    "$500K - $1M":    750_000,
    "$1M - $5M":      3_000_000,
    ">$5M":           5_000_000,
}

log = logging.getLogger(__name__)
_FETCH_LOCK = threading.Lock()   # prevents duplicate concurrent scrapes


def _load_cache() -> dict | None:
    if not os.path.exists(_CACHE_FILE):
        return None
    try:
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        cached_date = data.get("date", "")
        cached_dt   = datetime.datetime.fromisoformat(cached_date) if cached_date else None
        if cached_dt and (datetime.datetime.now() - cached_dt).total_seconds() < _CACHE_TTL_HOURS * 3600:
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    tmp = _CACHE_FILE + ".tmp"
    data["date"] = datetime.datetime.now().isoformat()
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, _CACHE_FILE)


def make_filing_key(trade: dict) -> str:
    """Stable composite key: '{politician}|{ticker}|{filing_date}'."""
    return f"{trade['politician']}|{trade['ticker']}|{trade.get('filing_date', '')}"


def load_seen_filings() -> set[str]:
    """Return set of filing keys already evaluated in prior scans."""
    if not os.path.exists(_SEEN_FILINGS_FILE):
        return set()
    try:
        with open(_SEEN_FILINGS_FILE) as f:
            data = json.load(f)
        return set(data.get("seen", []))
    except Exception:
        return set()


def save_seen_filings(seen: set[str]) -> None:
    """Persist seen filing keys; prune entries with filing dates older than 365 days."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    # Key format: "{politician}|{ticker}|{YYYY-MM-DD}" — date is the last token
    pruned = {k for k in seen if k.rsplit("|", 1)[-1] >= cutoff}
    tmp = _SEEN_FILINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(
            {"seen": sorted(pruned), "updated": datetime.datetime.now().isoformat()},
            f, indent=2,
        )
    os.replace(tmp, _SEEN_FILINGS_FILE)


def _parse_date(raw: str) -> str | None:
    """Normalise various date formats from Capitol Trades to ISO YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _normalize_amount(raw: str) -> str:
    """
    Convert Capitol Trades size string to AMOUNT_MIDPOINTS key format.
    Site uses en-dash and no $ signs: "1K–15K" → "$1K - $15K".
    """
    # Replace en-dash / em-dash with hyphen
    s = raw.replace("–", "-").replace("—", "-")
    parts = [p.strip() for p in s.split("-") if p.strip()]
    normalised = []
    for p in parts:
        if p.startswith(">"):
            normalised.append(">" + ("$" if not p[1:].startswith("$") else "") + p[1:])
        else:
            normalised.append(("$" if not p.startswith("$") else "") + p)
    return " - ".join(normalised) if len(normalised) > 1 else (normalised[0] if normalised else raw)


def _scrape_trades_page(page: int = 1) -> tuple[int, list[dict]]:
    """Scrape one page of stock trade disclosures.

    Returns (raw_row_count, us_stock_purchases) so callers can distinguish
    "page is empty → end of data" from "page has rows but none are US stock buys".

    Column order as of 2026-05 (confirmed by inspection):
      0: Politician  1: Issued (name | TICK:MKT)  2: Published (filing date)
      3: Traded (transaction date)  4: Filed After (days)  5: Owner
      6: Type (buy/sell/purchase)  7: Size (amount range)  8: Price  9: Link
    """
    # txDate=365d, txType=buy, assetType=stock are all server-side filters (verified).
    # The old asset_type / transaction params were client-side only and had no effect.
    url = f"{_BASE_URL}/trades?txDate=365d&txType=buy&assetType=stock&page={page}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Capitol Trades fetch failed (page {page}): {e}")
        return 0, []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        log.warning(f"Capitol Trades page {page}: no table found — site structure may have changed")
        return 0, []

    rows = table.select("tbody tr")
    raw_count = len(rows)
    trades = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        # Type filter — URL query param is client-side only; enforce server-side here
        txn_type = cells[6].get_text(strip=True).lower()
        if txn_type not in ("purchase", "buy"):
            continue

        # Politician name — first <a> tag is cleanest
        pol_a = cells[0].find("a")
        politician = (pol_a.get_text(strip=True) if pol_a
                      else cells[0].get_text(separator="|", strip=True).split("|")[0])

        # Ticker — last "|"-separated token in cell 1 is "TICK:MKT"; US only
        issuer_parts = cells[1].get_text(separator="|", strip=True).split("|")
        ticker = ""
        if len(issuer_parts) >= 2:
            tick_mkt = issuer_parts[-1].strip()   # e.g. "MSFT:US"
            if ":" in tick_mkt:
                tick, mkt = tick_mkt.rsplit(":", 1)
                if mkt.upper() == "US":
                    ticker = tick.upper().replace("/", ".").replace("-", ".")

        filing_date      = _parse_date(cells[2].get_text(separator=" ", strip=True))   # Published
        transaction_date = _parse_date(cells[3].get_text(separator=" ", strip=True))   # Traded

        if not ticker or not filing_date:
            continue

        amount_range = _normalize_amount(cells[7].get_text(strip=True))

        trades.append({
            "politician":        politician,
            "ticker":            ticker,
            "transaction_date":  transaction_date or filing_date,
            "filing_date":       filing_date,
            "asset_type":        "stock",
            "amount_range":      amount_range,
            "transaction_type":  "purchase",
        })

    return raw_count, trades


def fetch_all_trades(lookback_days: int = 365) -> list[dict]:
    """
    Scrape all stock buy disclosures from the past `lookback_days` days.
    Returns from cache if fresh (< 20 h old).
    Lock ensures a pre-warm thread and a scan thread never double-scrape: the
    second caller blocks until the first finishes, then gets an instant cache hit.
    """
    with _FETCH_LOCK:
        cached = _load_cache()
        if cached:
            log.info(f"Capitol Trades: returning {len(cached.get('trades', []))} cached trades")
            return cached.get("trades", [])

        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        all_trades: list[dict] = []

        for page in range(1, 101):   # cap at 100 pages (~1,200 rows); ~2.5 min at 1.5s/page
            raw_count, page_trades = _scrape_trades_page(page)
            # Break only when the page itself has no rows (end of data).
            # A page with rows but zero qualifying US stock purchases (e.g. all bond
            # buys or all sells) is normal — continue to the next page.
            if raw_count == 0:
                break
            all_trades.extend(page_trades)

            # Stop early when all purchases on this page pre-date the lookback window
            try:
                oldest = min(
                    datetime.date.fromisoformat(t["filing_date"])
                    for t in page_trades
                    if t.get("filing_date")
                )
                if oldest < cutoff:
                    break
            except (ValueError, TypeError):
                pass

            time.sleep(_REQUEST_DELAY)

        # Server already filters to 365d + buy + stock; apply cutoff as a safety net only
        stock_buys = [
            t for t in all_trades
            if t.get("filing_date", "") >= str(cutoff)
        ]

        # Only cache when we got real data — empty results indicate a scrape failure
        if stock_buys:
            _save_cache({"trades": stock_buys})
        log.info(f"Capitol Trades: fetched {len(stock_buys)} stock buys (past {lookback_days} days)")
        return stock_buys


def rank_politicians(trades: list[dict], quote_ctx,
                     min_trades: int = 5, top_n: int = 5) -> list[dict]:
    """
    Score each politician by risk-adjusted win rate measured from filing date.
    Returns list of top_n dicts sorted by score descending.
    """
    import contextlib as _cl, io as _io
    from agent.data_fetcher import fetch_kline, fetch_batch as _fetch_batch

    # Batch pre-fetch all unique tickers silently — populates the 260-day kline
    # cache and suppresses "possibly delisted" noise for invalid/renamed tickers.
    # Subsequent fetch_kline(days=260) calls inside the loop are then cache hits.
    _unique = list({
        t.get("ticker", "").strip().replace("/", ".").replace("-", ".")
        for t in trades
        if t.get("ticker") and 1 <= len(t.get("ticker", "")) <= 7
    })
    _buf = _io.StringIO()
    with _cl.redirect_stdout(_buf), _cl.redirect_stderr(_buf):
        _cached = _fetch_batch(_unique, days=260)
    # Only tickers that successfully downloaded — skip delisted/renamed ones silently
    _valid_tickers = set(_cached.keys())

    by_pol: dict[str, list[dict]] = {}
    for t in trades:
        by_pol.setdefault(t["politician"], []).append(t)

    results = []
    for politician, pol_trades in by_pol.items():
        if len(pol_trades) < min_trades:
            continue

        returns = []
        for t in pol_trades:
            ticker = t.get("ticker", "").strip().replace("/", ".").replace("-", ".")
            if not ticker or len(ticker) > 7:
                continue
            if ticker not in _valid_tickers:
                continue
            try:
                filing_date = datetime.date.fromisoformat(t["filing_date"])
            except (ValueError, TypeError):
                continue

            days_ago = (datetime.date.today() - filing_date).days + 5
            if days_ago < 5:
                continue

            # days=260 always → uses kline cache (no yfinance I/O, no sleep needed)
            df = fetch_kline(ticker, days=260)
            if df is None or df.empty:
                continue

            df = df.copy()
            df["_date"] = [
                r.date() if hasattr(r, "date") else datetime.date.fromisoformat(str(r)[:10])
                for r in df.index
            ]
            filing_rows = df[df["_date"] >= filing_date]
            if filing_rows.empty:
                continue

            price_at_filing = float(filing_rows.iloc[0]["close"])
            price_now       = float(df.iloc[-1]["close"])
            if price_at_filing <= 0:
                continue

            returns.append((price_now - price_at_filing) / price_at_filing)

        if not returns:
            continue

        win_rate   = sum(1 for r in returns if r > 0) / len(returns)
        avg_return = statistics.mean(returns)
        confidence = math.log1p(len(returns))
        score      = win_rate * confidence * max(avg_return, 0.0)

        results.append({
            "politician":  politician,
            "score":       round(score, 6),
            "win_rate":    round(win_rate, 4),
            "avg_return":  round(avg_return, 4),
            "trade_count": len(returns),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


def get_recent_trades_for(politician: str, trades: list[dict], days: int = 30) -> list[dict]:
    """Return stock buy disclosures filed in the past `days` days for one politician."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    out = []
    for t in trades:
        if t.get("politician") != politician:
            continue
        try:
            if datetime.date.fromisoformat(t["filing_date"]) >= cutoff:
                out.append(t)
        except (ValueError, TypeError):
            continue
    return out
