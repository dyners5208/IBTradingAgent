"""Quick connectivity test — run this after filling in config.py."""

from agent.ibkr_client import get_ib, ibkr_lock, is_paper, disconnect_ib


def main():
    print("Testing IBKR connection via TWS...")
    try:
        with ibkr_lock:
            ib   = get_ib()
            vals = ib.accountValues()

        def _tag(tag, currency="USD"):
            for v in vals:
                if v.tag == tag and v.currency == currency:
                    try:
                        return float(v.value)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        cash = _tag("CashBalance")
        bp   = _tag("BuyingPower")
        nlv  = _tag("NetLiquidation")
        acc  = next((v.account for v in vals if v.account), "?")
        mode = "Paper" if is_paper() else "LIVE"

        print(f"[IBKR] Connected — Account={acc}  ({mode})")
        print(f"[IBKR] Cash=${cash:,.2f}  BuyingPower=${bp:,.2f}  NLV=${nlv:,.2f}")

        # Verify positions can be read
        with ibkr_lock:
            positions = ib.positions()
        print(f"[IBKR] Open positions: {len(positions)}")
        for p in positions[:5]:
            con = p.contract
            print(f"  {con.symbol} ({con.secType})  qty={p.position}  avgCost={p.avgCost:.4f}")
        if len(positions) > 5:
            print(f"  ... and {len(positions) - 5} more")

        print("\n[IBKR] Connection test PASSED.")

    except Exception as exc:
        print(f"[IBKR] Connection FAILED: {exc}")
        print("  Check that TWS is running and API connections are enabled.")
        print("  TWS → Edit → Global Configuration → API → Settings")
        raise
    finally:
        disconnect_ib()


if __name__ == "__main__":
    main()
