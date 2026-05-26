# IBTradingAgent — Claude Code Reference

Autonomous options + stock trading agent using Interactive Brokers (ib_insync / TWS).
Two markets: US (9:30–16:00 ET) and HK (9:30–12:00 + 13:00–16:00 HKT).

---

## File Map

| File | Purpose |
|------|---------|
| `trade_main.py` | Entry point; `phase_autonomous_loop`, `phase_scan_and_execute`, `phase_monitor_once`, `phase_monitor_loop`, `phase_status` |
| `agent/constants.py` | All tunable constants via `_cfg()` override pattern; every constant reads `config.py` first |
| `config.example.py` | Template for `config.py`; copy and edit to override any constant |
| `agent/trading_agent.py` | Full scan: fetch klines → score → rank → options enrichment → result dicts |
| `agent/scorer.py` | `score_stock(df)` → composite in [−1,+1]; `rank_universe(data, top_n)` → sorted DataFrame |
| `agent/data_fetcher.py` | `fetch_kline(code, days, ctx)`, `fetch_batch(codes, days, ctx)`, `get_stock_names(codes, ctx)` |
| `agent/universe.py` | `get_us_universe()` (S&P 500 + Nasdaq 100), `get_hk_universe()` (HSI); Wikipedia scrape with curated fallback |
| `agent/options_analyzer.py` | `get_options_data(code, ctx)` → chain/iv/exp_date; `select_strategy(direction_score, hv, iv)` |
| `agent/order_executor.py` | `select_spread_legs`, `compute_contracts`, `place_spread` (anchor-first), `place_stock_order`, `wait_for_fill`, `retry_protection_legs` |
| `agent/risk_manager.py` | `get_account_info(market)`, `compute_weighted_allocations`, `margin_safe_to_trade`, `print_account_summary` |
| `agent/position_manager.py` | Trade log I/O (`add_trade`, `update_trade`, `close_trade`, `get_open_trades`, `get_all_trades`); `has_open_trade`, `has_open_russell_trade`, `get_last_assigned_csp` |
| `agent/market_hours.py` | `is_us_market_open()`, `is_hk_market_open()`, `is_past_open_buffer(mkt, mins)`, `market_today(mkt)`, `next_scan_trigger`, `market_status_line` |
| `agent/trade_monitor.py` | `run_monitor(acct_us, acct_hk)` → evaluate all open trades, execute TP/CL/exit/roll; `evaluate_trade`, `close_spread`, `_run_thesis_check`, `_is_thesis_invalidated` |
| `agent/trade_roller.py` | `evaluate_roll(trade, ctx)`, `execute_roll(...)`, `run_roll_checks(open_trades, acct_us, acct_hk)` |
| `agent/wheel_strategy.py` | `run_wheel_scan(acct_us)` → CC on held shares (pass 1), CSP on bullish WHEEL_UNIVERSE stocks (pass 2) |
| `agent/russell_scanner.py` | `run_russell_scan(acct_us)` → 2-stage scan on ~600 S&P 600 small-cap names; 0-or-1 plan item; spreads only; 5% flat allocation |
| `agent/russell_universe.py` | `get_russell2000_universe()` (S&P 600 small-cap); `prefilter_russell(codes, ctx, target=25)` |
| `agent/alert_log.py` | `log_alert(level, message, context)` → alerts.json (max 10K, circular) |
| `agent/report.py` | `save_report(results, filename)` → Excel with US/HK summary sheets; `print_summary(results)` |
| `agent/daily_report.py` | `generate_daily_report(acct_us, acct_hk)` → 3-sheet Excel: Orders, Trade P&L, Open Trades |

---

## Data Flow

1. **Startup** — load `scan_state.json` (last-scanned date) + `daily_candidates.json` (top-5 for re-scans)
2. **Full scan** — once per market per trading day, after `OPEN_BUFFER_MINS` (15 min) past open
   - Universe fetch → klines → score all → rank top-5 → options chain → strategy selection
   - Wheel scan (WHEEL_UNIVERSE): CC on held shares, CSP on bullish stocks
   - Russell scan: pre-filter 900 → ~25 → score → pick 1 (independent 5% budget)
