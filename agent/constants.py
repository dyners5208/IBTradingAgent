# ── Config override helper ────────────────────────────────────────────────────
# Every constant below reads its value from config.py first (if the name is
# defined there), then falls back to the hard-coded default shown here.
# To tune the agent, edit config.py only — no need to touch this file.

def _cfg(name, default):
    try:
        import config as _c
        return getattr(_c, name, default)
    except ImportError:
        return default


# ── Capital Allocation ─────────────────────────────────────────────────────────
US_ALLOCATION_PCT      = _cfg('US_ALLOCATION_PCT',      0.25)  # 35% of cash for US spreads
HK_ALLOCATION_PCT      = _cfg('HK_ALLOCATION_PCT',      0.50)  # 50% of cash for HK stocks
TOP_N_STOCKS           = _cfg('TOP_N_STOCKS',           5)     # Positions per market per session
SCORE_WEIGHT_STEEPNESS = _cfg('SCORE_WEIGHT_STEEPNESS', 3.0)   # Top trade gets 3× bottom budget

# ── Score Thresholds ──────────────────────────────────────────────────────────
# Minimum composite score [-1, +1] required before placing a trade.
# For directional strategies: abs(score) >= threshold.
# Iron Condor / Long Straddle are exempt — neutrality is their entry signal.

# Intra-day re-scan: re-qualify cached top-5 candidates on 30-min cycles
RESCAN_MIN_SCORE        = _cfg('RESCAN_MIN_SCORE',        0.10)

# Wheel Cash-Secured Put: must be bullish enough to accept assignment risk
CSP_MIN_COMPOSITE_SCORE = _cfg('CSP_MIN_COMPOSITE_SCORE', 0.10)

# Main US spread scan (S&P 500 + Nasdaq 100): directional trades only
# Applied as abs(composite_score) >= threshold; Iron Condor / Long Straddle exempt
US_SPREAD_MIN_SIGNAL    = _cfg('US_SPREAD_MIN_SIGNAL',    0.10)

# Russell 1000 opportunistic trade: stricter — mid-caps are riskier
# Applied as abs(composite_score) >= threshold
RUSSELL_MIN_SCORE       = _cfg('RUSSELL_MIN_SCORE',       0.25)

# ── Currency ──────────────────────────────────────────────────────────────────
# HK stock P&L is recorded in HKD. Divide by this rate to convert to USD for
# the consolidated P&L summary. HKMA pegs HKD at 7.75–7.85; 7.80 is a safe default.
HKD_USD_RATE = _cfg('HKD_USD_RATE', 7.80)  # HKD per 1 USD (used to convert HK P&L)

# ── Margin Safety ─────────────────────────────────────────────────────────────
MIN_MARGIN_BUFFER_PCT = _cfg('MIN_MARGIN_BUFFER_PCT', 0.30)  # Keep 30% buffer above maintenance
MAX_CONTRACTS         = _cfg('MAX_CONTRACTS',         10)    # Hard cap per spread leg

# ── Take Profit Targets ───────────────────────────────────────────────────────
# Credit spreads (Bull Put, Bear Call, Iron Condor, CSP, CC):
#   Close when remaining premium = TP_CREDIT_REMAINING × original credit
#   i.e. you have captured (1 - TP_CREDIT_REMAINING) of max profit
TP_CREDIT_REMAINING  = _cfg('TP_CREDIT_REMAINING', 0.20)   # Close at 80% profit captured

# Debit spreads (Bull Call, Bear Put):
#   Close when position value = TP_DEBIT_MULT × original debit paid
TP_DEBIT_MULT        = _cfg('TP_DEBIT_MULT',       1.50)   # Close at +50% gain on debit

# Long Straddle:
TP_STRADDLE_MULT     = _cfg('TP_STRADDLE_MULT',    1.50)   # Close at +50% gain on total debit

# ── Cut Loss Triggers ─────────────────────────────────────────────────────────
# Credit spreads: close when cost-to-close = CL_CREDIT_MULT × original credit
CL_CREDIT_MULT        = _cfg('CL_CREDIT_MULT',        2.00)  # Stop out at 100% of credit received

# Debit spreads: close when value has fallen to CL_DEBIT_REMAINING of original debit
CL_DEBIT_REMAINING    = _cfg('CL_DEBIT_REMAINING',    0.30)  # Stop out at 70% loss

