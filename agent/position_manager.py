"""
Position manager: persistent trade log (SQLite + WAL), duplicate detection.

Public API is identical to the previous JSON-backed version — all callers
are unchanged.  The DB file (trade_log.db) lives next to the old trade_log.json.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

_DB_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(_DB_DIR, "trade_log.db")
_DB_LOCK = threading.Lock()   # serialises same-process concurrent writes

# Tracks stocks closed via trail_stop or cut_loss this session — prevents
# immediate re-entry within the same trading session.  Resets on agent restart.
_SESSION_COOLDOWN: dict[str, str] = {}   # {stock_code: close_reason}
_COOLDOWN_REASONS = frozenset({"trail_stop", "cut_loss"})

# ── Columns that map 1-to-1 with the trades table ─────────────────────────────
# Any field in an incoming trade dict that is NOT in this set is stored in
# the extra_json TEXT column so schema migrations aren't needed for new fields.
_TRADE_COLUMNS: frozenset = frozenset({
    "stock_code", "strategy", "market", "trade_type", "status",
    "exp_date", "num_contracts", "multiplier", "net_credit_per_spread",
    "tp_value", "cl_value", "placement_status",
    "side", "qty", "limit_price", "cost", "order_id",
    "opened_at", "closed_at", "close_reason", "close_pnl",
    "thesis_last_checked", "thesis_score",
    "_pending_close_reason", "_pending_close_pnl", "_pending_close_date",
    "_close_attempt_count", "_trailing_high",
    "wheel_type", "wheel_assignment_price", "wheel_premium_accumulated",
    "scan_source", "rolled_from", "roll_credit",
    "entry_score", "entry_iv",
    "unrealized_pnl",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code                  TEXT    NOT NULL,
    strategy                    TEXT    NOT NULL,
    market                      TEXT    NOT NULL,
    trade_type                  TEXT    NOT NULL,
    status                      TEXT    NOT NULL DEFAULT 'open',
    exp_date                    TEXT,
    num_contracts               INTEGER,
    multiplier                  INTEGER,
    net_credit_per_spread       REAL,
    tp_value                    REAL,
    cl_value                    REAL,
    placement_status            TEXT,
    side                        TEXT,
    qty                         INTEGER,
    limit_price                 REAL,
    cost                        REAL,
    order_id                    TEXT,
    opened_at                   TEXT    NOT NULL,
    closed_at                   TEXT,
    close_reason                TEXT,
    close_pnl                   REAL,
    thesis_last_checked         TEXT,
    thesis_score                REAL,
    _pending_close_reason       TEXT,
    _pending_close_pnl          REAL,
    _pending_close_date         TEXT,
    _close_attempt_count        INTEGER DEFAULT 0,
    _trailing_high              REAL,
    wheel_type                  TEXT,
    wheel_assignment_price      REAL,
    wheel_premium_accumulated   REAL,
    scan_source                 TEXT,
    rolled_from                 TEXT,
    roll_credit                 REAL,
    entry_score                 REAL,
    entry_iv                    REAL,
    unrealized_pnl              REAL,
    extra_json                  TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS legs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    code            TEXT    NOT NULL,
    strike          REAL,
    call_or_put     TEXT,
    side            TEXT,
    delta           REAL,
    bid             REAL,
    ask             REAL,
    theta           REAL,
    iv              REAL,
    order_id        TEXT,
    limit_price     REAL,
    avg_fill_price  REAL,
    qty             INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_stock_code ON trades(stock_code);
CREATE INDEX IF NOT EXISTS idx_trades_status     ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_opened_at  ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_strategy   ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_legs_trade_id     ON legs(trade_id);

CREATE TABLE IF NOT EXISTS gem_universe (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code     TEXT NOT NULL UNIQUE,
    name           TEXT,
    sector         TEXT,
    macro_theme    TEXT,
    thesis         TEXT,
    conviction     INTEGER DEFAULT 3,
    added_at       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    removed_at     TEXT,
    removal_reason TEXT,
    last_scored    TEXT,
    last_score     REAL,
    metadata       TEXT
);

CREATE INDEX IF NOT EXISTS idx_gem_status ON gem_universe(status);
"""

_LEG_COLS = (
    "code", "strike", "call_or_put", "side", "delta",
    "bid", "ask", "theta", "iv", "order_id",
    "limit_price", "avg_fill_price", "qty",
)


