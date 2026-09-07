# -*- coding: utf-8 -*-
"""
数据质量验证（每次更新数据后跑一遍）。

检查项：
    1. 各表行数与覆盖范围
    2. 复权连续性：qfq 与 hfq 的比值在相邻交易日间应基本恒定
       比值突变说明复权因子跳变，会导致回测出现假跳空
    3. 时点对齐：财务的 ann_date 必须晚于 report_date（否则就是前视数据）
    4. 结构性异常：OHLC 自相矛盾、收盘价非正等
    5. 停牌 / 涨跌停标记是否合理
    6. 增量幂等性（--with-idempotent）：重跑同一只股票，行数不应增加

用法：
    python scripts/verify_data.py
    python scripts/verify_data.py --with-idempotent
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src.common.db import read_sql  # noqa: E402
from src.common.logger import logger  # noqa: E402

OK, FAIL, WARN = "[通过]", "[失败]", "[警告]"


def check_coverage() -> None:
    """表行数与覆盖范围。"""
    print("\n--- 1. 数据覆盖 ---")
    for t in ["stock_basic", "daily_price", "daily_basic", "financial_indicator",
              "industry_classification", "index_daily", "trade_calendar"]:
        try:
            n = int(read_sql(f"SELECT COUNT(*) AS c FROM `{t}`").iloc[0, 0])
            flag = OK if n > 0 else WARN
            print(f"{flag} {t:26s} {n:>10,} 行")
        except Exception as e:                            # noqa: BLE001
            print(f"{FAIL} {t:26s} {type(e).__name__}")

    df = read_sql("""
        SELECT adj_type,
               COUNT(*) AS n_rows,          -- rows 是 MySQL 保留字，别直接用
               COUNT(DISTINCT ts_code) AS stocks,
               MIN(trade_date) AS d0, MAX(trade_date) AS d1
        FROM daily_price GROUP BY adj_type
    """)
    if not df.empty:
        print("\n  日线明细:")
        for _, r in df.iterrows():
            print(f"    {r['adj_type']:5s} {int(r['n_rows']):>8,} 行  "
                  f"{int(r['stocks']):>5} 只  {r['d0']} ~ {r['d1']}")


def check_adjust_continuity() -> None:
    """复权连续性：qfq/hfq 比值应逐日恒定（除权日除外，但也不应剧烈跳变）。"""
    print("\n--- 2. 复权连续性 ---")
    # MySQL 不支持 "LIMIT & IN/ALL/ANY/SOME subquery"（错误 1235），
    # 所以拆成两步：先取样本代码，再用参数化 IN 查明细
    top = read_sql("""
        SELECT ts_code, COUNT(*) AS n
        FROM daily_price WHERE adj_type = 'qfq'
        GROUP BY ts_code ORDER BY n DESC LIMIT 5
    """)
    if top.empty:
        print(f"{WARN} 无数据可校验（需要 qfq 数据）")
        return

    codes = top["ts_code"].tolist()
    ph = ", ".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    df = read_sql(f"""
        SELECT q.ts_code, q.trade_date, q.close AS qc, h.close AS hc
        FROM daily_price q
        JOIN daily_price h
          ON q.ts_code = h.ts_code AND q.trade_date = h.trade_date
        WHERE q.adj_type = 'qfq' AND h.adj_type = 'hfq'
          AND q.ts_code IN ({ph})
        ORDER BY q.ts_code, q.trade_date
    """, params)
    if df.empty:
        print(f"{WARN} 无数据可校验（需要 qfq 与 hfq 同时存在）")
        return

    df["ratio"] = df["hc"] / df["qc"].replace(0, pd.NA)
    bad = 0
    for code, g in df.groupby("ts_code"):
        r = g["ratio"].dropna()
        if len(r) < 2:
            continue
        # 相邻交易日比值的相对变化，超过 1% 视为跳变
        chg = (r / r.shift(1) - 1).abs()
        n_bad = int((chg > 0.01).sum())
        bad += n_bad
        if n_bad:
            print(f"{WARN} {code}: 复权比值跳变 {n_bad} 次")
    print(f"{OK if bad == 0 else WARN} 复权比值跳变合计 {bad} 次"
          f"（每只股票每年除权 1-2 次属正常）")


def check_pit_alignment() -> None:
    """时点对齐：ann_date 必须晚于 report_date。"""
    print("\n--- 3. 时点对齐（杜绝前视偏差）---")
    df = read_sql("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN ann_date IS NULL THEN 1 ELSE 0 END) AS no_ann,
               SUM(CASE WHEN ann_date <= report_date THEN 1 ELSE 0 END) AS bad_order
        FROM financial_indicator
    """)
    if df.empty or df["total"].iloc[0] == 0:
        print(f"{WARN} 财务表为空，跳过")
        return
    total = int(df["total"].iloc[0])
    no_ann = int(df["no_ann"].iloc[0] or 0)
    bad = int(df["bad_order"].iloc[0] or 0)
    print(f"{OK if bad == 0 else FAIL} 共 {total:,} 条，"
          f"无公告日 {no_ann} 条，公告日早于报告期 {bad} 条")
    if bad:
        print("    ^ 公告日早于报告期意味着用到了未来数据，必须修复")


