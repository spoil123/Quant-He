# -*- coding: utf-8 -*-
import sys, time
import akshare as ak

def probe(name, func, kw, retry=3):
    if not hasattr(ak, func):
        print(f"[缺失] {name:16s} {func}"); sys.stdout.flush(); return
    for i in range(retry):
        try:
            df = getattr(ak, func)(**kw)
            n = 0 if df is None else len(df)
            cols = list(df.columns)[:9] if n else []
            print(f"[正常] {name:16s} {func:36s} 行数={n:<7} 字段={cols}")
            sys.stdout.flush(); return
        except Exception as e:
            if i == retry - 1:
                print(f"[失败] {name:16s} {func:36s} {type(e).__name__}: {str(e)[:60]}")
                sys.stdout.flush()
            else:
                time.sleep(2)

probe("沪市退市", "stock_info_sh_delist", {})
probe("深市退市", "stock_info_sz_delist", {})
probe("百度估值", "stock_zh_valuation_baidu", {"symbol": "600000", "indicator": "总市值", "period": "近一年"})
probe("申万行业历史", "stock_industry_clf_hist_sw", {})
probe("巨潮行业分类", "stock_industry_category_cninfo", {"symbol": "600000"})
probe("ST名单", "stock_zh_a_st_em", {})
probe("指数日线", "index_zh_a_hist", {"symbol": "000300", "period": "daily", "start_date": "20230101", "end_date": "20230131"})
probe("日线不复权", "stock_zh_a_hist", {"symbol": "600000", "period": "daily", "start_date": "20230101", "end_date": "20230110", "adjust": ""})
probe("行业成分股", "stock_board_industry_cons_em", {"symbol": "电子"})
probe("实时快照", "stock_zh_a_spot", {})
