# -*- coding: utf-8 -*-
import sys, time
import akshare as ak

funcs = [f for f in dir(ak) if ("zh_a" in f and ("hist" in f or "daily" in f))]
print("=== A股日线候选接口 ===")
for f in funcs: print("   ", f)
print("=" * 60); sys.stdout.flush()

def probe(name, func, kw, retry=3):
    if not hasattr(ak, func):
        print(f"[缺失] {name:18s} {func}"); sys.stdout.flush(); return
    for i in range(retry):
        try:
            df = getattr(ak, func)(**kw)
            n = 0 if df is None else len(df)
            cols = list(df.columns)[:10] if n else []
            print(f"[正常] {name:18s} {func:32s} 行数={n:<7} 字段={cols}")
            sys.stdout.flush(); return
        except Exception as e:
            if i == retry - 1:
                print(f"[失败] {name:18s} {func:32s} {type(e).__name__}: {str(e)[:55]}")
                sys.stdout.flush()
            else:
                time.sleep(3)

probe("新浪日线-前复权", "stock_zh_a_daily", {"symbol": "sh600000", "adjust": "qfq"})
probe("新浪日线-后复权", "stock_zh_a_daily", {"symbol": "sh600000", "adjust": "hfq"})
probe("新浪日线-不复权", "stock_zh_a_daily", {"symbol": "sh600000", "adjust": ""})
probe("腾讯日线", "stock_zh_a_hist_tx", {"symbol": "sh600000", "start_date": "20230101", "end_date": "20230110", "adjust": "qfq"})
probe("东财日线(重测)", "stock_zh_a_hist", {"symbol": "600000", "period": "daily", "start_date": "20230101", "end_date": "20230110", "adjust": "qfq"})