# ── Connection + schema init ───────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema creation."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    new_cols = [
        ("entry_score",    "REAL"),
        ("entry_iv",       "REAL"),
        ("unrealized_pnl", "REAL"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    gem_existing = {row[1] for row in conn.execute("PRAGMA table_info(gem_universe)")}
    gem_new_cols = [("metadata", "TEXT")]
    for col, typ in gem_new_cols:
        if col not in gem_existing:
            conn.execute(f"ALTER TABLE gem_universe ADD COLUMN {col} {typ}")


def init_db() -> None:
    """Create tables and indexes if they don't exist yet. Safe to call multiple times."""
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA_SQL)
        _migrate_db(conn)
        conn.commit()
    finally:
        conn.close()


init_db()   # run once at import time


# ── Row → dict conversion ──────────────────────────────────────────────────────

def _leg_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("id", None)
    d.pop("trade_id", None)
    return {k: v for k, v in d.items() if v is not None}


def _trade_row_to_dict(row: sqlite3.Row, legs: list) -> dict:
    d = dict(row)
    d.pop("id", None)
    extra = json.loads(d.pop("extra_json", None) or "{}")
    # extra_json is a low-priority fallback; column values always win
    result = {**extra}
    result.update({k: v for k, v in d.items() if v is not None})
    result["legs"] = legs
    return result


# ── Internal write helpers ─────────────────────────────────────────────────────

def _insert_legs(conn: sqlite3.Connection, trade_id: int, legs: list) -> None:
    ph = ", ".join("?" * (len(_LEG_COLS) + 1))
    for leg in legs:
        conn.execute(
            f"INSERT INTO legs (trade_id, {', '.join(_LEG_COLS)}) VALUES ({ph})",
            [trade_id] + [leg.get(c) for c in _LEG_COLS],
        )


def _insert_trade_row(trade: dict) -> int:
    """
    Insert a trade dict (and its legs) into the DB.
    Returns the new trade id.
    Called by add_trade() and the migration script.
    """
    t = dict(trade)
    t.setdefault("opened_at", datetime.now().isoformat())
    t.setdefault("status", "open")

    legs = t.pop("legs", []) or []

    col_vals = {k: v for k, v in t.items() if k in _TRADE_COLUMNS}
    extra_vals = {k: v for k, v in t.items() if k not in _TRADE_COLUMNS}
    col_vals["extra_json"] = json.dumps(extra_vals, default=str)

    columns      = ", ".join(col_vals.keys())
    placeholders = ", ".join("?" * len(col_vals))

    conn = _get_conn()
    try:
        with conn:
            cur = conn.execute(
                f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
                list(col_vals.values()),
            )
            trade_id = cur.lastrowid
            _insert_legs(conn, trade_id, legs)
        return trade_id
    finally:
        conn.close()


def _fetch_trades(where_sql: str = "", params: tuple = ()) -> list:
    """Shared query for get_open_trades() and get_all_trades()."""
    conn = _get_conn()
    try:
        trade_rows = conn.execute(
            f"SELECT * FROM trades {where_sql} ORDER BY opened_at DESC", params
        ).fetchall()
        if not trade_rows:
            return []
        ids = [r["id"] for r in trade_rows]
        ph  = ",".join("?" * len(ids))
        leg_rows = conn.execute(
            f"SELECT * FROM legs WHERE trade_id IN ({ph}) ORDER BY id", ids
        ).fetchall()
    finally:
        conn.close()

    legs_by_id: dict = {}
    for lr in leg_rows:
        legs_by_id.setdefault(lr["trade_id"], []).append(_leg_row_to_dict(lr))

    return [
        _trade_row_to_dict(r, legs_by_id.get(r["id"], []))
        for r in trade_rows
    ]


# ── Public API — identical signatures to the old JSON version ─────────────────

def add_trade(trade: dict) -> None:
    """Persist a new trade record to the log."""
    with _DB_LOCK:
        _insert_trade_row(trade)


def update_trade(stock_code: str, updates: dict) -> None:
    """Update fields on the most recent open trade for stock_code."""
    with _DB_LOCK:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id, extra_json FROM trades "
                "WHERE stock_code=? AND status='open' ORDER BY opened_at DESC LIMIT 1",
                (stock_code,),
            ).fetchone()
            if row is None:
                return

            trade_id      = row["id"]
            existing_extra = json.loads(row["extra_json"] or "{}")

            legs_update: list | None = updates.get("legs")
            col_updates: dict = {}
            extra_updates: dict = {}
            for k, v in updates.items():
                if k == "legs":
                    continue
                elif k in _TRADE_COLUMNS:
                    col_updates[k] = v
                else:
                    extra_updates[k] = v

            if extra_updates:
                existing_extra.update(extra_updates)
                col_updates["extra_json"] = json.dumps(existing_extra, default=str)

            with conn:
                if col_updates:
                    set_clause = ", ".join(f"{k}=?" for k in col_updates)
                    conn.execute(
                        f"UPDATE trades SET {set_clause} WHERE id=?",
                        (*col_updates.values(), trade_id),
                    )
                if legs_update is not None:
                    conn.execute("DELETE FROM legs WHERE trade_id=?", (trade_id,))
                    _insert_legs(conn, trade_id, legs_update)
        finally:
            conn.close()


