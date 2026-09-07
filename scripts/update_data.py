# -*- coding: utf-8 -*-
"""
第 1 层数据更新入口。

用法：
    # 小样本跑通全链路（强烈建议第一次先跑这个）
    python scripts/update_data.py --sample 30 --start 2023-01-01

    # 全量初始化（5000+ 只，耗时长，建议后台跑）
    python scripts/update_data.py --all --start 2015-01-01

    # 日常增量更新（挂定时任务）
    python scripts/update_data.py --update

    # 只更新某一部分
    python scripts/update_data.py --only calendar
    python scripts/update_data.py --only index
    python scripts/update_data.py --only industry
    python scripts/update_data.py --only basic
    python scripts/update_data.py --only daily --codes 600000,000001
    python scripts/update_data.py --only financial --sample 100

    # 强制重写（忽略复权漂移检测，整表覆盖）
    python scripts/update_data.py --only daily --codes 600000 --force-full
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_config  # noqa: E402
from src.common.logger import logger  # noqa: E402
from src.common.utils import chunked, normalize_code  # noqa: E402
from src.layer1_data.storage import repository as repo  # noqa: E402
from src.layer1_data.storage import updater  # noqa: E402


def _pick_codes(args) -> list[str]:
    """决定本次要处理哪些股票。"""
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()]

    known = repo.get_stock_codes()
    if args.sample and known:
        # 从库内已有主表里等间隔抽样，覆盖不同板块
        step = max(1, len(known) // args.sample)
        return known[::step][: args.sample]
    if args.sample:
        # 主表还没建时，先临时抓一次列表再抽样
        from src.layer1_data.fetcher import stock_list as sl
        uni = sl.build_universe()
        step = max(1, len(uni) // args.sample)
        return uni["ts_code"].tolist()[::step][: args.sample]
    return known


def main() -> int:
    ap = argparse.ArgumentParser(description="第 1 层数据更新")
    ap.add_argument("--all", action="store_true", help="全量初始化（主表+日历+指数+行业+日线+财务）")
    ap.add_argument("--update", action="store_true", help="增量更新")
    ap.add_argument("--only", choices=["basic", "calendar", "index", "industry",
                                       "daily", "financial"], help="只更新指定部分")
    ap.add_argument("--sample", type=int, default=0, help="只处理 N 只股票（调试用）")
    ap.add_argument("--codes", type=str, default="", help="指定股票代码，逗号分隔")
    ap.add_argument("--start", type=str, default=None, help="数据起始日期")
    ap.add_argument("--force-full", action="store_true", help="强制重写（忽略复权漂移检测）")
    ap.add_argument("--workers", type=int, default=4,
                    help="并发线程数（默认 4。请求速率仍受全局限流控制，调高主要减少等待）")
    args = ap.parse_args()

    if not (args.all or args.update or args.only):
        ap.print_help()
        return 1

    t0 = datetime.now()
    cfg = get_config("universe")
    start = args.start or cfg["history"]["start_date"]

    print("=" * 70)
    print(f"第 1 层数据更新   起始日期={start}")
    print("=" * 70)

    # 失败汇总（P1-10 告警）：任何环节有失败，最终非零退出 + 落盘错误日志
    failures: list[str] = []

    # ---------------- 单模块 ----------------
    if args.only == "basic":
        updater.update_stock_basic()
    elif args.only == "calendar":
        updater.update_trade_calendar()
    elif args.only == "index":
        updater.update_index()
    elif args.only == "industry":
        updater.update_industry()
    elif args.only == "daily":
        codes = _pick_codes(args)
        logger.info(f"更新日线：{len(codes)} 只")
        stats = updater.update_daily_batch(codes, start_date=start, force_full=args.force_full,
                                           max_workers=args.workers)
        print("\n日线统计:", {k: v for k, v in stats.items() if k != "failed_codes"})
        if stats["failed"]:
            failures.append(f"daily 失败 {stats['failed']} 只: {stats['failed_codes'][:20]}")
        if stats["failed_codes"]:
            print("失败代码:", stats["failed_codes"][:20])
    elif args.only == "financial":
        codes = _pick_codes(args)
        # 增量：跳过已入库且含最新报告期的 ts_code。判定逻辑统一放在
        # update_financial(skip_existing=True) 内，与 update_all 共用同一实现，
        # 避免两条路径行为不一致（M5 修复）。
        stats = updater.update_financial(codes, skip_existing=True)
        print("\n财务统计:", {k: v for k, v in stats.items() if k != "failed_codes"})
        if stats["failed"]:
            failures.append(f"financial 失败 {stats['failed']} 只: {stats['failed_codes'][:20]}")
    elif args.all or args.update:
        if args.all:
            codes = _pick_codes(args) if (args.sample or args.codes) else None
            result = updater.init_all(codes=codes, start_date=start, max_workers=args.workers)
        else:
            codes = _pick_codes(args) if (args.sample or args.codes) else None
            result = updater.update_all(codes=codes, max_workers=args.workers)
        print("\n更新结果:")
        for k, v in result.items():
            print(f"  {k:16s} {v}")
        daily = result.get("daily") or {}
        if isinstance(daily, dict) and daily.get("failed"):
            failures.append(f"daily 失败 {daily['failed']} 只")
        fin = result.get("financial") or {}
        if isinstance(fin, dict) and fin.get("failed"):
            failures.append(f"financial 失败 {fin['failed']} 只")

    # ---------------- 收尾统计 ----------------
    print("\n" + "=" * 70)
    print("库内行数概览")
    print("=" * 70)
    for k, v in repo.table_stats().items():
        print(f"  {k:26s} {v:>12,}")
    print(f"\n总耗时 {(datetime.now() - t0).total_seconds():.1f}s")

    # P1-10 失败告警：非零退出 + 错误日志（任务挂了调度层第一时间发现）
    if failures:
        for msg in failures:
            logger.error(f"[抓取失败] {msg}")
        print("\n" + "=" * 70)
        print(f"❌ 数据更新存在失败（{len(failures)} 项），退出码 1")
        print("=" * 70)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
