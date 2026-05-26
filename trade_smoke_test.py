import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.constants import US_ALLOCATION_PCT, MIN_MARGIN_BUFFER_PCT, TP_CREDIT_REMAINING
from agent.risk_manager import get_account_info, compute_allocation, margin_safe_to_trade
from agent.position_manager import has_open_trade, get_open_trades, print_open_trades
from agent.order_executor import select_spread_legs, compute_contracts
from agent.trade_monitor import evaluate_trade, _days_to_expiry
from agent.trade_roller import evaluate_roll

print("All trading module imports OK")

# Test constants
assert US_ALLOCATION_PCT == 0.25
assert MIN_MARGIN_BUFFER_PCT == 0.30
assert TP_CREDIT_REMAINING == 0.20
print("Constants OK")

# Test DTE helper
dte = _days_to_expiry("2099-01-01")
assert dte > 0
dte_past = _days_to_expiry("2020-01-01")
assert dte_past == 0
print("DTE helper OK")

# Test margin check
mock_account = {"avail_margin": 100_000, "maintenance_margin": 50_000}
ok, _ = margin_safe_to_trade(mock_account, 10_000)
assert ok, "Should be safe"
ok2, msg = margin_safe_to_trade(mock_account, 90_000)
assert not ok2, "Should fail margin check"
print("Margin safety check OK")

# Test position log
trades = get_open_trades()
print(f"Open trades in log: {len(trades)}")

# Test account info fetch
print("\nFetching US account info...")
acc = get_account_info()
if acc:
    alloc = compute_allocation(acc)
    print(f"  US cash: {acc['currency']} {acc['cash']:,.2f}")
    print(f"  US budget: {acc['currency']} {alloc['total_budget']:,.2f} per-trade={alloc['per_trade']:,.2f}")
else:
    print("  Could not fetch US account (check Alpaca API keys in config.py)")

print("\nAll smoke tests passed.")
