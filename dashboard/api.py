"""
IBTradingAgent Dashboard API

Start with:
    pip install fastapi uvicorn[standard]
    python dashboard/api.py

In development, run the React dev server separately:
    npm --prefix dashboard/frontend run dev  →  http://localhost:5173

In production (after npm run build):
    python dashboard/api.py  →  http://localhost:8050  (serves React + API)
"""

import json
import os
import sys
from datetime import date as _date
from pathlib import Path
from typing import Optional

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load ANTHROPIC_API_KEY from config.py if not already set in the environment
try:
    import config as _cfg
    _key = getattr(_cfg, "ANTHROPIC_API_KEY", None)
    if _key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = _key
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Any
import uvicorn

import dashboard.db as db

app = FastAPI(title="IBTradingAgent API", docs_url="/api/docs")

# Allow the Vite dev server (localhost:5173) to call the API during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── API endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    return db.get_stats()


@app.get("/api/pnl-history")
def pnl_history():
    return db.get_pnl_history()


@app.get("/api/by-strategy")
def by_strategy():
    return db.get_by_strategy()


@app.get("/api/trades/open")
def open_trades():
    return db.get_open_trades()


@app.get("/api/trades/closed")
def closed_trades():
    return db.get_closed_trades()


@app.get("/api/wheel")
def wheel():
    return db.get_wheel()


@app.get("/api/alerts")
def alerts():
    return db.get_alerts()


@app.get("/api/category-stats")
def category_stats():
    return db.get_category_analytics()


@app.get("/api/category-analytics")
def category_analytics():
    return db.get_category_analytics()


@app.get("/api/category-trades")
def category_trades(category: str = "US Spreads"):
    return db.get_category_trades(category)


@app.get("/api/strategy-analytics")
def strategy_analytics():
    return db.get_strategy_analytics()


@app.get("/api/portfolio-composition")
def portfolio_composition():
    return db.get_portfolio_composition()


# ── Post-mortem endpoints ──────────────────────────────────────────────────────

@app.get("/api/postmortem/losses")
def postmortem_losses():
    return db.get_losing_trades_with_postmortem()


@app.get("/api/postmortem/loss-breakdown")
def postmortem_loss_breakdown():
    return db.get_loss_breakdown()


