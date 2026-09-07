# -*- coding: utf-8 -*-
"""
初始化数据库：建库 -> 建表 -> 建视图 -> 打印校验结果。

用法：
    python scripts/init_db.py                # 建库建表（已存在则跳过）
    python scripts/init_db.py --drop         # 先删库再重建（危险，会清空全部数据）
    python scripts/init_db.py --check        # 只检查连接与表状态，不改动

前置条件：已复制 .env.example 为 .env 并填好 DB_PASSWORD。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证能从项目根导入 src
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import PROJECT_ROOT as ROOT  # noqa: E402
from src.common.config import get_config, get_db_url  # noqa: E402
from src.common.db import (  # noqa: E402
    check_connection,
    create_database,
    execute_sql_file,
    get_server_engine,
    read_sql,
    table_exists,
)
from src.common.logger import logger  # noqa: E402

SQL_DIR = ROOT / "sql"
TABLES = [
    "stock_basic", "daily_price", "adj_factor", "daily_basic",
    "financial_indicator", "industry_classification", "index_daily",
    "trade_calendar", "update_log", "factor_exposure", "backtest_result",
]


def drop_database() -> None:
    mysql = get_config("database")["mysql"]
    db = mysql["database"]
    logger.warning(f"即将删除数据库 {db} 及其全部数据")
    eng = get_server_engine()
    with eng.begin() as conn:
        conn.execute(f"DROP DATABASE IF EXISTS `{db}`")
    eng.dispose()
    logger.info(f"数据库 {db} 已删除")


def run(drop: bool = False) -> int:
    if drop:
        drop_database()

    logger.info("创建数据库 ...")
    create_database()

    for f in ("01_create_database.sql", "02_create_tables.sql", "03_create_views.sql"):
        path = SQL_DIR / f
        if not path.exists():
            logger.error(f"缺少 SQL 脚本: {path}")
            return 1
        logger.info(f"执行 {f} ...")
        execute_sql_file(path)

    # 校验
    logger.info("-" * 60)
    missing = [t for t in TABLES if not table_exists(t)]
    if missing:
        logger.error(f"以下表未创建成功: {missing}")
        return 1

    logger.info("表创建校验通过：")
    for t in TABLES:
        n = int(read_sql(f"SELECT COUNT(*) AS c FROM `{t}`").iloc[0, 0])
        logger.info(f"  {t:26s} {n:>10,} 行")

    parts = read_sql(
        "SELECT PARTITION_NAME, TABLE_ROWS FROM information_schema.PARTITIONS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'daily_price' "
        "AND PARTITION_NAME IS NOT NULL"
    )
    if not parts.empty:
        logger.info(f"daily_price 分区数: {len(parts)}")
    return 0


def check() -> int:
    ok = check_connection()
    if not ok:
        return 1
    for t in TABLES:
        if table_exists(t):
            n = int(read_sql(f"SELECT COUNT(*) AS c FROM `{t}`").iloc[0, 0])
            print(f"  [存在] {t:26s} {n:>10,} 行")
        else:
            print(f"  [缺失] {t}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 quant 数据库")
    ap.add_argument("--drop", action="store_true", help="先删除数据库再重建")
    ap.add_argument("--check", action="store_true", help="只检查连接与表状态")
    args = ap.parse_args()

    if args.check:
        return check()

    print("=" * 70)
    print("本地金融数据库初始化")
    print(f"目标: {get_db_url(hide_password=True)}")
    print("=" * 70)
    return run(drop=args.drop)


if __name__ == "__main__":
    sys.exit(main())
