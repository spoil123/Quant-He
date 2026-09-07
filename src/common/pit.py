# -*- coding: utf-8 -*-
"""
Point-In-Time 时点对齐（杜绝前视偏差的通用工具）。

为什么需要它：
    财报有两个日期 —— report_date（报告期，如 2023-12-31）和 ann_date
    （实际公布日，可能是 2024-03-28）。用 report_date 做对齐，等于在
    2023 年底就用上了 2024 年 3 月才公布的利润，回测必然虚高。

    A 股现实：AKShare 多数财务接口只给报告期，不给公告日。本模块提供两档策略：
      conservative（默认）：用「法定最迟披露日」作为可用日，宁可晚用不早用。
      precise              ：若能从接口拿到真实公告日，则用真实公告日。

法定最迟披露日（依据《上市公司信息披露管理办法》）：
    Q1（03-31）  -> 当年 04-30
    半年（06-30）-> 当年 08-31
    Q3（09-30）  -> 当年 10-31
    年报（12-31）-> 次年 04-30

TTM 口径（中国财报是「年初至今累计」口径，不能简单相加四季）：
    TTM(t) = 累计(t) + 累计(去年年报) - 累计(去年同报告期)
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.common.logger import logger
from src.common.utils import to_date

# 报告期 -> 法定最迟披露日（月，日）
_STATUTORY_DEADLINE: Dict[tuple[int, int], tuple[int, int, int]] = {
    (3, 31): (4, 30, 0),     # Q1     -> 同年 4/30
    (6, 30): (8, 31, 0),     # 半年报  -> 同年 8/31
    (9, 30): (10, 31, 0),    # Q3     -> 同年 10/31
    (12, 31): (4, 30, 1),    # 年报    -> 次年 4/30
}


def statutory_deadline(report_date: date | str) -> Optional[date]:
    """由报告期推算法定最迟披露日。"""
    rd = to_date(report_date)
    if rd is None:
        return None
    rule = _STATUTORY_DEADLINE.get((rd.month, rd.day))
    if rule is None:
        # 非常规报告期（如季度调整），退化为报告期末 + 4 个月
        month = rd.month + 4
        year = rd.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return date(year, month, 1)
    month, day, year_offset = rule
    return date(rd.year + year_offset, month, day)


def report_type_of(report_date: date | str) -> str:
    """报告期类型：quarter1 / semi / quarter3 / annual。"""
    rd = to_date(report_date)
    if rd is None:
        return "unknown"
    return {
        (3, 31): "quarter1",
        (6, 30): "semi",
        (9, 30): "quarter3",
        (12, 31): "annual",
    }.get((rd.month, rd.day), "unknown")


def add_ann_date(
    df: pd.DataFrame,
    report_col: str = "report_date",
    ann_col: str = "ann_date",
    strategy: str = "conservative",
    source_col: str = "ann_date_source",
) -> pd.DataFrame:
    """为财务表补全数据可用日 ann_date。

    strategy:
      conservative —— ann_date = 法定最迟披露日（无视已有 ann_date）
      precise      —— 已有 ann_date 就沿用；缺失的用法定截止日兜底
    """
    if df is None or df.empty:
        return df
    df = df.copy()

    if report_col not in df.columns:
        raise KeyError(f"财务表缺少报告期列 {report_col}")

    df[report_col] = pd.to_datetime(df[report_col], errors="coerce").dt.date

    if ann_col not in df.columns:
        df[ann_col] = None
        df[source_col] = 0
    else:
        df[ann_col] = pd.to_datetime(df[ann_col], errors="coerce").dt.date
        df[source_col] = df[ann_col].notna().astype(int)

    if strategy == "conservative":
        df[ann_col] = df[report_col].map(statutory_deadline)
        df[source_col] = 0
    else:  # precise
        fallback = df[report_col].map(statutory_deadline)
        df[ann_col] = df[ann_col].fillna(fallback)

    df["report_type"] = df[report_col].map(report_type_of)
    return df


def next_trade_date(calendar: Sequence[date], d: date) -> Optional[date]:
    """在交易日历中找 d 之后的第一个交易日（含 d 本身若在日历中）。"""
    if calendar is None or len(calendar) == 0:            # noqa: SIM108
        return d
    cal = pd.to_datetime(pd.Series(list(calendar))).sort_values().reset_index(drop=True)
    pos = int(cal.searchsorted(pd.Timestamp(d)))
    if pos >= len(cal):
        return None
    return cal.iloc[pos].date()


def shift_to_trade_date(
    df: pd.DataFrame,
    calendar: Sequence[date],
    date_col: str = "ann_date",
    out_col: str = "usable_date",
    lag_days: int = 1,
) -> pd.DataFrame:
    """把自然日 ann_date 映射到「可交易的最早日期」，并延后 lag_days 个交易日。

    lag_days=1 表示：公告日当天数据才公开（实为盘后），最快 T+1 才能交易。
    """
    if df is None or df.empty or calendar is None or len(calendar) == 0:
        df = df.copy() if df is not None else pd.DataFrame()
        df[out_col] = df.get(date_col)
        return df

    df = df.copy()
    cal = pd.to_datetime(pd.Series(list(calendar))).sort_values().reset_index(drop=True)

    def _shift(d: date | None) -> Optional[date]:
        if d is None or pd.isna(d):
            return None
        pos = int(cal.searchsorted(pd.Timestamp(d)))
        pos = min(pos + lag_days - 1, len(cal) - 1)
        val = cal.iloc[pos]
        return val.date() if hasattr(val, "date") else val

    df[out_col] = df[date_col].map(_shift)
    return df


def build_pit_panel(
    fin_df: pd.DataFrame,
    trade_dates: Sequence[date],
    ts_codes: Optional[Sequence[str]] = None,
    ts_col: str = "ts_code",
    date_col: str = "usable_date",
    value_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """构建 PIT 面板：对每个 (交易日, 股票) 给出「该日实际可见的最新一期财务值」。

    实现用 merge_asof(direction="backward")：只取 <= 交易日 的最近一条，
    天然保证不引入未来信息。

    返回 DataFrame: trade_date / ts_code / <value_cols...>
    """
    if fin_df is None or fin_df.empty:
        return pd.DataFrame(columns=["trade_date", "ts_code"] + (value_cols or []))

    fin = fin_df.copy()
    fin[date_col] = pd.to_datetime(fin[date_col], errors="coerce")
    fin = fin.dropna(subset=[date_col]).sort_values([ts_col, date_col])

    value_cols = value_cols or [c for c in fin.columns
                                if c not in (ts_col, date_col, "report_date",
                                             "ann_date", "report_type", "ann_date_source")]

    # 左表：交易日 × 股票 的笛卡尔积
    codes = list(ts_codes) if ts_codes is not None else sorted(fin[ts_col].unique())
    left = pd.MultiIndex.from_product(
        [pd.to_datetime(pd.Series(list(trade_dates))), codes],
        names=["trade_date", ts_col],
    ).to_frame(index=False)

    right = fin[[ts_col, date_col] + value_cols].rename(columns={date_col: "trade_date"})

    panel = pd.merge_asof(
        left.sort_values("trade_date"),
        right.sort_values("trade_date"),
        by=ts_col,
        on="trade_date",
        direction="backward",
        allow_exact_matches=True,
    )
    panel["trade_date"] = panel["trade_date"].dt.date
    return panel.sort_values(["trade_date", ts_col]).reset_index(drop=True)


def compute_ttm(
    fin_df: pd.DataFrame,
    value_col: str,
    ts_col: str = "ts_code",
    report_col: str = "report_date",
    out_col: str | None = None,
) -> pd.Series:
    """把累计口径的季节数据换算成 TTM（滚动 12 个月）。

    公式（中国财报为年初至今累计口径）：
        年报(12-31)：TTM = 当年累计
        其他报告期：TTM = 本期累计 + 去年年报累计 - 去年同报告期累计
    """
    out_col = out_col or f"{value_col}_ttm"
    if fin_df is None or fin_df.empty or value_col not in fin_df.columns:
        return pd.Series(index=fin_df.index if fin_df is not None else None, dtype="float64")

    df = fin_df[[ts_col, report_col, value_col]].copy()
    df[report_col] = pd.to_datetime(df[report_col], errors="coerce").dt.date
    df["_year"] = df[report_col].map(lambda d: d.year if d else None)
    df["_md"] = df[report_col].map(lambda d: (d.month, d.day) if d else None)

    # 去年年报（12-31）
    annual = df[df["_md"] == (12, 31)][[ts_col, "_year", value_col]].rename(
        columns={"_year": "_y_ann", value_col: "_v_ann"}
    )
    annual["_y_ann"] = annual["_y_ann"] + 1   # 对齐到「使用它的年份」

    # 去年同报告期
    same = df[[ts_col, "_year", "_md", value_col]].rename(
        columns={"_year": "_y_same", value_col: "_v_same"}
    )
    same["_y_same"] = same["_y_same"] + 1

    m = df.merge(annual, left_on=[ts_col, "_year"], right_on=[ts_col, "_y_ann"], how="left")
    m = m.merge(same, left_on=[ts_col, "_year", "_md"], right_on=[ts_col, "_y_same", "_md"], how="left")

    is_annual = m["_md"] == (12, 31)
    ttm = m[value_col].astype("float64")
    ttm_non_annual = (
        m[value_col].astype("float64")
        + m["_v_ann"].astype("float64")
        - m["_v_same"].astype("float64")
    )
    result = ttm.where(is_annual, ttm_non_annual)
    return pd.Series(result.values, index=fin_df.index, name=out_col)


if __name__ == "__main__":
    for rd in ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]:
        d = to_date(rd)
        print(f"{rd} 报告期={report_type_of(d):9s} 法定最迟披露={statutory_deadline(d)}")

    # PIT 对齐演示
    fin = pd.DataFrame({
        "ts_code": ["600000"] * 4,
        "report_date": pd.to_datetime(["2022-12-31", "2023-03-31", "2023-06-30", "2023-09-30"]).date,
        "net_profit": [100.0, 30.0, 65.0, 95.0],
    })
    fin = add_ann_date(fin, strategy="conservative")
    cal = pd.bdate_range("2023-04-25", "2023-11-05").date
    fin = shift_to_trade_date(fin, cal, lag_days=1)
    print("\n财务表（含可用日）:")
    print(fin.to_string())

    panel = build_pit_panel(fin, cal, value_cols=["net_profit"])
    print("\nPIT 面板抽样（验证 2023-05-01 还看不到 Q1 之后的数据）:")
    for d in ["2023-04-28", "2023-05-04", "2023-09-01", "2023-11-01"]:
        row = panel[(panel["trade_date"] == to_date(d))]
        print(f"  {d}: 可见净利润 = {row['net_profit'].values}")
