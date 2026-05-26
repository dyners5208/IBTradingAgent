# ══════════════════════════════════════════════════════════════════════════════
#  IBTradingAgent — User Configuration
#  Copy this file to config.py and fill in your settings.
#  config.py is in .gitignore and will NEVER be committed to git.
# ══════════════════════════════════════════════════════════════════════════════

# ── IBKR Connection ────────────────────────────────────────────────────────────
# Interactive Brokers Trader Workstation (TWS) must be running with API enabled.
#
# TWS setup (one-time):
#   1. TWS → Edit → Global Configuration → API → Settings
#   2. Check "Enable ActiveX and Socket Clients"
#   3. Socket Port: 7497 (paper) or 7496 (live)
#   4. Uncheck "Read-Only API"
#   5. Check "Allow connections from localhost only"
#   6. Confirm 127.0.0.1 is in Trusted IP Addresses
#   7. Restart TWS

IBKR_PAPER     = True        # Set False for live trading (real money!)
IBKR_HOST      = '127.0.0.1'
IBKR_CLIENT_ID = 1           # Unique integer per running agent process

# Market data type: 1=live (requires subscription), 3=delayed/free (default)
# IBKR_MARKET_DATA_TYPE = 3

# ── Anthropic API (post-mortem analysis & gem research) ───────────────────────
# Required for Claude-powered trade analysis and AI gem research.
# Get your key from: https://console.anthropic.com → API Keys
# ANTHROPIC_API_KEY = "sk-ant-..."

# ── Wheel Strategy Watchlist ──────────────────────────────────────────────────
# Stocks you are comfortable owning at a discount (Cash-Secured Put) or
# holding long while collecting call premium (Covered Call).
WHEEL_UNIVERSE = [
    "MSFT",
    "GOOGL",
    "AAPL",
    "AMZN",
    "NVDA",
    "MA",
]

# ══════════════════════════════════════════════════════════════════════════════
#  Trading Parameters
#  Uncomment and edit any value to override the agent default in constants.py.
# ══════════════════════════════════════════════════════════════════════════════

# ── Capital Allocation ────────────────────────────────────────────────────────
# US_ALLOCATION_PCT          = 0.35   # 35% of cash for US spread trades
# WHEEL_CSP_TOTAL_BUDGET_PCT = 0.35   # Max 35% of cash as wheel CSP collateral
# RUSSELL_ALLOCATION_PCT     = 0.05   # 5% of cash per Russell trade (flat)
# TOP_N_STOCKS               = 5      # Max positions per session
# SCORE_WEIGHT_STEEPNESS     = 3.0    # Top-ranked gets 3× budget of bottom-ranked

# ── Score Thresholds ──────────────────────────────────────────────────────────
# US_SPREAD_MIN_SIGNAL    = 0.10  # Directional US spreads
# CSP_MIN_COMPOSITE_SCORE = 0.10  # Wheel Cash-Secured Put
# RUSSELL_MIN_SCORE       = 0.25  # Russell opportunistic trade
# RESCAN_MIN_SCORE        = 0.10  # Intra-day re-scan retry threshold

# ── Take Profit Targets ───────────────────────────────────────────────────────
# TP_CREDIT_REMAINING  = 0.20   # Close credit spreads when 80% of premium decayed
# TP_DEBIT_MULT        = 1.50   # Close debit spreads at +50% gain
# TP_STRADDLE_MULT     = 1.50   # Close straddle at +50% gain

# ── Cut Loss Triggers ─────────────────────────────────────────────────────────
# CL_CREDIT_MULT        = 2.00  # Stop out at 2× credit received as loss
# CL_DEBIT_REMAINING    = 0.30  # Stop out at 70% loss on debit paid
# CL_STRADDLE_REMAINING = 0.50  # Stop out at 50% loss on straddle debit

# ── Theta & Rolling ───────────────────────────────────────────────────────────
# THETA_EXIT_DTE_DEBIT  = 21   # Exit debit positions when DTE < this
# THETA_ROLL_DTE_CREDIT = 14   # Roll evaluation trigger for credit positions
# ROLL_DTE_TRIGGER      = 14   # Begin roll evaluation when DTE < this
# ROLL_MIN_DTE          = 7    # Complete roll before DTE reaches this
# ROLL_MAX_DEBIT_PCT    = 0.0  # Only roll at breakeven or net credit

# ── Risk / Margin ─────────────────────────────────────────────────────────────
# MIN_MARGIN_BUFFER_PCT = 0.30  # Keep buying_power >= maintenance_margin × 1.30
# MAX_CONTRACTS         = 10   # Hard cap on contracts per spread

# ── Stock Trade Targets ───────────────────────────────────────────────────────
# STOCK_TP_PCT               = 0.25
# STOCK_CL_PCT               = 0.10
# STOCK_TRAIL_ACTIVATION_PCT = 0.05
# STOCK_TRAIL_PCT            = 0.08

# ── Politician Copy-Trade Scanner ─────────────────────────────────────────────
# POLITICIAN_ALLOCATION_PCT   = 0.05
# POLITICIAN_MAX_TRADES       = 3
# POLITICIAN_MAX_ACTIVE       = 10
# POLITICIAN_MIN_SCORE        = 0.30

# ── Order Execution ───────────────────────────────────────────────────────────
# ORDER_MID_NUDGE           = 0.05
# ORDER_RETRY_SECONDS       = 30
# ORDER_TIMEOUT_MINS_SINGLE = 20
# ORDER_TIMEOUT_MINS_SPREAD = 10

# ── Agent Loop Intervals ──────────────────────────────────────────────────────
# AGENT_LOOP_INTERVAL_MINS  = 30
# MONITOR_INTERVAL_MINUTES  = 15
# OPEN_BUFFER_MINS          = 15

# ── IBKR Historical Data Pacing ───────────────────────────────────────────────
# Uncomment to tune if you hit "pacing violation" errors during universe scan:
# IBKR_HIST_REQ_SLEEP    = 0.35   # Seconds between individual bar requests
# IBKR_HIST_BATCH_SIZE   = 50     # Requests before an extra pause
# IBKR_HIST_BATCH_SLEEP  = 12     # Seconds extra pause between batches

# ── Trade Channel Toggles ─────────────────────────────────────────────────────
# TRADE_US_SPREADS_ENABLED = True   # US options spreads (S&P 500 / Nasdaq 100)
# TRADE_WHEEL_CSP_ENABLED  = True   # Wheel Cash-Secured Puts
# TRADE_WHEEL_CC_ENABLED   = True   # Wheel Covered Calls
# TRADE_RUSSELL_ENABLED    = True   # Russell 2000 opportunistic spread
# TRADE_POLITICIAN_ENABLED = True   # Politician copy-trade scanner (STOCK Act)
# TRADE_GEM_ENABLED        = True   # Gems channel (AI-researched conviction trades)
