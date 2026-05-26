"""
IBKR connection singleton — ib_insync wrapper.

Thread-safety contract:
    Every call to ib_insync MUST be made while holding ibkr_lock.
    Pattern:
        with ibkr_lock:
            ib = get_ib()
            result = ib.someMethod(...)
            ib.sleep(N)   # ib.sleep also counts — keep inside the lock

ibkr_lock is a re-entrant lock (RLock) so composed helper functions that each
call get_ib() can safely acquire the lock without deadlocking.
"""

from __future__ import annotations
import logging
import threading
import time

ibkr_lock: threading.RLock = threading.RLock()

# ── Suppress known non-fatal IBKR informational messages ──────────────────────
# Error 10197 fires in paper trading when another TWS session holds the same
# market data subscription — data still arrives, the warning is just noise.
# cancelMktData "No reqId found" fires when a subscription never started
# (due to 10197) and we try to clean it up.
_IBKR_SOFT_CODES = frozenset({
    10197,  # No market data during competing live session
    2104,   # Market data farm connection OK
    2106,   # HMDS data farm connection OK
    2119,   # Market data farm connecting
    2158,   # Sec-def data farm connection OK
})

class _IBSoftFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "cancelMktData: No reqId found" in msg:
            return False
        for code in _IBKR_SOFT_CODES:
            if f"Error {code}" in msg or f"error {code}" in msg:
                return False
        return True

_ib_soft_filter = _IBSoftFilter()
for _ln in ("ib_insync", "ib_insync.ib", "ib_insync.client", "ib_insync.wrapper"):
    logging.getLogger(_ln).addFilter(_ib_soft_filter)
del _ln

_ib = None
_connected: bool = False


def _get_port() -> int:
    from agent.constants import IBKR_PAPER, IBKR_PAPER_PORT, IBKR_LIVE_PORT
    return IBKR_PAPER_PORT if IBKR_PAPER else IBKR_LIVE_PORT


def get_ib():
    """Return the connected IB singleton. Auto-connects on first call.
    MUST be called while holding ibkr_lock."""
    global _ib, _connected
    if _ib is not None and _connected and _ib.isConnected():
        return _ib
    _connect()
    return _ib


def _connect() -> None:
    global _ib, _connected
    from ib_insync import IB, util
    from agent.constants import (
        IBKR_HOST, IBKR_CLIENT_ID, IBKR_TIMEOUT, IBKR_MARKET_DATA_TYPE, IBKR_PAPER,
    )

    util.patchAsyncio()

    if _ib is None:
        _ib = IB()

    port = _get_port()
    mode = "PAPER" if IBKR_PAPER else "LIVE"

    for attempt in range(3):
        try:
            if _ib.isConnected():
                _ib.disconnect()
            _ib.connect(IBKR_HOST, port, clientId=IBKR_CLIENT_ID, timeout=IBKR_TIMEOUT)
            _ib.reqMarketDataType(IBKR_MARKET_DATA_TYPE)
            _connected = True
            print(f"  [IBKR] Connected to TWS on {IBKR_HOST}:{port} ({mode})")
            return
        except Exception as exc:
            print(f"  [IBKR] Connection attempt {attempt + 1}/3 failed: {exc}")
            if attempt < 2:
                time.sleep(3)

    _connected = False
    raise ConnectionError(
        f"Cannot connect to TWS on {IBKR_HOST}:{port} after 3 attempts. "
        "Ensure TWS is running with API connections enabled (Edit → Global Config → API → Settings)."
    )


def ensure_connected() -> bool:
    """Check connection health; reconnect if needed. Returns True if connected."""
    global _connected
    with ibkr_lock:
        try:
            if _ib is not None and _ib.isConnected():
                return True
            _connect()
            return True
        except Exception as exc:
            print(f"  [IBKR] ensure_connected failed: {exc}")
            _connected = False
            return False


def disconnect_ib() -> None:
    """Graceful disconnect. Call at agent shutdown."""
    global _ib, _connected
    with ibkr_lock:
        if _ib is not None and _ib.isConnected():
            _ib.disconnect()
        _connected = False
    print("  [IBKR] Disconnected.")


def is_paper() -> bool:
    from agent.constants import IBKR_PAPER
    return bool(IBKR_PAPER)


def reset_client() -> None:
    """Force re-creation of the IB instance (useful in tests or after config change)."""
    global _ib, _connected
    with ibkr_lock:
        if _ib is not None and _ib.isConnected():
            _ib.disconnect()
        _ib = None
        _connected = False
