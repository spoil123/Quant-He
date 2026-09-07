# -*- coding: utf-8 -*-
"""
第 1 层存储层：把清洗后的数据落库。

要点：
1. 写入前统一做「列对齐」—— DataFrame 只保留目标表存在的列，缺的补 None。
   不这么做，一旦 fetcher 多返回一个字段，to_sql 就会直接抛错中断整批。
2. 幂等：daily_price / daily_basic / index_daily 用 INSERT IGNORE（主键唯一），
   重复跑同一批数据不会报错也不会产生重复行；stock_basic / financial 用
   ON DUPLICATE KEY UPDATE，让更正可以覆盖旧值。
3. 每次写入都记 update_log，记录行数、耗时、状态，出问题能定位到具体哪只票。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.common.config import get_config
from src.common.db import execute, read_sql, write_df
from src.common.logger import logger

# 各表的目标列（顺序与建表脚本一致）
TABLE_COLUMNS: Dict[str, List[str]] = {
    "stock_basic": ["ts_code", "name", "exchange", "board", "list_date",
                    "delist_date", "is_delisted", "is_st", "status"],
    "daily_price": ["trade_date", "ts_code", "adj_type", "open", "high", "low", "close",
                    "pre_close", "change", "change_pct", "volume", "amount",
                    "turnover_rate", "amplitude", "status", "is_limit_up",
                    "is_limit_down", "is_suspended"],
    "daily_basic": ["trade_date", "ts_code", "close", "total_mv", "float_mv",
                    "total_share", "float_share", "pe_ttm", "pb", "ps_ttm",
                    "dv_ttm", "turnover_rate"],
    "financial_indicator": ["ts_code", "report_date", "report_type", "ann_date",
                            "ann_date_source", "eps", "eps_ttm", "bps", "roe",
                            "roe_ttm", "net_profit", "net_profit_ttm",
                            "total_revenue", "total_revenue_ttm", "total_assets",
                            "total_equity", "gross_margin", "debt_ratio"],
    "industry_classification": ["ts_code", "industry_code", "industry_name",
                                "source", "effective_date"],
    "index_daily": ["trade_date", "index_code", "open", "high", "low", "close",
                    "change_pct", "volume", "amount"],
    "trade_calendar": ["trade_date", "is_trading", "prev_date", "next_date", "is_month_end"],
    "factor_exposure": ["trade_date", "ts_code", "factor_name", "raw_value", "value"],
}

# 用 ON DUPLICATE KEY UPDATE 的表（内容会被更正，需要覆盖）
_UPSERT_TABLES = {"stock_basic", "financial_indicator", "industry_classification",
                  "factor_exposure", "trade_calendar"}


# ---------------------------------------------------------------- 数据质量闸门
# P0-2：写入前校验，脏数据超阈值即抛错中断，不做静默入库。
# 校验表：daily_price / daily_basic / stock_basic / financial_indicator
#   （financial_indicator 2026-09-06 加入：此前财务无清洗直接 upsert，
#   主备源字段异常/ann_date 缺失会静默入库 —— 见 cleaner 死代码问题）。
_QUALITY_GATED = {"daily_price", "daily_basic", "stock_basic",
                  "financial_indicator"}


class DataQualityError(Exception):
    """入库前质量校验失败。"""


def _quality_config() -> dict:
    try:
        return get_config("database").get("quality", {}) or {}
    except Exception:                                    # noqa: BLE001
        return {}


def check_quality(df: pd.DataFrame, table: str) -> None:
    """入库前质量闸门：指标超阈值抛 DataQualityError。

    逐表校验：
      - daily_price: 价格 <=0 或 NaN 占比、close(NaN) 占比、is_suspended 缺失率
      - daily_basic : 流通市值 <=0 或 NaN 占比
      - stock_basic : is_st 缺失率
    """
    q = _quality_config()
    if not q.get("enabled", True) or table not in _QUALITY_GATED:
        return
    if df is None or df.empty:
        return

    problems: List[str] = []

    if table == "daily_price":
        bad_price_ratio = float(q.get("max_bad_price_ratio", 0.001))
        close_nan_ratio = float(q.get("max_close_nan_ratio", 0.001))
        flag_missing = float(q.get("max_flag_missing_ratio", 0.05))

        price_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
        if price_cols:
            num = df[price_cols].apply(pd.to_numeric, errors="coerce")
            bad = (num.isna() | (num <= 0)).sum().sum()
            ratio = bad / (len(df) * len(price_cols))
            if ratio > bad_price_ratio:
                problems.append(
                    f"价格<=0或NaN占比 {ratio:.4%} > 阈值 {bad_price_ratio:.4%}"
                    f"（涉及 {bad} 格 / {len(df)*len(price_cols)} 格）"
                )
        if "close" in df.columns:
            cn = float(pd.to_numeric(df["close"], errors="coerce").isna().mean())
            if cn > close_nan_ratio:
                problems.append(f"复权收盘价 NaN 占比 {cn:.4%} > 阈值 {close_nan_ratio:.4%}")
        if "is_suspended" in df.columns:
            ms = float(df["is_suspended"].isna().mean())
            if ms > flag_missing:
                problems.append(f"is_suspended 缺失率 {ms:.4%} > 阈值 {flag_missing:.4%}")

    elif table == "daily_basic":
        bad_mv_ratio = float(q.get("max_bad_mv_ratio", 0.001))
        if "float_mv" in df.columns:
            mv = pd.to_numeric(df["float_mv"], errors="coerce")
            bad = (mv.isna() | (mv <= 0)).mean()
            if bad > bad_mv_ratio:
                problems.append(f"流通市值<=0或NaN占比 {bad:.4%} > 阈值 {bad_mv_ratio:.4%}")

    elif table == "stock_basic":
        flag_missing = float(q.get("max_flag_missing_ratio", 0.05))
        if "is_st" in df.columns:
            ms = float(df["is_st"].isna().mean())
            if ms > flag_missing:
                problems.append(f"is_st 缺失率 {ms:.4%} > 阈值 {flag_missing:.4%}")

    elif table == "financial_indicator":
        # 财务指标：ann_date 缺失 = PIT 对齐失效（回测会按错误时点用数据）；
        # 净资产收益率异常（|roe|>200%）多为字段错位/单位混用。只告警明显异常，
        # 比率阈值宽松 —— 财务字段本就允许 None（源不提供），不缺则不许错。
        ann_missing = float(q.get("max_fin_ann_missing_ratio", 0.05))
        if "ann_date" in df.columns:
            ma = float(df["ann_date"].isna().mean())
            if ma > ann_missing:
                problems.append(f"财务 ann_date 缺失率 {ma:.4%} > 阈值 {ann_missing:.4%}")
        if "roe_ttm" in df.columns:
            roe = pd.to_numeric(df["roe_ttm"], errors="coerce")
            bad_roe = float((roe.abs() > 500).mean())
            if bad_roe > float(q.get("max_fin_bad_roe_ratio", 0.02)):
                problems.append(f"财务 roe_ttm 异常(|x|>500) 占比 {bad_roe:.4%} > 2% 阈值")
        if "ts_code" in df.columns:
            dup = int(df["ts_code"].duplicated().sum())
            if dup:
                problems.append(f"ts_code 重复 {dup} 行（主键冲突会静默丢弃）")

    if problems:
        raise DataQualityError(
            f"[入库闸门] {table} 数据质量不达标，拒绝入库：\n  - " + "\n  - ".join(problems)
        )


def align_columns(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """把 DataFrame 裁剪/补齐成目标表的列结构。"""
    cols = TABLE_COLUMNS.get(table)
    if not cols:
        raise KeyError(f"未知表 {table}，请检查 TABLE_COLUMNS")
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = None
    extra = [c for c in out.columns if c not in cols]
    if extra:
        logger.debug(f"{table}: 忽略多余列 {extra}")
    return out[cols]


def save(df: pd.DataFrame, table: str, chunk_size: int | None = None,
         con=None) -> int:    # noqa: ANN001
    """通用写入。返回写入行数。

    con: 传入 SQLAlchemy 连接时在该连接的事务内写入（配合外层
         transaction_scope 做"删+写"原子操作，如复权重写）。
    """
    if df is None or df.empty:
        return 0
    # P0-2 数据质量闸门：脏数据超阈值抛错中断，不做静默入库
    check_quality(df, table)
    data = align_columns(df, table)
    method = "upsert" if table in _UPSERT_TABLES else "insert_ignore"
    return write_df(data, table, method=method, chunk_size=chunk_size, con=con)


# ---------------------------------------------------------------- 业务封装

def save_stock_basic(df: pd.DataFrame) -> int:
    n = save(df, "stock_basic")
    logger.info(f"stock_basic 写入 {n} 行")
    return n


def save_daily_price(df: pd.DataFrame, con=None) -> int:    # noqa: ANN001
    n = save(df, "daily_price", con=con)
    return n


def save_daily_basic(df: pd.DataFrame, con=None) -> int:    # noqa: ANN001
    return save(df, "daily_basic", con=con)


def save_financial(df: pd.DataFrame) -> int:
    return save(df, "financial_indicator")


def save_industry(df: pd.DataFrame) -> int:
    """行业分类入库。只保留映射需要的列。"""
    if df is None or df.empty:
        return 0
    d = df.copy()
    if "industry_code" not in d.columns and "industry" in d.columns:
        d["industry_code"] = d["industry"]
    if "industry_name" not in d.columns:
        d["industry_name"] = None
    if "effective_date" not in d.columns:
        d["effective_date"] = None
    if "source" not in d.columns:
        d["source"] = "sw_l1"
    return save(d, "industry_classification")


def save_index_daily(df: pd.DataFrame) -> int:
    return save(df, "index_daily")


def save_trade_calendar(df: pd.DataFrame) -> int:
    return save(df, "trade_calendar")


def save_factor_exposure(df: pd.DataFrame) -> int:
    return save(df, "factor_exposure")


# ---------------------------------------------------------------- 查询辅助

def get_last_date(table: str, ts_code: str | None = None, date_col: str = "trade_date") -> Optional[str]:
    """查询某表（或某只股票）已存的最大日期，作为增量更新起点。"""
    sql = f"SELECT MAX(`{date_col}`) AS d FROM `{table}`"
    params: Dict[str, object] = {}
    if ts_code:
        sql += " WHERE ts_code = :code"
        params["code"] = ts_code
    try:
        df = read_sql(sql, params)
        val = df.iloc[0, 0]
        return None if pd.isna(val) else str(val)
    except Exception as e:                                # noqa: BLE001
        logger.debug(f"查询 {table} 最大日期失败: {e}")
        return None


def get_stored_prices(ts_code: str, adj_type: str = "qfq",
                      start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """读取库内某只股票的价格（用于复权重述检测）。"""
    sql = "SELECT trade_date, close FROM `daily_price` WHERE ts_code = :code AND adj_type = :adj"
    params: Dict[str, object] = {"code": ts_code, "adj": adj_type}
    if start:
        sql += " AND trade_date >= :s"
        params["s"] = start
    if end:
        sql += " AND trade_date <= :e"
        params["e"] = end
    sql += " ORDER BY trade_date"
    return read_sql(sql, params)


def get_stock_codes(only_active: bool = False) -> List[str]:
    """取库内股票代码列表。"""
    sql = "SELECT ts_code FROM `stock_basic`"
    if only_active:
        sql += " WHERE is_delisted = 0"
    sql += " ORDER BY ts_code"
    df = read_sql(sql)
    return df["ts_code"].astype(str).tolist() if not df.empty else []


def get_trade_calendar(start: str | None = None, end: str | None = None) -> List:
    sql = "SELECT trade_date FROM `trade_calendar` WHERE is_trading = 1"
    params: Dict[str, object] = {}
    if start:
        sql += " AND trade_date >= :s"
        params["s"] = start
    if end:
        sql += " AND trade_date <= :e"
        params["e"] = end
    sql += " ORDER BY trade_date"
    df = read_sql(sql, params)
    return df["trade_date"].tolist() if not df.empty else []


def get_month_end_dates(start: str | None = None, end: str | None = None) -> List:
    """月末交易日（月度调仓用）。"""
    sql = "SELECT trade_date FROM `trade_calendar` WHERE is_month_end = 1"
    params: Dict[str, object] = {}
    if start:
        sql += " AND trade_date >= :s"
        params["s"] = start
    if end:
        sql += " AND trade_date <= :e"
        params["e"] = end
    sql += " ORDER BY trade_date"
    df = read_sql(sql, params)
    return df["trade_date"].tolist() if not df.empty else []


# ---------------------------------------------------------------- 更新日志

def log_update(task_name: str, status: str = "success", ts_code: str | None = None,
               start_date=None, end_date=None, rows: int = 0,
               message: str | None = None, started_at: datetime | None = None) -> None:
    """写一条更新日志。"""
    started = started_at or datetime.now()
    finished = datetime.now()
    dur = (finished - started).total_seconds()
    sql = """
        INSERT INTO `update_log`
            (task_name, ts_code, start_date, end_date, rows_written,
             status, message, started_at, finished_at, duration_sec)
        VALUES
            (:task, :code, :s, :e, :rows, :status, :msg, :st, :ft, :dur)
    """
    try:
        execute(sql, {
            "task": task_name, "code": ts_code, "s": start_date, "e": end_date,
            "rows": int(rows), "status": status, "msg": (message or "")[:500],
            "st": started, "ft": finished, "dur": round(dur, 2),
        })
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"写更新日志失败（不影响主流程）: {e}")


def table_stats() -> Dict[str, int]:
    """各表行数概览。"""
    out = {}
    for t in TABLE_COLUMNS:
        try:
            out[t] = int(read_sql(f"SELECT COUNT(*) AS c FROM `{t}`").iloc[0, 0])
        except Exception:                                 # noqa: BLE001
            out[t] = -1
    return out


if __name__ == "__main__":
    stats = table_stats()
    for k, v in stats.items():
        print(f"{k:26s} {v:>12,}")
