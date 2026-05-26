import sys
import os

# Ensure project root is on path when run directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.trading_agent import run_agent
from agent.report import save_report, print_summary


def main() -> None:
    results = run_agent(top_n=5)

    if not results:
        print("\nNo results to report.")
        return

    print_summary(results)
    save_report(results)


if __name__ == "__main__":
    main()