def close_trade(stock_code: str, close_reason: str, close_pnl: float) -> None:
    """Mark the open trade for stock_code as closed."""
    with _DB_LOCK:
        conn = _get_conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE trades "
                    "SET status='closed', close_reason=?, close_pnl=?, closed_at=? "
                    "WHERE id = ("
                    "  SELECT id FROM trades WHERE stock_code=? AND status='open'"
                    "  ORDER BY opened_at DESC LIMIT 1"
                    ")",
                    (close_reason, round(close_pnl, 2), datetime.now().isoformat(), stock_code),
                )
        finally:
            conn.close()
    if close_reason in _COOLDOWN_REASONS:
        _SESSION_COOLDOWN[stock_code] = close_reason


def is_in_session_cooldown(code: str) -> bool:
    """Return True if this stock was closed via trail_stop/cut_loss this session."""
    return code in _SESSION_COOLDOWN


def get_session_cooldown() -> dict[str, str]:
    """Return a snapshot of the current session cooldown map."""
    return dict(_SESSION_COOLDOWN)


def get_open_trades() -> list:
    return _fetch_trades("WHERE status='open'")


def get_all_trades() -> list:
    return _fetch_trades()


# ── Duplicate detection ────────────────────────────────────────────────────────

def has_open_trade(stock_code: str, trade_type: str | None = None) -> bool:
    sql: str     = "SELECT 1 FROM trades WHERE stock_code=? AND status='open'"
    params: list = [stock_code]
    if trade_type:
        sql += " AND trade_type=?"
        params.append(trade_type)
    sql += " LIMIT 1"
    conn = _get_conn()
    try:
        return conn.execute(sql, params).fetchone() is not None
    finally:
        conn.close()


def has_open_russell_trade() -> bool:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT 1 FROM trades WHERE scan_source='russell' AND status='open' LIMIT 1"
        ).fetchone() is not None
    finally:
        conn.close()


def has_open_politician_trade(stock_code: str) -> bool:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT 1 FROM trades WHERE stock_code=? AND scan_source='politician' "
            "AND status='open' LIMIT 1",
            (stock_code,),
        ).fetchone() is not None
    finally:
        conn.close()


def count_open_politician_trades() -> int:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE scan_source='politician' AND status='open'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_last_assigned_csp(stock_code: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trades "
            "WHERE stock_code=? AND strategy='Cash-Secured Put' "
            "AND close_reason='assigned' AND wheel_assignment_price IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 1",
            (stock_code,),
        ).fetchone()
        if row is None:
            return None
        leg_rows = conn.execute(
            "SELECT * FROM legs WHERE trade_id=? ORDER BY id", (row["id"],)
        ).fetchall()
    finally:
        conn.close()
    return _trade_row_to_dict(row, [_leg_row_to_dict(l) for l in leg_rows])


def print_open_trades() -> None:
    trades = get_open_trades()
    if not trades:
        print("  No open trades in log.")
        return

    options_trades = [t for t in trades if t.get("trade_type", "options") == "options"]
    stock_trades   = [t for t in trades if t.get("trade_type") == "stock"]

    if options_trades:
        print(f"\n  -- Options trades --")
        print(f"  {'Stock':<14} {'Strategy':<22} {'Net Credit':>11} {'TP Target':>10} {'CL Target':>10} {'Opened':<20}")
        print("  " + "-" * 95)
        for t in options_trades:
            print(
                f"  {t['stock_code']:<14} {t['strategy']:<22} "
                f"{t.get('net_credit_per_spread', 0):>11.4f} "
                f"{t.get('tp_value', 0):>10.2f} "
                f"{t.get('cl_value', 0):>10.2f} "
                f"{str(t.get('opened_at', ''))[:19]:<20}"
            )

    if stock_trades:
        print(f"\n  -- Stock trades --")
        print(f"  {'Stock':<14} {'Strategy':<18} {'Side':<6} {'Qty':>8} {'Limit':>9} {'Cost':>12} {'Opened':<20}")
        print("  " + "-" * 95)
        for t in stock_trades:
            print(
                f"  {t['stock_code']:<14} {t['strategy']:<18} "
                f"{t.get('side', ''):<6} "
                f"{t.get('qty', 0):>8,} "
                f"{t.get('limit_price', 0):>9.3f} "
                f"{t.get('cost', 0):>12,.2f} "
                f"{str(t.get('opened_at', ''))[:19]:<20}"
            )
