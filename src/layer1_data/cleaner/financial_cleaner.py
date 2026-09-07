# -*- coding: utf-8 -*-
"""
财务数据清洗。

财务数据的脏法和行情不一样，重点查这几类：

1. 公告日晚于报告期太久
   正常年报最迟次年 4 月底公布。如果 ann_date 比 report_date 晚两年以上，
   基本是接口串了数据或者公司长期停牌，这种样本直接弃用。

2. 比率型字段越界
   ROE 超过 ±100%、毛利率超出 [-100%, 100%]、资产负债率为负，都是不可能的取值，
   置为 NaN 而不是截断 —— 截断会造出"看起来合理"的假数据。

3. 同一报告期重复行
   接口偶尔会返回重复记录，按「字段完整性」挑最好的一条保留。

4. TTM 列与单季列的勾稽关系
   eps_ttm 应该与 eps 同号（除非业绩剧烈反转），差异过大时记录但不改，
   留给人工复核。

清洗规则只做「标记 + 置 NaN」，不删除整只股票的数据。
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from src.common.logger import logger
from src.common.utils import normalize_code

# 各字段的合理取值区间（超出则置 NaN）
_VALID_RANGES: Dict[str, Tuple[float, float]] = {
    "roe": (-100.0, 100.0),
    "roe_ttm": (-100.0, 100.0),
    "gross_margin": (-100.0, 100.0),
    "debt_ratio": (0.0, 100.0),
    "eps": (-20.0, 50.0),
    "eps_ttm": (-30.0, 60.0),
    "bps": (-20.0, 200.0),
}

# 报告期到公告日的合理间隔（天）：超过则视为异常
_MAX_ANN_LAG_DAYS = 730


def clean_financial(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
    """清洗财务表。返回 (清洗后的 DataFrame, 质量报告)。"""
    report: Dict = {"input_rows": 0, "output_rows": 0, "notes": {}}

    if df is None or df.empty:
        return pd.DataFrame(), report

    out = df.copy()
    report["input_rows"] = len(out)

    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).map(normalize_code)

    # ---- 日期统一 ----
    for c in ("report_date", "ann_date"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.date

    # ---- 1. 公告日晚于报告期过久 ----
    if {"report_date", "ann_date"}.issubset(out.columns):
        rd = pd.to_datetime(out["report_date"], errors="coerce")
        ad = pd.to_datetime(out["ann_date"], errors="coerce")
        lag = (ad - rd).dt.days
        bad = lag > _MAX_ANN_LAG_DAYS
        n_bad = int(bad.sum())
        if n_bad:
            report["notes"]["ann_date_too_late"] = n_bad
            out.loc[bad, "ann_date"] = None
        # 公告日早于报告期（不可能）也清掉
        bad2 = lag < 0
        n_bad2 = int(bad2.sum())
        if n_bad2:
            report["notes"]["ann_date_before_report"] = n_bad2
            out.loc[bad2, "ann_date"] = None

        # ann_date 为空的行无法做时点对齐，直接剔除
        before = len(out)
        out = out.dropna(subset=["ann_date"])
        if len(out) < before:
            report["notes"]["dropped_no_ann_date"] = before - len(out)

    # ---- 2. 比率字段越界 -> 置 NaN ----
    for col, (lo, hi) in _VALID_RANGES.items():
        if col not in out.columns:
            continue
        s = pd.to_numeric(out[col], errors="coerce")
        bad = (s < lo) | (s > hi)
        n = int(bad.sum())
        if n:
            report["notes"][f"{col}_out_of_range"] = n
            out.loc[bad, col] = np.nan

    # ---- 3. 同一报告期去重：保留非缺失字段最多的一条 ----
    if {"ts_code", "report_date"}.issubset(out.columns):
        before = len(out)
        value_cols = [c for c in out.columns if c in _VALID_RANGES]
        if value_cols:
            out["_completeness"] = out[value_cols].notna().sum(axis=1)
            out = out.sort_values(["ts_code", "report_date", "_completeness"])
            out = out.drop_duplicates(subset=["ts_code", "report_date"], keep="last")
            out = out.drop(columns=["_completeness"])
        else:
            out = out.drop_duplicates(subset=["ts_code", "report_date"], keep="last")
        if len(out) < before:
            report["notes"]["duplicate_report_dropped"] = before - len(out)

    # ---- 4. TTM 勾稽（只记录不改） ----
    if {"eps", "eps_ttm"}.issubset(out.columns):
        eps = pd.to_numeric(out["eps"], errors="coerce")
        ttm = pd.to_numeric(out["eps_ttm"], errors="coerce")
        conflict = (np.sign(eps) != np.sign(ttm)) & eps.notna() & ttm.notna() & (eps != 0)
        n_conf = int(conflict.sum())
        if n_conf:
            report["notes"]["eps_ttm_sign_conflict"] = n_conf

    out = out.sort_values(["ts_code", "report_date"]).reset_index(drop=True)
    report["output_rows"] = len(out)

    if report["notes"]:
        logger.info(f"财务清洗标记: {report['notes']}")
    return out, report


if __name__ == "__main__":
    from src.layer1_data.fetcher.financial import fetch_financial

    raw = fetch_financial("600000", start_year="2021")
    cleaned, rep = clean_financial(raw)
    print("质量报告:", rep)
    print(cleaned.head(6).to_string())
