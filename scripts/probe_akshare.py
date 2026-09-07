# -*- coding: utf-8 -*-
"""AKShare 接口可用性探测。

用途：环境搭建或 akshare 升级后，先跑这个确认关键接口还活着、字段名没变。
用法：python scripts/probe_akshare.py
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple

import akshare as ak


TESTS: List[Tuple[str, Dict[str, Any]]] = [
    ("当前A股列表",      "stock_info_a_code_name", {}),
    ("沪市列表",        "stock_info_sh_name_code", {}),
    ("深市列表",        "stock_info_sz_name_code", {}),
    ("北交所列表",      "stock_info_bj_name_code", {}),
    ("退市列表",        "stock_info_delist", {}),
    ("ST名单",         "stock_zh_a_st_em", {}),
    ("指数日线",        "index_zh_a_hist", {"symbol": "000300", "period": "daily",
                                        "start_date": "20230101", "end_date": "20230131"}),
    ("交易日历",        "tool_trade_date_hist_sina", {}),
    ("每日指标(估值)",  "stock_a_indicator_lg", {"symbol": "600000"}),
    ("实时行情快照",    "stock_zh_a_spot", {}),
    ("财务指标",        "stock_financial_analysis_indicator", {"symbol": "600000", "start_year": "2022"}),
    ("行业板块列表",    "stock_board_industry_name_em", {}),
    ("行业成分股",      "stock_board_industry_cons_em", {"symbol": "电子"}),
    ("申万行业分类",    "stock_board_industry_summary_ths", {}),
]

PRICE_TESTS: List[Tuple[str, Dict[str, Any]]] = [
    ("日线-前复权", "stock_zh_a_hist", {"symbol": "600000", "period": "daily",
                                    "start_date": "20230101", "end_date": "20230131", "adjust": "qfq"}),
]


def probe(name: str, func: str, kwargs: Dict[str, Any]) -> None:
    if not hasattr(ak, func):
        print(f"[缺失] {name:14s} {func}")
        return
    try:
        df = getattr(ak, func)(**kwargs)
        n = 0 if df is None else len(df)
        cols = list(df.columns)[:8] if n else []
        print(f"[正常] {name:14s} {func:38s} 行数={n:<7} 字段={cols}")
    except Exception as e:                                # noqa: BLE001
        print(f"[失败] {name:14s} {func:38s} {type(e).__name__}: {str(e)[:80]}")


def main() -> None:
    print(f"akshare 版本: {ak.__version__}\n" + "=" * 100)
    for name, func, kw in TESTS:
        probe(name, func, kw)
        sys.stdout.flush()
    print("=" * 100)
    for name, func, kw in PRICE_TESTS:
        probe(name, func, kw)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
