"""
One-shot migration: trade_log.json → trade_log.db

Run once before switching the agent to the new SQLite-backed position_manager:

    python -m agent.migrate_to_sqlite

The JSON file is left in place as a read-only backup.
Subsequent agent runs will use trade_log.db exclusively.
"""

import json
import os
import sys

# Allow running as a module from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.position_manager import DB_PATH, init_db, _insert_trade_row   # noqa: E402


def migrate() -> None:
    json_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "trade_log.json")

    if not os.path.exists(json_path):
        print("trade_log.json not found — nothing to migrate.")
        return

    with open(json_path, "r") as f:
        data = json.load(f)

    trades = data.get("trades", [])
    if not trades:
        print("trade_log.json is empty — nothing to migrate.")
        return

    print(f"Migrating {len(trades)} trade(s) from trade_log.json to {DB_PATH}")
    init_db()

    ok = 0
    errors = 0
    for i, trade in enumerate(trades):
        try:
            _insert_trade_row(trade)
            ok += 1
        except Exception as exc:
            errors += 1
            print(f"  [ERROR] trade #{i} ({trade.get('stock_code', '?')}): {exc}")

    print(f"\nDone: {ok} migrated, {errors} error(s).")
    if errors == 0:
        print("trade_log.json kept as read-only backup.")
        print("Rename it to trade_log.json.bak to confirm the switch is complete.")
    else:
        print("Fix errors above and re-run before using the new DB.")


if __name__ == "__main__":
    migrate()
