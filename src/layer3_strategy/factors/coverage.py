# -*- coding: utf-8 -*-
"""
因子有效样本率报告（P2-7）。

背景：实跑日志显示预处理后各因子有效样本仅 40~58 万行/年（全量约 113 万行/年），
momentum 首年有效 0 —— 近半行因缺失被滤掉。缺失的主要来源：
  1. 上市初期：新股上市后前 250+ 个交易日没有足够历史算动量（合理缺失，无法消除）
  2. 财务 PIT 对齐：财务表覆盖缺口 / 上市后首份财报前的空窗（merge_asof backward
     在无财报可匹配时给 NaN）
  3. 截面样本不足：交易日截面 < min_cross_section 时整日跳过

本模块把「有效样本率」变成显式可见的闸门：
  - 按年 × 因子输出覆盖率表（落盘 + 日志）
  - 低于阈值的年份/因子打警告（让缺失率突变第一时间被发现）
  - 覆盖率连续走低 = 数据质量在退化，比盯单次回测数字更早暴露问题

用法：
    from src.layer3_strategy.factors.coverage import factor_coverage_report
    rep = factor_coverage_report(panel, score_cols)   # 返回 DataFrame，自动打日志
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from src.common.logger import logger

# 默认告警阈值：该年有效样本率低于此值即告警
DEFAULT_MIN_RATE = 0.30


def factor_coverage_report(
    panel: pd.DataFrame,
    score_cols: List[str],
    min_rate: float = DEFAULT_MIN_RATE,
) -> pd.DataFrame:
    """按年 × 因子统计有效样本率，低于阈值告警。

    有效样本 = 该年该因子得分非 NaN 的行数；总行数 = 该年面板行数。
    返回 DataFrame：index=year，columns=因子裸名，值=覆盖率。
    """
    if panel is None or panel.empty or not score_cols:
        logger.warning("面板为空或无因子列，跳过覆盖率报告")
        return pd.DataFrame()

    p = panel.copy()
    p["_year"] = pd.to_datetime(p["trade_date"]).dt.year
    total = p.groupby("_year").size()

    cols = []
    for col in score_cols:
        name = col.replace("f_", "").replace("_score", "")
        valid = p.groupby("_year")[col].apply(lambda s: int(s.notna().sum()))
        rate = (valid / total).round(4)
        cols.append(pd.Series(rate, name=name))

    rep = pd.concat(cols, axis=1)
    rep["total_rows"] = total.values
    rep.index.name = "year"

    # 告警扫描：0 有效年份（因子完全失效）优先；低于阈值次之
    low = []
    zero = []
    for year, row in rep.iterrows():
        for name in rep.columns[:-1]:
            v = float(row[name])
            if v <= 0:
                zero.append(f"{year}:{name}")
            elif v < min_rate:
                low.append(f"{year}:{name}={v:.0%}")

    lines = ["\n===== 因子有效样本率（按年，阈值 " + f"{min_rate:.0%}" + "） ====="]
    for year, row in rep.iterrows():
        parts = [f"{name}={row[name]:.0%}" for name in rep.columns[:-1]]
        lines.append(f"  {year}: 总行 {int(row['total_rows']):>8,}  " + "  ".join(parts))
    if zero:
        lines.append(f"  ⚠ 完全无有效样本: {', '.join(zero)}")
    if low:
        lines.append(f"  ⚠ 低于 {min_rate:.0%} 阈值: {', '.join(low)}")
    if not zero and not low:
        lines.append(f"  ✔ 所有因子逐年覆盖率均 ≥ {min_rate:.0%}")
    lines.append("=" * 60)
    logger.info("\n".join(lines))
    return rep


def warn_if_degraded(panel: pd.DataFrame, score_cols: List[str],
                     min_rate: float = DEFAULT_MIN_RATE) -> bool:
    """闸门：存在完全无有效样本的 (年份, 因子) 即返回 True（调用方可决定是否中断）。

    返回 True 表示「数据质量不达标」。
    """
    rep = factor_coverage_report(panel, score_cols, min_rate=min_rate)
    if rep.empty:
        return False
    for col in rep.columns[:-1]:
        if (rep[col] <= 0).any():
            return True
    return False
