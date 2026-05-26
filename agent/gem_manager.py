"""
Gem Universe manager — research interface for Claude Code sessions.

Usage (from a Claude Code session):
    from agent.gem_manager import add_gem, remove_gem, list_gems

    add_gem("US.AXON", "Axon Enterprise", "Public Safety Tech",
            "AI-driven law enforcement", "Near-monopoly on Taser + body cameras...",
            conviction=4)

    remove_gem("US.AXON", "thesis broken — competition entering")

    list_gems()          # returns active gems
    list_gems("removed") # returns removed gems
"""

import json as _json
import os
import sqlite3
from datetime import datetime, timezone

from agent.position_manager import init_db as _init_db, DB_PATH as _DB_PATH

_init_db()   # ensure gem_universe table exists (safe to call multiple times)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def add_gem(stock_code: str,
            name: str,
            sector: str,
            macro_theme: str,
            thesis: str,
            conviction: int = 3,
            metadata: dict | None = None) -> None:
    """Add or reactivate a gem. Conviction: 1 (speculative) to 5 (high confidence)."""
    now = datetime.now(timezone.utc).isoformat()
    meta_str = _json.dumps(metadata) if metadata else None
    with _conn() as c:
        c.execute(
            """INSERT INTO gem_universe
                   (stock_code, name, sector, macro_theme, thesis, conviction, added_at, status, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   name=excluded.name, sector=excluded.sector,
                   macro_theme=excluded.macro_theme, thesis=excluded.thesis,
                   conviction=excluded.conviction, added_at=excluded.added_at,
                   status='active', removed_at=NULL, removal_reason=NULL,
                   metadata=excluded.metadata""",
            (stock_code, name, sector, macro_theme, thesis, conviction, now, meta_str),
        )
    print(f"  [gems] Added: {stock_code} ({name}) — conviction {conviction}/5")


def remove_gem(stock_code: str, reason: str = "") -> None:
    """Mark a gem as removed. It will no longer be scored or traded."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        rows = c.execute(
            "UPDATE gem_universe SET status='removed', removed_at=?, removal_reason=? "
            "WHERE stock_code=? AND status='active'",
            (now, reason, stock_code),
        ).rowcount
    if rows:
        print(f"  [gems] Removed: {stock_code} — {reason}")
    else:
        print(f"  [gems] {stock_code} not found or already removed")


def list_gems(status: str = "active") -> list[dict]:
    """Return gems with the given status. Default: active gems only."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM gem_universe WHERE status=? ORDER BY conviction DESC, added_at ASC",
            (status,),
        ).fetchall()
    gems = [dict(r) for r in rows]
    if gems:
        print(f"\n  {'='*60}")
        print(f"  Gem Universe — {status.upper()}  ({len(gems)} stock(s))")
        print(f"  {'='*60}")
        for g in gems:
            print(f"  {g['stock_code']:12}  {g['name'] or '':25}  conv={g['conviction']}/5"
                  f"  score={g['last_score'] or 'n/a'}"
                  f"  [{g['sector'] or ''}]")
            print(f"             Theme: {g['macro_theme'] or 'n/a'}")
            print(f"             Thesis: {(g['thesis'] or '')[:120]}")
        print(f"  {'='*60}\n")
    else:
        print(f"  [gems] No {status} gems found.")
    return gems


def update_gem_score(stock_code: str, score: float, score_date: str) -> None:
    """Update last_score and last_scored. Called by gem_scanner after each scoring run."""
    with _conn() as c:
        c.execute(
            "UPDATE gem_universe SET last_score=?, last_scored=? WHERE stock_code=?",
            (round(score, 4), score_date, stock_code),
        )