# Long Straddle:
CL_STRADDLE_REMAINING = _cfg('CL_STRADDLE_REMAINING', 0.50)  # Stop out at 50% loss

# ── Theta Management ──────────────────────────────────────────────────────────
THETA_EXIT_DTE_DEBIT  = _cfg('THETA_EXIT_DTE_DEBIT',  21)  # Exit debit positions before this DTE
THETA_ROLL_DTE_CREDIT = _cfg('THETA_ROLL_DTE_CREDIT', 14)  # Evaluate rolling credit positions here

# ── Rolling Rules ─────────────────────────────────────────────────────────────
ROLL_DTE_TRIGGER   = _cfg('ROLL_DTE_TRIGGER',   14)   # Evaluate roll when DTE < this
ROLL_MIN_DTE       = _cfg('ROLL_MIN_DTE',        7)   # Must roll before DTE hits this
ROLL_MAX_DEBIT_PCT = _cfg('ROLL_MAX_DEBIT_PCT',  0.0)  # Only roll at breakeven or net credit

# ── Close Pricing Escalation ──────────────────────────────────────────────────
CLOSE_ESCALATE_ATTEMPTS = _cfg('CLOSE_ESCALATE_ATTEMPTS', 2)  # Use natural bid/ask after N failed cycles
CLOSE_NATURAL_DTE       = _cfg('CLOSE_NATURAL_DTE',       3)  # Always use natural pricing when DTE <= this

# ── Protection Retry Escalation ───────────────────────────────────────────────
PROT_ESCALATE_ATTEMPTS = _cfg('PROT_ESCALATE_ATTEMPTS', 2)   # Natural ask/bid after N failed retry cycles
PROT_URGENT_MINS       = _cfg('PROT_URGENT_MINS',       30)  # Natural pricing when < N min to market close

# ── Order Execution ───────────────────────────────────────────────────────────
ORDER_MID_NUDGE           = _cfg('ORDER_MID_NUDGE',           0.05)  # 5% nudge from mid toward bid/ask
ORDER_RETRY_SECONDS       = _cfg('ORDER_RETRY_SECONDS',       30)    # Seconds between fill-status polls
ORDER_TIMEOUT_MINS_SINGLE = _cfg('ORDER_TIMEOUT_MINS_SINGLE', 20)    # CSP/CC fill timeout (minutes)
ORDER_TIMEOUT_MINS_SPREAD = _cfg('ORDER_TIMEOUT_MINS_SPREAD', 10)    # Multi-leg spread fill timeout

# ── Stock Trade Targets ───────────────────────────────────────────────────────
STOCK_TP_PCT               = _cfg('STOCK_TP_PCT',               0.25)  # Safety ceiling TP at +25% (trailing stop is primary exit)
STOCK_CL_PCT               = _cfg('STOCK_CL_PCT',               0.10)  # Hard cut loss at -10% from entry
STOCK_TRAIL_ACTIVATION_PCT = _cfg('STOCK_TRAIL_ACTIVATION_PCT', 0.05)  # Begin trailing once up 5%
STOCK_TRAIL_PCT            = _cfg('STOCK_TRAIL_PCT',            0.08)  # Trail stop = 8% below rolling peak
STOCK_MIN_ENTRY_SCORE      = _cfg('STOCK_MIN_ENTRY_SCORE',      0.10)  # Min abs(composite_score) for HK stock buys/shorts

# ── Politician Copy-Trade Scanner ─────────────────────────────────────────────
POLITICIAN_ALLOCATION_PCT   = _cfg('POLITICIAN_ALLOCATION_PCT',   0.05)  # 5% of cash total budget
POLITICIAN_MAX_TRADES       = _cfg('POLITICIAN_MAX_TRADES',       3)     # Max new trades placed per scan cycle
POLITICIAN_MAX_ACTIVE       = _cfg('POLITICIAN_MAX_ACTIVE',       5)    # Hard cap: total open politician trades at any time
POLITICIAN_MIN_SCORE        = _cfg('POLITICIAN_MIN_SCORE',        0.30)  # Technical validation minimum
POLITICIAN_MIN_TRADE_COUNT  = _cfg('POLITICIAN_MIN_TRADE_COUNT',  5)     # Min trades to rank politician
POLITICIAN_TOP_N            = _cfg('POLITICIAN_TOP_N',            5)     # Top N politicians to pull trades from
POLITICIAN_MAX_RUN_PCT      = _cfg('POLITICIAN_MAX_RUN_PCT',      0.20)  # Skip if stock up >20% since transaction
POLITICIAN_MAX_DRAWDOWN_PCT = _cfg('POLITICIAN_MAX_DRAWDOWN_PCT', 0.25)  # Skip if stock down >25% since disclosure

