# -*- coding: utf-8 -*-
"""
第 1 层数据层自检（不需要数据库连接）。

用途：
    在首次全量抓取之前，先花 1~2 分钟确认「数据源能通、清洗逻辑正确、
    时点对齐无误」。全量抓取要跑很久，先自检能省下大量返工时间。

检查项：
    1. 配置文件能加载
    2. 股票列表接口（含退市股）
    3. 日线接口（前/后/不复权 + 流通市值派生）
    4. 财务接口（含 TTM 与公告可用日）
    5. 申万行业分类（含历史变动）
    6. 价格清洗 / 财务清洗
    7. PIT 时点对齐（验证不会用到未来数据）

用法：
    python scripts/check_data_layer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src.common.config import PROJECT_ROOT as ROOT  # noqa: E402
from src.common.logger import logger  # noqa: E402

OK, FAIL, WARN = "[通过]", "[失败]", "[警告]"
_results = []


def _rec(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    print(f"{status} {name:28s} {detail}")


def check_config() -> None:
    try:
        from src.common.config import get_config
        for f in ("database", "universe", "costs", "factors", "strategy", "risk"):
            get_config(f)
        _rec("配置文件加载", OK, "6 个 YAML 全部解析通过")
    except FileNotFoundError as e:
        _rec("配置文件加载", FAIL, str(e))
    except ValueError as e:
        # DB_PASSWORD 未提供属于预期情况（尚未配置数据库）
        _rec("配置文件加载", WARN, f"{str(e)[:60]}（属正常，未配置 .env 密码）")


def check_stock_list() -> None:
    try:
        from src.layer1_data.fetcher import stock_list as sl
        uni = sl.build_universe()
        if uni.empty:
            _rec("股票列表", FAIL, "返回为空")
            return
        n_delist = int(uni["is_delisted"].sum())
        _rec("股票列表", OK,
             f"共 {len(uni)} 只（在市 {len(uni) - n_delist}，退市 {n_delist}，"
             f"ST {int(uni['is_st'].sum())}）")
    except Exception as e:                                # noqa: BLE001
        _rec("股票列表", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_daily_price() -> None:
    try:
        from src.layer1_data.fetcher import daily_price as dp
        price, basic = dp.fetch_stock_full("600000", "2023-01-01", "2023-06-30")
        if price.empty:
            _rec("日线抓取", FAIL, "返回为空")
            return
        adjs = sorted(price["adj_type"].unique().tolist())
        mv = float(basic["float_mv"].iloc[-1]) if not basic.empty else 0.0
        _rec("日线抓取", OK,
             f"{len(price)} 行，复权类型 {adjs}，最新流通市值 {mv / 1e8:.0f} 亿元")
    except Exception as e:                                # noqa: BLE001
        _rec("日线抓取", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_financial() -> None:
    try:
        from src.layer1_data.fetcher import financial as fin
        df = fin.fetch_financial("600000", start_year="2022")
        if df.empty:
            _rec("财务抓取", FAIL, "返回为空")
            return
        has_ttm = df["eps_ttm"].notna().sum()
        _rec("财务抓取", OK,
             f"{len(df)} 个报告期，eps_ttm 有效 {has_ttm} 期，"
             f"公告可用日已补齐 {int(df['ann_date'].notna().sum())} 期")
    except Exception as e:                                # noqa: BLE001
        _rec("财务抓取", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_industry() -> None:
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from src.layer1_data.fetcher import industry as ind
        df = ind.fetch_sw_classification()
        if df.empty:
            _rec("行业分类", FAIL, "返回为空")
            return
        _rec("行业分类", OK,
             f"{len(df)} 行，{df['ts_code'].nunique()} 只股票，"
             f"L1 分组 {df['industry_code'].nunique()} 个")
    except Exception as e:                                # noqa: BLE001
        _rec("行业分类", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_index_and_calendar() -> None:
    try:
        from src.layer1_data.fetcher import daily_price as dp
        idx = dp.fetch_index_daily("000300", "2023-01-01", "2023-03-31")
        _rec("指数日线", OK if not idx.empty else FAIL, f"{len(idx)} 行")
    except Exception as e:                                # noqa: BLE001
        _rec("指数日线", FAIL, f"{type(e).__name__}: {str(e)[:70]}")

    try:
        from src.layer1_data.fetcher import daily_price as dp
        cal = dp.fetch_trade_calendar()
        _rec("交易日历", OK if not cal.empty else FAIL, f"{len(cal)} 个交易日")
    except Exception as e:                                # noqa: BLE001
        _rec("交易日历", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_cleaners() -> None:
    try:
        from src.layer1_data.fetcher import daily_price as dp
        from src.layer1_data.cleaner.price_cleaner import clean_price, validate_price
        price, _ = dp.fetch_stock_full("600000", "2023-01-01", "2023-06-30")
        cleaned, rep = clean_price(price, name="浦发银行")
        bad = validate_price(cleaned)
        _rec("价格清洗", OK,
             f"输入 {rep['input_rows']} -> 输出 {rep['output_rows']}，"
             f"剔除 {rep['dropped']}，可疑行 {len(bad)}")
    except Exception as e:                                # noqa: BLE001
        _rec("价格清洗", FAIL, f"{type(e).__name__}: {str(e)[:70]}")

    try:
        from src.layer1_data.fetcher import financial as fin
        from src.layer1_data.cleaner.financial_cleaner import clean_financial
        raw = fin.fetch_financial("600000", start_year="2022")
        cleaned, rep = clean_financial(raw)
        _rec("财务清洗", OK,
             f"输入 {rep['input_rows']} -> 输出 {rep['output_rows']}，标记 {rep['notes'] or '无'}")
    except Exception as e:                                # noqa: BLE001
        _rec("财务清洗", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def check_pit_alignment() -> None:
    """验证时点对齐：T 日只能用 T 日之前已公布的数据。"""
    try:
        from src.common.pit import add_ann_date, build_pit_panel, shift_to_trade_date
        fin_df = pd.DataFrame({
            "ts_code": ["TEST"] * 4,
            "report_date": pd.to_datetime(
                ["2022-12-31", "2023-03-31", "2023-06-30", "2023-09-30"]).date,
            "eps": [1.0, 0.3, 0.65, 0.95],
        })
        fin_df = add_ann_date(fin_df, strategy="conservative")
        cal = pd.bdate_range("2023-04-20", "2023-11-10").date
        fin_df = shift_to_trade_date(fin_df, cal, lag_days=1)
        panel = build_pit_panel(fin_df, cal, value_cols=["eps"])

        def _v(d: str):
            r = panel[panel["trade_date"] == pd.Timestamp(d).date()]
            return None if r.empty else r["eps"].iloc[0]

        v_0428 = _v("2023-04-28")     # 年报可用日 2023-05-04 之前 -> 应为 NaN
        v_0505 = _v("2023-05-05")     # Q1 已可用
        v_0901 = _v("2023-09-01")     # 半年报已可用
        v_1101 = _v("2023-11-01")     # Q3 已可用

        ok = (pd.isna(v_0428) and v_0505 == 0.3
              and v_0901 == 0.65 and v_1101 == 0.95)
        _rec("PIT 时点对齐", OK if ok else FAIL,
             f"4/28={v_0428}  5/05={v_0505}  9/01={v_0901}  11/01={v_1101}")
        if not ok:
            print("      ^ 应分别为 NaN / 0.3 / 0.65 / 0.95，"
                  "4/28 必须看不到一季报，否则存在前视偏差")
    except Exception as e:                                # noqa: BLE001
        _rec("PIT 时点对齐", FAIL, f"{type(e).__name__}: {str(e)[:70]}")


def main() -> int:
    print("=" * 78)
    print("第 1 层数据层自检")
    print(f"项目根目录: {ROOT}")
    print("=" * 78)

    check_config()
    check_stock_list()
    check_daily_price()
    check_index_and_calendar()
    check_financial()
    check_industry()
    check_cleaners()
    check_pit_alignment()

    print("=" * 78)
    n_ok = sum(1 for _, s, _ in _results if s == OK)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    n_warn = sum(1 for _, s, _ in _results if s == WARN)
    print(f"汇总: 通过 {n_ok}  失败 {n_fail}  警告 {n_warn}")
    if n_fail:
        print("\n失败项需要处理后才能进行全量抓取。")
    print("=" * 78)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
