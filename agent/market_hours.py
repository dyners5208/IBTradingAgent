from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

_ET  = ZoneInfo("America/New_York")
_HKT = ZoneInfo("Asia/Hong_Kong")

# US: NYSE/NASDAQ regular session
_US_OPEN  = dtime(9, 30)
_US_CLOSE = dtime(16, 0)

# HK: HKEX morning + afternoon sessions
_HK_AM_OPEN  = dtime(9, 30)
_HK_AM_CLOSE = dtime(12, 0)
_HK_PM_OPEN  = dtime(13, 0)
_HK_PM_CLOSE = dtime(16, 0)

# NYSE/NASDAQ holidays (exchange-observed dates, not statutory dates)
_US_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1, 1),  date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19),
    date(2025, 7, 4),  date(2025, 9, 1),  date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),  date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3),  date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3),  date(2026, 9, 7),  date(2026, 11, 26),
    date(2026, 12, 25),
})

# HKEX public holidays
_HK_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1, 1),  date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 4, 4),  date(2025, 4, 18),
    date(2025, 4, 19), date(2025, 4, 21), date(2025, 5, 1),
    date(2025, 5, 5),  date(2025, 6, 2),  date(2025, 7, 1),
    date(2025, 9, 29), date(2025, 10, 2), date(2025, 10, 7),
    date(2025, 12, 25), date(2025, 12, 26),
    # 2026
    date(2026, 1, 1),  date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 4, 3),  date(2026, 4, 4),
    date(2026, 4, 6),  date(2026, 5, 1),  date(2026, 5, 25),
    date(2026, 6, 20), date(2026, 7, 1),  date(2026, 10, 1),
    date(2026, 10, 9), date(2026, 12, 25), date(2026, 12, 26),
})


def is_us_market_open() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    if now.date() in _US_HOLIDAYS:
        return False
    return _US_OPEN <= now.time() < _US_CLOSE


def is_hk_market_open() -> bool:
    now = datetime.now(_HKT)
    if now.weekday() >= 5:
        return False
    if now.date() in _HK_HOLIDAYS:
        return False
    t = now.time()
    return (_HK_AM_OPEN <= t < _HK_AM_CLOSE) or (_HK_PM_OPEN <= t < _HK_PM_CLOSE)


def is_hk_lunch_break() -> bool:
    """True during the HKEX lunch break (12:00–13:00) on weekdays."""
    now = datetime.now(_HKT)
    if now.weekday() >= 5:
        return False
    if now.date() in _HK_HOLIDAYS:
        return False
    return _HK_AM_CLOSE <= now.time() < _HK_PM_OPEN


def is_hk_trading_day() -> bool:
    """True while HK is in its trading day: AM session, lunch break, or PM session."""
    now = datetime.now(_HKT)
    if now.weekday() >= 5:
        return False
    if now.date() in _HK_HOLIDAYS:
        return False
    return _HK_AM_OPEN <= now.time() < _HK_PM_CLOSE   # 09:30 – 16:00


def minutes_until_close(market: str) -> float:
    """Minutes until market session close. Returns 999.0 when market is closed."""
    if market == "US":
        if not is_us_market_open():
            return 999.0
        now = datetime.now(_ET)
        close_dt = now.replace(hour=_US_CLOSE.hour, minute=_US_CLOSE.minute,
                               second=0, microsecond=0)
        return max(0.0, (close_dt - now).total_seconds() / 60)
    else:
        if not is_hk_market_open():
            return 999.0
        now = datetime.now(_HKT)
        close_dt = now.replace(hour=_HK_PM_CLOSE.hour, minute=_HK_PM_CLOSE.minute,
                               second=0, microsecond=0)
        return max(0.0, (close_dt - now).total_seconds() / 60)


def is_past_open_buffer(market: str, buffer_mins: int = 15) -> bool:
    """True if the market has been open for at least buffer_mins minutes in any session today."""
    tz  = _ET if market == "US" else _HKT
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    if market == "HK":
        # AM trigger: 9:30 + buffer
        am_trigger = now.replace(hour=9,  minute=30, second=0, microsecond=0) + timedelta(minutes=buffer_mins)
        # PM trigger: 13:00 + buffer
        pm_trigger = now.replace(hour=13, minute=0,  second=0, microsecond=0) + timedelta(minutes=buffer_mins)
        return now >= am_trigger or now >= pm_trigger
    open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return now >= open_dt + timedelta(minutes=buffer_mins)


def market_today(market: str) -> date:
    """Current calendar date in the market's local timezone."""
    tz = _ET if market == "US" else _HKT
    return datetime.now(tz).date()


def next_scan_trigger(market: str, buffer_mins: int = 15) -> datetime:
    """Return the next future datetime when a scan should fire for this market.

    Always returns a timezone-aware datetime in the market's local timezone.
    HK has two sessions: tries AM trigger (09:30+buffer) then PM trigger (13:00+buffer)
    before falling through to next weekday.
    """
    tz = _ET if market == "US" else _HKT
    now = datetime.now(tz)

    if market == "HK" and now.weekday() < 5:
        am_trigger = now.replace(hour=9,  minute=30, second=0, microsecond=0) + timedelta(minutes=buffer_mins)
        pm_trigger = now.replace(hour=13, minute=0,  second=0, microsecond=0) + timedelta(minutes=buffer_mins)
        for t in (am_trigger, pm_trigger):
            if t > now:
                return t
        # Both today's triggers are past — fall through to next weekday below
    elif market == "US":
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        trigger    = open_today + timedelta(minutes=buffer_mins)
        if trigger > now and now.weekday() < 5:
            return trigger

    # Advance to next weekday's 09:30 + buffer
    candidate = now + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.replace(hour=9, minute=30, second=0, microsecond=0) \
           + timedelta(minutes=buffer_mins)


def market_status_line(market: str) -> str:
    """Return a human-readable status string for display."""
    if market == "US":
        now = datetime.now(_ET)
        status = "OPEN" if is_us_market_open() else "CLOSED"
        return f"US market  {status}  ({now.strftime('%a %H:%M ET')})"
    else:
        now = datetime.now(_HKT)
        if is_hk_market_open():
            status = "OPEN"
        elif is_hk_lunch_break():
            status = "LUNCH (reopens 13:00)"
        else:
            status = "CLOSED"
        return f"HK market  {status}  ({now.strftime('%a %H:%M HKT')})"