# ── Gems Channel (AI-researched fundamental conviction trades) ─────────────────
GEM_US_ALLOCATION_PCT = _cfg('GEM_US_ALLOCATION_PCT', 0.05)  # 5% of US cash for gem positions
GEM_HK_ALLOCATION_PCT = _cfg('GEM_HK_ALLOCATION_PCT', 0.05)  # 5% of HK cash for gem positions
GEM_MAX_POSITIONS     = _cfg('GEM_MAX_POSITIONS',     3)     # Max concurrent gem trades (both markets)
GEM_MIN_SCORE         = _cfg('GEM_MIN_SCORE',         0.15)  # Min abs(composite_score) for gem entry

# ── Trade Category Toggles ────────────────────────────────────────────────────
# Set to False in config.py to disable a trade channel entirely.
TRADE_US_SPREADS_ENABLED = _cfg('TRADE_US_SPREADS_ENABLED', True)
TRADE_HK_ENABLED         = _cfg('TRADE_HK_ENABLED',         True)
TRADE_WHEEL_CSP_ENABLED  = _cfg('TRADE_WHEEL_CSP_ENABLED',  True)
TRADE_WHEEL_CC_ENABLED   = _cfg('TRADE_WHEEL_CC_ENABLED',   True)
TRADE_RUSSELL_ENABLED    = _cfg('TRADE_RUSSELL_ENABLED',    True)
TRADE_POLITICIAN_ENABLED = _cfg('TRADE_POLITICIAN_ENABLED', True)
TRADE_GEM_ENABLED        = _cfg('TRADE_GEM_ENABLED',        True)

# ── Wheel Strategy ────────────────────────────────────────────────────────────
WHEEL_CSP_TOTAL_BUDGET_PCT = _cfg('WHEEL_CSP_TOTAL_BUDGET_PCT', 0.45)  # Max 35% cash as CSP collateral
WHEEL_UNIVERSE = _cfg('WHEEL_UNIVERSE', ["MSFT", "GOOGL", "AAPL", "AMZN", "NVDA", "MA"])

# ── Russell 1000 Opportunistic Trade ─────────────────────────────────────────
RUSSELL_ALLOCATION_PCT = _cfg('RUSSELL_ALLOCATION_PCT', 0.05)  # 5% of cash per Russell trade

# ── Agent Loop ────────────────────────────────────────────────────────────────
MONITOR_INTERVAL_MINUTES = _cfg('MONITOR_INTERVAL_MINUTES', 15)  # Standalone monitor mode interval
AGENT_LOOP_INTERVAL_MINS = _cfg('AGENT_LOOP_INTERVAL_MINS', 30)  # Autonomous loop wake-up interval
OPEN_BUFFER_MINS         = _cfg('OPEN_BUFFER_MINS',         15)  # Minutes after open before scanning
TRADE_LOG_FILE           = _cfg('TRADE_LOG_FILE', "trade_log.json")

# ── Volatility Regime & Post-Mortem ──────────────────────────────────────────
# HIGH_IV_THRESHOLD is used in strategy selection (options_analyzer.py) and
# in post-mortem IV regime mismatch classification.
HIGH_IV_THRESHOLD         = _cfg('HIGH_IV_THRESHOLD',         30.0)  # IV% above which = high-vol regime
POSTMORTEM_FAST_LOSS_DAYS = _cfg('POSTMORTEM_FAST_LOSS_DAYS', 2)     # cut_loss within N days = BAD_ENTRY_TIMING
POSTMORTEM_PREMATURE_DAYS = _cfg('POSTMORTEM_PREMATURE_DAYS', 5)     # cut_loss within N days = PREMATURE_STOP