3. **Trade plan** — `_build_trade_plan()` → score-weighted budgets → `_execute_plan()` (concurrent)
   - Concurrent execution: `ThreadPoolExecutor`, one thread per trade
   - `place_spread()`: anchor legs first → wait fill → protection legs
4. **Intra-day re-scan** — every 30 min while market open, for cached top-5 with unfilled slots
   - Re-score those candidates; if still ≥ `RESCAN_MIN_SCORE`, retry execution
5. **Monitor** (`run_monitor`) — every 30 min, all open trades:
   - Fill status from moomoo order history (active + historical, back to open date)
   - Authoritative P&L from `position_list_query` (overrides bid/ask estimates)
   - External-close detection (not in position list → expired / assigned / manually closed)
   - CSP assignment: if stock appears in positions when CSP disappears → set `wheel_assignment_price` + `wheel_premium_accumulated`
   - Protection leg retry: re-place dead protection legs with fresh pricing each cycle
   - Daily thesis check: once per calendar day, re-score underlying; thesis broken → `thesis_exit` or `roll`
   - Actions: `take_profit`, `cut_loss`, `theta_exit`, `thesis_exit` → `close_spread`; `roll` → flag `_pending_action`
6. **Roll** (`run_roll_checks`) — after monitor, for trades flagged `_pending_action="roll"`:
   - Fetch next expiry, select legs, estimate net credit
   - Wheel CC: check new strike ≥ `net_cost_basis`; refuse if strike + new premium < basis
   - Roll or close; propagate `wheel_assignment_price` + `wheel_premium_accumulated`
7. **Reports** — merged daily scan report + end-of-day P&L report to `reports/`

---

## Trade Record Schema (trade_log.json)

```python
{
  # Core
  "stock_code": "US.AAPL",
  "strategy": "Bull Put Spread",   # see Strategy Reference below
  "market": "US",                  # "HK"
  "trade_type": "options",         # "stock"
  "status": "open",                # "closed"

  # Options fields
  "exp_date": "2025-03-21",
  "num_contracts": 2,
  "multiplier": 100,
  "net_credit_per_spread": 0.45,   # + = credit received, - = debit paid (per share)
  "tp_value": 360.0,               # P&L at TP threshold
  "cl_value": -900.0,              # P&L at CL threshold (negative)
  "placement_status": "complete",  # "partial", "pending_fill"
  "legs": [{
    "code": "US.AAPL-25031-P-185.0",
    "strike": 185.0,
    "call_or_put": "PUT",          # "CALL"
    "side": "SELL",                # "BUY"
    "delta": 0.30, "bid": 0.50, "ask": 0.60, "theta": -0.02, "iv": 0.18,
    "order_id": "12345", "limit_price": 0.55, "avg_fill_price": 0.54, "qty": 2
  }, ...],

  # Stock fields
  "side": "BUY",                   # "SELL"
  "qty": 100,
  "limit_price": 189.50,
  "cost": 18950.0,
  "order_id": "12346",

  # Lifecycle
  "opened_at": "2025-02-05T14:32:15.123456",
  "closed_at": "2025-02-10T15:45:22.654321",
  "close_reason": "take_profit",   # cut_loss | theta_exit | thesis_exit | roll | rolled
                                   # assigned | expired | manually_closed | cancelled
                                   # roll_declined | roll_declined_close
  "close_pnl": 360.0,

  # Monitor state (updated daily)
  "thesis_last_checked": "2025-02-05",   # ISO date; gates once-per-day re-scoring
  "thesis_score": 0.35,                  # latest composite_score from re-scoring

  # Wheel metadata (present only on wheel-origin trades)
  "wheel_type": "CSP",                   # "CC"
  "wheel_assignment_price": 185.0,       # CSP strike at assignment (per share)
  "wheel_premium_accumulated": 45.0,     # total $ premiums from all prior CSPs + CC rolls

  # Russell metadata (present only on Russell trades)
  "scan_source": "russell",             # enforces 1-trade limit in has_open_russell_trade()

  # Roll metadata
  "rolled_from": "2025-02-21",          # prior exp_date
  "roll_credit": 0.12,                  # net per-share credit/debit from roll transaction
}
```

---

## Plan Dict Schema (_execute_plan input)

