# -*- coding: utf-8 -*-
import sys, time
import akshare as ak
def probe(name, func, kw, retry=3, show=3):
    if not hasattr(ak, func):
        print(f"[缺失] {name:20s} {func}"); sys.stdout.flush(); return
    for i in range(retry):
        try:
            df = getattr(ak, func)(**kw)
            n = 0 if df is None else len(df)
            print(f"[正常] {name:20s} {func:34s} 行数={n:<6} 字段={list(df.columns)[:8] if n else []}")
            if n: print(df.head(show).to_string(index=False))
            sys.stdout.flush(); return
        except Exception as e:
            if i == retry-1:
                print(f"[失败] {name:20s} {func:34s} {type(e).__name__}: {str(e)[:60]}"); sys.stdout.flush()
            else: time.sleep(3)

probe("东财行业列表", "stock_board_industry_name_em", {})
probe("东财行业成分", "stock_board_industry_cons_em", {"symbol": "电子"})
probe("同花顺行业列表", "stock_board_industry_name_ths", {})
probe("同花顺行业成分", "stock_board_industry_cons_ths", {"symbol": "电子"})
probe("巨潮行业分类", "stock_industry_category_cninfo", {"symbol": "600000"})
probe("申万一级成分", "sw_index_third_cons", {"symbol": "801010"})