@app.get("/api/postmortem/trade/{trade_id}")
def postmortem_trade(trade_id: int, force: bool = False):
    """
    Return cached post-mortem for a trade, or generate one.
    Pass ?force=true to regenerate even if cached.
    Claude narrative is included only when ANTHROPIC_API_KEY env var is set.
    """
    if not force:
        cached = db.get_postmortem(trade_id)
        if cached and cached.get("claude_analysis"):
            return cached

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent.postmortem import analyze_trade
        return analyze_trade(trade_id, api_key=api_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")


@app.get("/api/postmortem/insights")
def postmortem_insights(force: bool = False):
    """
    Return the latest weekly insights, or generate new ones.
    Requires ANTHROPIC_API_KEY env var.
    """
    if not force:
        cached = db.get_insights()
        if cached:
            return cached

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Set the ANTHROPIC_API_KEY environment variable to generate insights.",
        )
    try:
        from agent.postmortem import generate_weekly_insights
        return generate_weekly_insights(api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Insights generation failed: {e}")


@app.get("/api/gems")
def gem_universe(status: str = "active"):
    return db.get_gem_universe(status=status)


@app.post("/api/gems/research")
def gems_research(payload: dict[str, Any] = Body(default={})):
    """Run an AI-powered web search to find gem candidates for the given theme.
    Returns candidates WITHOUT saving them — the user approves in the dashboard.
    """
    theme = (payload.get("theme") or "").strip()
    if not theme:
        raise HTTPException(status_code=400, detail="theme is required")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Set ANTHROPIC_API_KEY in config.py to enable gem research.",
        )
    try:
        from agent.gem_research import research_gems
        candidates = research_gems(theme, api_key)
        return {"candidates": candidates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Research failed: {e}")


_METADATA_KEYS = {
    "moat", "pe_ratio", "pe_sector_avg", "pb_ratio", "pb_sector_avg",
    "ps_ratio", "ps_sector_avg", "revenue_growth_yoy_pct", "gross_margin_pct",
    "debt_to_equity", "debt_to_equity_sector_avg", "metrics_note",
}


@app.post("/api/gems/add")
def gems_add(payload: dict[str, Any] = Body(default={})):
    """Save user-approved gem candidates to the gem_universe table."""
    from agent.gem_manager import add_gem
    added = []
    for g in payload.get("gems", []):
        metadata = {k: g[k] for k in _METADATA_KEYS if k in g and g[k] is not None}
        add_gem(
            g["stock_code"], g.get("name", ""), g.get("sector", ""),
            g.get("macro_theme", ""), g.get("thesis", ""),
            int(g.get("conviction", 3)),
            metadata=metadata or None,
        )
        added.append(g["stock_code"])
    return {"added": added}


@app.post("/api/gems/remove")
def gems_remove(payload: dict[str, Any] = Body(default={})):
    """Mark a gem as removed."""
    stock_code = (payload.get("stock_code") or "").strip()
    if not stock_code:
        raise HTTPException(status_code=400, detail="stock_code is required")
    from agent.gem_manager import remove_gem
    remove_gem(stock_code, payload.get("reason", ""))
    return {"ok": True}


@app.get("/api/today-activity")
def today_activity():
    return db.get_today_activity()


@app.get("/api/account")
def account():
    try:
        from agent.risk_manager import get_account_info
        from agent.ibkr_client import is_paper
        info = get_account_info()
        if not info:
            return {"total_equity": None, "cash": None, "buying_power": None, "is_paper": is_paper()}
        return {
            "total_equity": info["total_assets"],
            "cash":         info["cash"],
            "buying_power": info["buying_power"],
            "is_paper":     is_paper(),
        }
    except Exception as exc:
        return {"total_equity": None, "cash": None, "buying_power": None, "is_paper": True, "error": str(exc)}


@app.get("/api/signals")
def signals():
    _root = Path(__file__).parent.parent
    candidates_file = _root / "daily_candidates.json"
    scan_cache_file = _root / "scan_results_cache.json"

    candidates: list = []
    scan_date: str | None = None
    if candidates_file.exists():
        try:
            data = json.loads(candidates_file.read_text(encoding="utf-8"))
            scan_date  = data.get("date")
            candidates = data.get("candidates", [])
        except Exception:
            pass

    total_scanned = 0
    if scan_cache_file.exists():
        try:
            sc = json.loads(scan_cache_file.read_text(encoding="utf-8"))
            total_scanned = len(sc.get("results", []))
        except Exception:
            pass

    buy_count = hold_count = sell_count = 0
    latest: list = []
    for c in candidates:
        score = float(c.get("Composite Score", 0) or 0)
        if score >= 0.10:
            signal = "BUY";  buy_count  += 1
        elif score <= -0.10:
            signal = "SELL"; sell_count += 1
        else:
            signal = "HOLD"; hold_count += 1
        code   = c.get("Code") or c.get("stock_code", "")
        ticker = code.split(".")[-1] if "." in code else code
        latest.append({
            "stock_code":  ticker,
            "signal":      signal,
            "score":       round(score, 3),
            "strategy":    c.get("Strategy", ""),
            "scan_source": c.get("scan_source"),
        })

    executed = db.get_today_activity().get("opened_count", 0)
    return {
        "buy":           buy_count,
        "hold":          hold_count,
        "sell":          sell_count,
        "total_scanned": total_scanned,
        "executed":      executed,
        "latest":        latest,
        "scan_date":     scan_date,
    }


@app.get("/api/budget")
def budget_utilization():
    try:
        from agent.risk_manager import get_account_info
        from agent.constants import (
            US_ALLOCATION_PCT, WHEEL_CSP_TOTAL_BUDGET_PCT,
            RUSSELL_ALLOCATION_PCT, POLITICIAN_ALLOCATION_PCT,
            GEM_US_ALLOCATION_PCT, TOP_N_STOCKS,
        )
        info   = get_account_info()
        equity = info["total_assets"] if info else 0.0
        cat    = db.get_category_analytics()

        def _row(name, pct):
            budget   = round(equity * pct, 2)
            deployed = round(cat.get(name, {}).get("open_income_usd", 0.0), 2)
            return {
                "name":     name,
                "budget":   budget,
                "deployed": deployed,
                "pct":      round(deployed / budget * 100, 1) if budget else 0,
            }

        return {
            "total_equity":  round(equity, 2),
            "max_positions": TOP_N_STOCKS,
            "categories": [
                _row("US Spreads",    US_ALLOCATION_PCT),
                _row("Wheel",         WHEEL_CSP_TOTAL_BUDGET_PCT),
                _row("Russell",       RUSSELL_ALLOCATION_PCT),
                _row("US Politician", POLITICIAN_ALLOCATION_PCT),
                _row("Gems",          GEM_US_ALLOCATION_PCT),
            ],
        }
    except Exception as exc:
        return {"total_equity": None, "categories": [], "error": str(exc)}


@app.get("/api/positions-live")
def positions_live():
    try:
        from agent.ibkr_client import get_ib, ibkr_lock
        with ibkr_lock:
            ib = get_ib()
            positions = ib.positions()
        result = []
        for pos in positions:
            con = pos.contract
            qty = float(pos.position or 0)
            if qty == 0:
                continue
            avg_cost = float(pos.avgCost or 0)
            symbol = getattr(con, "symbol", "") or ""
            result.append({
                "symbol":          symbol,
                "qty":             qty,
                "avg_entry_price": avg_cost,
                "current_price":   0.0,
                "market_value":    round(qty * avg_cost, 2),
                "unrealized_pl":   0.0,
                "unrealized_plpc": 0.0,
            })
        return result
    except Exception:
        return []


@app.get("/api/pnl-by-ticker")
def pnl_by_ticker():
    return db.get_pnl_by_ticker()


@app.get("/api/log")
def session_log(lines: int = 300):
    return db.get_session_log(lines=lines)


# ── Serve built React app (production) ────────────────────────────────────────

DIST = Path(__file__).parent / "frontend" / "dist"

if DIST.exists():
    # Mount static assets under /assets so index.html catch-all doesn't intercept them
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        return FileResponse(str(DIST / "index.html"))
else:
    @app.get("/")
    def root():
        return {
            "message": "API is running. Build the React frontend first:",
            "instructions": [
                "npm --prefix dashboard/frontend install",
                "npm --prefix dashboard/frontend run build",
                "Then restart this server.",
            ]
        }


if __name__ == "__main__":
    uvicorn.run("dashboard.api:app", host="127.0.0.1", port=8050, reload=False)
