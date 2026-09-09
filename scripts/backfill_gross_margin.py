#!/usr/bin/env python3
"""毛利率存量回填（2026-09-09，第三轮审计 G1）。

背景：财务表 gross_margin 自 2020 年起 100% NULL（同花顺上游停更）。
2026-09-07 的 _repair_gross_margin 只挂在抓取路径，而 update_financial
(skip_existing=True) 会跳过全部已入库代码，存量永远等不到修复。

优化（为什么快）：不走「逐只重抓 5912 只」（baostock 限流下要 1-2 小时），
改用东财业绩报表 ak.stock_yjbb_em(报告期) —— 一次请求拿全市场 5000+ 只
的销售毛利率，26 个季度 = 26 次请求，全程约 5 分钟。

口径核对（2023-03-31 实测）：600519 茅台 92.60、000651 格力 27.42，
与 baostock gpMargin / 库内已有 % 口径一致（累计口径）。

幂等：只 UPDATE gross_margin IS NULL 的行，可随时中断重跑。
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pymysql

from src.common.logger import logger


def main() -> int:
    ap = argparse.ArgumentParser(description="毛利率存量回填（东财业绩报表，按季度整市场）")
    ap.add_argument("--start", default="2020-01-01", help="只回填该日期之后的报告期")
    ap.add_argument("--sleep", type=float, default=0.8, help="季度间请求间隔（限流）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                           password="123456", database="quant", charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT report_date FROM financial_indicator "
        "WHERE gross_margin IS NULL AND report_date >= %s ORDER BY 1",
        (args.start,),
    )
    quarters = [r[0] for r in cur.fetchall()]
    total_rows = len(quarters)
    if not quarters:
        logger.info("没有待回填的报告期，退出")
        return 0
    logger.info(f"待回填 {len(quarters)} 个季度（{quarters[0]} ~ {quarters[-1]}），"
                f"{'dry-run' if args.dry_run else '开始写库'}")

    filled_total, missed_total = 0, 0
    t0 = time.time()
    for i, qd in enumerate(quarters, 1):
        ymd = qd.strftime("%Y%m%d")
        qstr = qd.strftime("%Y-%m-%d")
        try:
            df = ak.stock_yjbb_em(date=ymd)
        except Exception as e:                            # noqa: BLE001
            logger.error(f"[{i}/{len(quarters)}] {qstr} 拉取失败: "
                         f"{type(e).__name__}: {e}（跳过，重跑可补）")
            missed_total += 1
            time.sleep(max(args.sleep, 2.0))
            continue

        pairs = [
            (float(gm), str(code))
            for code, gm in zip(df["股票代码"].astype(str), df["销售毛利率"])
            if gm is not None and str(gm) != "nan" and str(gm) != "--"
        ]

        if args.dry_run:
            cur.execute(
                "SELECT COUNT(*) FROM financial_indicator "
                "WHERE report_date=%s AND gross_margin IS NULL", (qstr,))
            n_null = cur.fetchone()[0]
            logger.info(f"[{i}/{len(quarters)}] {qstr} dry-run: "
                        f"接口 {len(pairs)} 只，库内待填 {n_null} 行")
            continue

        rows = [(gm, code, qstr) for gm, code in pairs]
        n = cur.executemany(
            "UPDATE financial_indicator SET gross_margin=%s, updated_at=%s "
            "WHERE ts_code=%s AND report_date=%s AND gross_margin IS NULL",
            [(gm, datetime.now(), code, qstr) for gm, code, _ in rows],
        )
        conn.commit()
        filled_total += n
        logger.info(f"[{i}/{len(quarters)}] {qstr}: 接口 {len(pairs)} 只，"
                    f"回填 {n} 行")
        time.sleep(args.sleep)

    cur.execute(
        "SELECT COUNT(*) FROM financial_indicator "
        "WHERE gross_margin IS NULL AND report_date >= %s", (args.start,))
    left = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM financial_indicator "
        "WHERE gross_margin IS NULL AND report_date >= '2020-01-01'")
    left_all = cur.fetchone()[0]
    logger.info(f"完成：回填 {filled_total} 行，失败季度 {missed_total}，"
                f"耗时 {time.time() - t0:.0f}s")
    logger.info(f"剩余 NULL（>= {args.start}）：{left} 行；"
                f"2020 后全部剩余 {left_all} 行（银行/券商等无毛利率概念属正常）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
