import sys, os, io, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
tables = pd.read_html(io.StringIO(resp.text))

print(f"Total tables: {len(tables)}")
for i, df in enumerate(tables):
    print(f"\nTable {i}: shape={df.shape}")
    print(f"  Column types : {[type(c).__name__ for c in df.columns]}")
    print(f"  Columns      : {list(df.columns)}")
    # Check if any column contains 'ticker' case-insensitive
    for c in df.columns:
        if "ticker" in str(c).lower():
            print(f"  *** TICKER COL FOUND: repr={repr(c)}")
            print(f"  Sample values: {df[c].head(5).tolist()}")