def check_quality_view() -> None:
    """结构性异常行。"""
    print("\n--- 4. 结构性异常 ---")
    try:
        df = read_sql("SELECT issue, COUNT(*) AS n FROM v_data_quality GROUP BY issue")
    except Exception as e:                                # noqa: BLE001
        print(f"{WARN} 质量视图查询失败: {e}")
        return
    if df.empty:
        print(f"{OK} 未发现结构性异常行")
        return
    for _, r in df.iterrows():
        print(f"{WARN} {r['issue']:24s} {int(r['n']):>8,} 行")


def check_flags() -> None:
    """停牌 / 涨跌停标记的合理性。"""
    print("\n--- 5. 交易状态标记 ---")
    df = read_sql("""
        SELECT COUNT(*) AS total,
               SUM(is_suspended) AS suspended,
               SUM(is_limit_up) AS lu,
               SUM(is_limit_down) AS ld,
               SUM(status = 2) AS st
        FROM daily_price WHERE adj_type = 'qfq'
    """)
    if df.empty or int(df["total"].iloc[0] or 0) == 0:
        print(f"{WARN} 无数据")
        return
    r = df.iloc[0]
    total = int(r["total"])
    print(f"{OK} 总行数 {total:,}  停牌 {int(r['suspended'] or 0):,}  "
          f"涨停 {int(r['lu'] or 0):,}  跌停 {int(r['ld'] or 0):,}  "
          f"ST {int(r['st'] or 0):,}")
    # 涨停占比正常应在 1%~5%，过高说明阈值判断有问题
    lu_ratio = (int(r["lu"] or 0) / total) if total else 0
    if lu_ratio > 0.15:
        print(f"{WARN} 涨停占比 {lu_ratio:.1%} 偏高，涨跌停阈值可能需要按板块细分")


def check_idempotent() -> None:
    """增量幂等：重跑同一只股票，行数不应增加。"""
    print("\n--- 6. 增量幂等性 ---")
    from src.layer1_data.storage import repository as repo
    from src.layer1_data.storage import updater

    # 必须挑一只「已经入库过」的股票才能测出幂等性。
    # 挑一只没数据的票，结果会是 0 -> N，那是首次写入，不是重复插入。
    top = read_sql("""
        SELECT ts_code, COUNT(*) AS n FROM daily_price
        GROUP BY ts_code ORDER BY n DESC LIMIT 1
    """)
    if top.empty:
        print(f"{WARN} 日线表为空，无法测试幂等性")
        return
    code = str(top.iloc[0, 0])

    before = int(read_sql(
        "SELECT COUNT(*) AS c FROM daily_price WHERE ts_code = :c", {"c": code}).iloc[0, 0])
    updater.update_daily_one(code, "2015-01-01")
    after = int(read_sql(
        "SELECT COUNT(*) AS c FROM daily_price WHERE ts_code = :c", {"c": code}).iloc[0, 0])

    if after == before:
        print(f"{OK} {code}: 重跑前后均为 {before:,} 行（幂等）")
    else:
        print(f"{FAIL} {code}: {before:,} -> {after:,} 行，重复插入了 {after - before:,} 行")


def main() -> int:
    print("=" * 70)
    print("数据质量验证")
    print("=" * 70)
    check_coverage()
    check_adjust_continuity()
    check_pit_alignment()
    check_quality_view()
    check_flags()
    if "--with-idempotent" in sys.argv:
        check_idempotent()
    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