# ── Options Liquidity Filters ─────────────────────────────────────────────────
# Applied in select_spread_legs() before choosing contract strikes.
# Filters the chain so only liquid contracts are eligible for leg selection.
OPT_MAX_BID_ASK_SPREAD_PCT = _cfg('OPT_MAX_BID_ASK_SPREAD_PCT', 200)  # Max (ask-bid)/mid as %; OTM options naturally wider; real execution during mkt hours
OPT_MIN_OPEN_INTEREST      = _cfg('OPT_MIN_OPEN_INTEREST',       0)    # Alpaca SDK does not provide OI; keep 0 (disabled)

# ── Intraday Momentum Gate (re-scan only) ─────────────────────────────────────
# Applied in the 30-min re-scan cycle before retrying an unfilled position.
# Both RSI AND VWAP% must agree before the gate blocks an entry — requiring
# both signals prevents false negatives from short-lived intraday noise.
INTRADAY_GATE_MIN_BARS           = _cfg('INTRADAY_GATE_MIN_BARS',           6)     # Min 15-min bars needed (~1.5 h of data)
INTRADAY_GATE_RSI_PERIOD         = _cfg('INTRADAY_GATE_RSI_PERIOD',         9)     # RSI lookback on 15-min bars
INTRADAY_GATE_RSI_BULL_FLOOR     = _cfg('INTRADAY_GATE_RSI_BULL_FLOOR',     35)    # Below → strong intraday selling pressure
INTRADAY_GATE_RSI_BEAR_CEILING   = _cfg('INTRADAY_GATE_RSI_BEAR_CEILING',   65)    # Above → strong intraday buying pressure
INTRADAY_GATE_VWAP_PCT_THRESHOLD = _cfg('INTRADAY_GATE_VWAP_PCT_THRESHOLD', 1.5)   # % from intraday VWAP to confirm signal

# ── Intraday Momentum Overlay (live position monitoring) ──────────────────────
# Two-stage check every monitor cycle: Stage 1 uses the pre-fetched snap_map
# (zero extra API calls); Stage 2 fetches 15M klines only when Stage 1 triggers.
# This keeps extra API overhead near zero on quiet days (< 2% move).
INTRADAY_CHECK_ENABLED    = _cfg('INTRADAY_CHECK_ENABLED',    True)   # Master on/off switch
INTRADAY_MIN_MOVE_PCT     = _cfg('INTRADAY_MIN_MOVE_PCT',     2.0)    # Stage 1: underlying must move ≥ this % against thesis to fetch klines
INTRADAY_COOLDOWN_MINUTES = _cfg('INTRADAY_COOLDOWN_MINUTES', 60)     # Per-stock cooldown after Stage 1 triggers (prevents API hammering)

# ── IBKR Connection ────────────────────────────────────────────────────────────
IBKR_HOST             = _cfg('IBKR_HOST',             '127.0.0.1')
IBKR_PAPER_PORT       = _cfg('IBKR_PAPER_PORT',       7497)   # TWS paper port (live: 7496)
IBKR_LIVE_PORT        = _cfg('IBKR_LIVE_PORT',        7496)
IBKR_PAPER            = _cfg('IBKR_PAPER',            True)
IBKR_CLIENT_ID        = _cfg('IBKR_CLIENT_ID',        1)      # Unique per process
IBKR_TIMEOUT          = _cfg('IBKR_TIMEOUT',          10)     # Seconds to wait for TWS response
IBKR_MARKET_DATA_TYPE = _cfg('IBKR_MARKET_DATA_TYPE', 3)      # 1=live, 2=frozen, 3=delayed(free), 4=delayed-frozen

# Historical data pacing: IBKR limits ~50 requests per 10 min per client ID
IBKR_HIST_BATCH_SIZE    = _cfg('IBKR_HIST_BATCH_SIZE',    50)    # Requests before extra pause
IBKR_HIST_BATCH_SLEEP   = _cfg('IBKR_HIST_BATCH_SLEEP',   12)    # Seconds to pause between batches
IBKR_HIST_REQ_SLEEP     = _cfg('IBKR_HIST_REQ_SLEEP',     0.35)  # Seconds between individual requests

# Options chain pacing: per-contract reqTickers data timeout
IBKR_OPT_TICKER_TIMEOUT = _cfg('IBKR_OPT_TICKER_TIMEOUT', 5)    # Seconds to wait for ticker/greeks data
