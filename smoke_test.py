import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.universe import US_UNIVERSE, HK_UNIVERSE
from agent.indicators import calc_mfi, calc_cmf, calc_obv_slope, calc_vwap_pct, calc_rsi, calc_macd, calc_hv
from agent.scorer import score_stock, rank_universe
from agent.options_analyzer import select_strategy
from agent.report import save_report, print_summary

print("All imports OK")
print(f"US universe: {len(US_UNIVERSE)} stocks")
print(f"HK universe: {len(HK_UNIVERSE)} stocks")

s1 = select_strategy(0.4, 25.0, None)
print(f"Strategy (bullish, low IV)  : {s1['strategy']}")

s2 = select_strategy(-0.3, 35.0, None)
print(f"Strategy (bearish, high IV) : {s2['strategy']}")

s3 = select_strategy(0.1, 40.0, None)
print(f"Strategy (neutral, high IV) : {s3['strategy']}")

print("Smoke test passed.")
