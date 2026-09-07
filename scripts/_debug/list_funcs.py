import akshare as ak
funcs = [f for f in dir(ak) if not f.startswith("_")]
import re
kw = ["delist", "indicator", "valuation", "industry", "sw_", "basic", "abandon", "suspend", "st_"]
for k in kw:
    hits = [f for f in funcs if k in f]
    print(f"=== {k} ({len(hits)}) ===")
    for f in hits[:25]:
        print("   ", f)