```python
{
  "trade_type": "options",         # "stock"
  "stock_code": "US.AAPL",
  "strategy": "Bull Put Spread",
  "market": "US",
  "account": {account_info},       # from get_account_info()
  "alloc": {
    "per_trade": 1000.0,
    "total_budget": 5000.0,
    "currency": "USD",
    "weight_pct": 20.0,
  },
  # Options
  "exp_date": "2025-03-21",
  "legs": [...],                   # from select_spread_legs()
  "num_contracts": 2,
  "current_price": 189.50,
  "composite_score": 0.35,         # float, or label string for CC
  # Stock
  "side": "BUY", "qty": 100, "lot_size": 100,
  # Optional
  "wheel_type": "CSP",             # "CC"
  "wheel_assignment_price": 185.0, # CC only, from prior CSP assignment
  "wheel_premium_accumulated": 45.0,
  "scan_source": "russell",        # Russell trades only
}
```

---

## Strategy Reference

| Strategy | Type | Entry | Legs |
|----------|------|-------|------|
| Bull Put Spread | Credit | Bullish + high IV | SELL 0.30Δ put, BUY 0.15Δ put |
| Bear Call Spread | Credit | Bearish + high IV | SELL 0.30Δ call, BUY 0.15Δ call |
| Bull Call Spread | Debit | Bullish + low IV | BUY 0.50Δ call, SELL 0.25Δ call |
| Bear Put Spread | Debit | Bearish + low IV | BUY 0.50Δ put, SELL 0.25Δ put |
| Iron Condor | Credit | Neutral + high IV | SELL 0.25Δ call+put, BUY 0.10Δ call+put |
| Long Straddle | Debit | Neutral + low IV | BUY 0.50Δ call + 0.50Δ put (ATM) |
| Cash-Secured Put | Credit | Bullish (wheel) | SELL 0.30Δ put (~6% OTM); single leg |
| Covered Call | Credit | Neutral/bullish (wheel) | SELL 0.25Δ call (~6% OTM); single leg |
| Stock Buy | Stock | Bullish (HK or no options) | Market buy |
| Stock Sell Short | Stock | Bearish (HK or no options) | Market sell short |

**TP/CL targets by type:**
- Credit spreads: TP at 80% profit captured (`cost_to_close ≤ credit × 0.20`); CL at 2× credit loss
- Debit spreads: TP at +50% gain; CL at 70% loss (value ≤ debit × 0.30); theta-exit at DTE ≤ 21
- Straddle: TP at +50%; CL at 50% loss; theta-exit at DTE ≤ 21
- Stocks: TP at +10%; CL at −5%
- Roll trigger: DTE ≤ 14 for credit positions

---

## Score Thresholds

| Constant | Default | Used For |
|----------|---------|---------|
| `US_SPREAD_MIN_SIGNAL` | 0.10 | Directional US spreads; check `abs(score) ≥ threshold`; Iron Condor + Long Straddle exempt |
| `CSP_MIN_COMPOSITE_SCORE` | 0.10 | Wheel CSP eligibility (must be bullish) |
| `RUSSELL_MIN_SCORE` | 0.25 | Russell 1000 (stricter — mid-caps riskier) |
| `RESCAN_MIN_SCORE` | 0.10 | Intra-day re-scan retry |

Thesis invalidation (once per day, from `_is_thesis_invalidated()`):
- Bullish spreads: invalidated if `score ≤ −0.10`
- Bearish spreads: invalidated if `score ≥ +0.10`
- Iron Condor: invalidated if `abs(score) ≥ 0.25`
- CSP: invalidated if `score < CSP_MIN_COMPOSITE_SCORE`
- Covered Call: invalidated if `score < 0`
- Action for spreads/IC/stocks: `thesis_exit` (close immediately)
- Action for CSP/CC: `thesis_exit` if P&L ≥ 0; `roll` if P&L < 0 (avoid crystallising loss)

---

## Key Constants (all overridable in config.py)

