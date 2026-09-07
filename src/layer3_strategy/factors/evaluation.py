# -*- coding: utf-8 -*-
"""
因子有效性检验。

三个核心指标：
    IC   信息系数：因子值与未来收益的相关系数。用 Spearman 秩相关，
                  因为因子经过标准化后只有排序有意义，且秩相关不受极端值影响。
                  IC > 0.02 就算有效，> 0.05 相当不错。
    IR   信息比率：IC 均值 / IC 标准差。衡量 IC 的稳定性。
                  IR > 0.5 说明因子稳定有效。
    分层收益：按因子值分 N 组，看各组收益是否单调。
                  单调性比 IC 均值更重要 —— 单调说明因子在整个横截面上都有区分度，
                  而不是只靠头尾几只股票撑起来。

注意：这些指标只能用「已实现的未来收益」计算，属于评估环节。
      生成交易信号时绝不能引用未来收益列。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.common.logger import logger


def calc_ic(
    panel: pd.DataFrame,
    factor_col: str,
    fwd_col: str = "fwd_ret_20d",
    method: str = "spearman",
    min_samples: int = 30,
) -> pd.Series:
    """逐交易日计算 IC，返回以 trade_date 为索引的 Series。"""
    if fwd_col not in panel.columns:
        raise KeyError(f"缺少未来收益列 {fwd_col}（评估专用，不要用于生成信号）")

    sub = panel[["trade_date", factor_col, fwd_col]].dropna()

    def _one(g: pd.DataFrame) -> float:
        if len(g) < min_samples:
            return np.nan
        return g[factor_col].corr(g[fwd_col], method=method)

    ic = sub.groupby("trade_date").apply(_one, include_groups=False)
    return ic.dropna()


def ic_summary(ic: pd.Series) -> Dict[str, float]:
    """IC 统计摘要。"""
    if ic is None or len(ic) == 0:
        return {"ic_mean": np.nan, "ic_std": np.nan, "ir": np.nan,
                "ic_positive_ratio": np.nan, "t_stat": np.nan, "periods": 0}
    mean, std = ic.mean(), ic.std()
    n = len(ic)
    ir = mean / std if std and np.isfinite(std) and std > 0 else np.nan
    t = mean / (std / np.sqrt(n)) if std and np.isfinite(std) and std > 0 else np.nan
    return {
        "ic_mean": float(mean),
        "ic_std": float(std),
        "ir": float(ir) if np.isfinite(ir) else np.nan,
        "ic_positive_ratio": float((ic > 0).mean()),
        "t_stat": float(t) if np.isfinite(t) else np.nan,
        "periods": int(n),
    }


def quantile_returns(
    panel: pd.DataFrame,
    factor_col: str,
    fwd_col: str = "fwd_ret_20d",
    n_groups: int = 5,
    min_samples: int = 30,
) -> pd.DataFrame:
    """分层回测：按因子值分 N 组，返回各组平均未来收益。"""
    if fwd_col not in panel.columns:
        raise KeyError(f"缺少未来收益列 {fwd_col}")

    sub = panel[["trade_date", factor_col, fwd_col]].dropna().copy()

    def _grp(g: pd.DataFrame, date) -> pd.DataFrame:
        if len(g) < min_samples * n_groups // 2:
            return pd.DataFrame()
        # 因子值越大越优 -> 组号最大的是最高分组
        g["group"] = pd.qcut(g[factor_col].rank(method="first"),
                             n_groups, labels=range(1, n_groups + 1))
        return g.groupby("group")[fwd_col].mean().to_frame().T.assign(trade_date=date)

    # 注意：groupby 得到的 g 是 DataFrame，没有 .name（只有 Series 有），
    # 日期必须作为显式参数传进去
    parts = [p for p in (_grp(g, d) for d, g in sub.groupby("trade_date")) if not p.empty]
    if not parts:
        return pd.DataFrame()

    daily = pd.concat(parts, ignore_index=True)
    grp_cols = [c for c in daily.columns if c != "trade_date"]
    summary = daily[grp_cols].mean()
    out = pd.DataFrame({
        "group": [f"Q{int(g)}" for g in summary.index],
        "mean_return": summary.values,
    })
    out["annualized"] = out["mean_return"] * 12      # 月度收益年化的粗略换算
    return out


def evaluate_factors(
    panel: pd.DataFrame,
    factor_cols: Sequence[str],
    fwd_col: str = "fwd_ret_20d",
    n_groups: int = 5,
) -> pd.DataFrame:
    """对一组因子做完整评估，返回汇总表。"""
    rows = []
    for col in factor_cols:
        if col not in panel.columns:
            continue
        ic = calc_ic(panel, col, fwd_col)
        s = ic_summary(ic)
        qr = quantile_returns(panel, col, fwd_col, n_groups)
        mono = np.nan
        if not qr.empty and len(qr) == n_groups:
            # 单调性：分组收益与组号的秩相关
            mono = float(pd.Series(qr["mean_return"].values).corr(
                pd.Series(range(len(qr))), method="spearman"))
        rows.append({
            "factor": col,
            "IC均值": s["ic_mean"],
            "IC标准差": s["ic_std"],
            "IR": s["ir"],
            "IC为正比例": s["ic_positive_ratio"],
            "t值": s["t_stat"],
            "期数": s["periods"],
            "分层单调性": mono,
            "多头超额(Q高-Q低)": (float(qr["mean_return"].iloc[-1] - qr["mean_return"].iloc[0])
                              if not qr.empty else np.nan),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        logger.info(f"\n因子评估结果（未来收益口径: {fwd_col}）:\n{df.round(4).to_string(index=False)}")
    return df


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    n_dates, n_stocks = 24, 200
    rows = []
    for i in range(n_dates):
        d = pd.Timestamp("2022-01-31") + pd.DateOffset(months=i)
        f = rng.normal(0, 1, n_stocks)                       # 因子值
        ret = 0.003 * f + rng.normal(0, 0.05, n_stocks)      # 带微弱预测力的收益
        rows.append(pd.DataFrame({
            "trade_date": d.date(), "ts_code": [f"{j:06d}" for j in range(n_stocks)],
            "factor": f, "fwd_ret_20d": ret}))
    p = pd.concat(rows, ignore_index=True)
    print(evaluate_factors(p, ["factor"]).round(4).to_string(index=False))
