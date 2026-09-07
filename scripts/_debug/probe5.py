# -*- coding: utf-8 -*-
import sys, time
import akshare as ak
print("=== 个股信息/行业相关接口 ===")
cands = [f for f in dir(ak) if not f.startswith("_") and ("individual" in f or ("industry" in f) or ("board_cons" in f))]
for f in cands: print("   ", f)
print("="*60); sys.stdout.flush()
def probe(name, func, kw, retry=3, show=6):
    if not hasattr(ak, func):
        print(f"[缺失] {name:20s} {func}"); sys.stdout.flush(); return
    for i in range(retry):
        try:
            df = getattr(ak, func)(**kw)
            n = 0 if df is None else len(df)
            print(f"[正常] {name:20s} {func:32s} 行数={n}")
            if n: print(df.head(show).to_string(index=False))
            sys.stdout.flush(); return
        except Exception as e:
            if i == retry-1:
                print(f"[失败] {name:20s} {func:32s} {type(e).__name__}: {str(e)[:55]}"); sys.stdout.flush()
            else: time.sleep(3)
probe("东财个股信息", "stock_individual_info_em", {"symbol": "600000"})
probe("东财A股快照", "stock_zh_a_spot_em", {})
probe("同花顺行业成分(备选)", "stock_board_cons_ths", {"symbol": "881121"})