```
Allocation:   US_ALLOCATION_PCT=0.35, HK_ALLOCATION_PCT=0.50
              WHEEL_CSP_TOTAL_BUDGET_PCT=0.35, RUSSELL_ALLOCATION_PCT=0.05
              TOP_N_STOCKS=5, SCORE_WEIGHT_STEEPNESS=3.0

Risk:         MIN_MARGIN_BUFFER_PCT=0.30, MAX_CONTRACTS=10

Rolling:      ROLL_DTE_TRIGGER=14, ROLL_MIN_DTE=7, ROLL_MAX_DEBIT_PCT=0.10
              THETA_EXIT_DTE_DEBIT=21, THETA_ROLL_DTE_CREDIT=14

Orders:       ORDER_MID_NUDGE=0.05, ORDER_RETRY_SECONDS=30
              ORDER_TIMEOUT_MINS_SINGLE=20, ORDER_TIMEOUT_MINS_SPREAD=10

Loops:        AGENT_LOOP_INTERVAL_MINS=30, MONITOR_INTERVAL_MINUTES=15
              OPEN_BUFFER_MINS=15
```

---

## On-Disk State Files

| File | Contents | Survives restart |
|------|----------|-----------------|
| `trade_log.json` | All trade records (open + closed). FileLock. Read fresh on every call — never cached in memory | ✓ always |
| `scan_state.json` | `{US: date, HK: date}` last-scanned date per market (local TZ) | ✓ always |
| `scan_results_cache.json` | `{date, results}` today's scored candidates for merged report | ✓ same UTC day |
| `daily_candidates.json` | `{date, candidates}` top-5 for intra-day re-scans | ✓ same UTC day |
| `russell2000_universe_cache.json` | `{date, universe}` ~600 S&P 600 tickers — skips Wikipedia scrape on restart | ✓ same calendar day |
| `alerts.json` | Circular buffer of critical/warning events (max 10K) | ✓ always |
| `reports/*.xlsx` | Scan reports and daily P&L (written, never read back) | ✓ always |

---

## Recurring Patterns

### Atomic JSON write (used everywhere)
```python
tmp = file_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, default=str)
os.replace(tmp, file_path)   # atomic on POSIX + Windows
```

### `_cfg()` override in constants.py
```python
def _cfg(name, default):
    try:
        import config as _c
        return getattr(_c, name, default)
    except ImportError:
        return default

MY_CONST = _cfg('MY_CONST', 0.35)   # config.MY_CONST wins; else default
```
Adding a new constant: add `_cfg()` line in `constants.py` AND commented example in `config.example.py`.

### FileLock on trade log
```python
with _LOG_LOCK:
    data = _load_log()
    # mutate
    _save_log(data)
```
`update_trade()` and `close_trade()` both search from the end (reversed) for the most recent open trade.

### Anchor-first leg placement (order_executor.py)
- Credit spreads (Bull Put, Bear Call, IC, CSP, CC): anchors = SELL legs
- Debit spreads (Bull Call, Bear Put): anchors = BUY legs
- Straddle: anchor = CALL leg
- Phase 1: place anchors → Phase 2: wait for fill confirmation → Phase 3: place protection
- Single-leg (CSP, CC): logged as `pending_fill` immediately; monitor tracks fill

### Wheel cost basis
```
net_cost_basis (per share) = wheel_assignment_price − (wheel_premium_accumulated / (num_contracts × 100))
```
Set at CSP assignment; carried through every CC roll. Roll refused if `new_strike + new_cc_premium < net_cost_basis`.

### Thesis check gate
```python
if trade.get("thesis_last_checked") == today:
    return None   # already checked today
```
`update_trade()` writes `thesis_last_checked` + `thesis_score` whether or not thesis is invalidated.

---

## Constraints & Design Decisions

- **Russell 1000**: exactly 1 active trade at a time (`scan_source="russell"` + `has_open_russell_trade()`)
- **Wheel CSP/CC**: CSP and CC are mutually exclusive per stock per session (if CC is placed, CSP skipped for that stock)
- **US spread filter**: `abs(composite_score) ≥ US_SPREAD_MIN_SIGNAL` for directional strategies; Iron Condor + Long Straddle exempt
- **HK market**: stocks only — no options trading supported
- **Score weights**: money_flow 60% + direction 40%; both clamped [−1, +1] before combination
- **Protection leg retry**: runs every monitor cycle (30 min) while market open, until protection fills or market closes
- **P&L authority**: when `pos_pnl_map` (from moomoo) is available, it overrides bid/ask-derived P&L
- **All config in config.py**: never edit `constants.py` defaults directly; always use `config.py` overrides
