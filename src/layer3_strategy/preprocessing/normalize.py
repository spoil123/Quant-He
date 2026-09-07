# -*- coding: utf-8 -*-
"""
因子预处理：去极值 -> 标准化 -> 中性化。

三步各自解决什么问题：

1. MAD 去极值
   为什么不用 3σ：金融数据厚尾，极端值本身就会把 σ 撑大，导致真正的异常值
   "躲"进 3σ 范围内。MAD（中位数绝对偏差）用中位数做基准，不会被极端值带偏，
   对厚尾分布稳健。
       mad   = median(|x - median(x)|)
       bound = median(x) ± n × 1.4826 × mad     # 1.4826 让 MAD 在正态分布下与 σ 可比
   注意是「截断(clip)」而不是「删除」—— 删掉会破坏截面的股票池完整性。

2. Z-score 标准化
   不同因子量纲差几个数量级（换手率是百分之几，市值是几百亿），不能直接相加。
   转成标准差单位后才有可比性。

3. 行业 / 市值中性化
   不中性化的话，因子很可能只是"行业"或"小市值"的马甲。比如低估值因子天然
   重仓银行地产，你以为赚的是估值的钱，其实赌的是行业。
   做法：截面回归 factor ~ 行业哑变量 + ln(流通市值)，取残差作为纯净因子。
   残差的含义是"剥离掉行业和市值影响后，这只股票在同类中还剩多少超额特征"。

严格按交易日分组处理（groupby trade_date），绝不跨日期计算。
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.common.logger import logger

# MAD 换算成与标准差可比的系数
MAD_TO_SIGMA = 1.4826


# ---------------------------------------------------------------- 去极值

def winsorize(
    s: pd.Series,
    method: str = "mad",
    n: float = 5.0,
    q: Sequence[float] = (0.01, 0.99),
) -> pd.Series:
    """截面去极值（截断而非删除）。"""
    if s is None or len(s) == 0:
        return s

    x = pd.to_numeric(s, errors="coerce")
    valid = x.dropna()
    if len(valid) < 5:
        return x

    if method == "none":
        return x

    if method == "quantile":
        lo, hi = valid.quantile(q[0]), valid.quantile(q[1])
    elif method == "sigma":
        mu, sd = valid.mean(), valid.std()
        lo, hi = mu - n * sd, mu + n * sd
    else:  # mad，默认
        med = valid.median()
        mad = (valid - med).abs().median()
        span = n * MAD_TO_SIGMA * mad
        # 极端情况下 mad=0（超过半数取同一值），退化用分位数，避免上下界重合
        if mad == 0 or not np.isfinite(span):
            lo, hi = valid.quantile(0.01), valid.quantile(0.99)
        else:
            lo, hi = med - span, med + span

    return x.clip(lower=lo, upper=hi)


# ---------------------------------------------------------------- 标准化

def standardize(s: pd.Series, method: str = "zscore", clip: float = 3.0) -> pd.Series:
    """截面标准化。"""
    if s is None or len(s) == 0:
        return s

    x = pd.to_numeric(s, errors="coerce")
    valid = x.dropna()
    if len(valid) < 5:
        return pd.Series(np.nan, index=x.index)

    if method == "rank":
        out = x.rank(pct=True)
        return (out - 0.5) * 2          # 映射到 [-1, 1]
    if method == "minmax":
        lo, hi = valid.min(), valid.max()
        if hi == lo:
            return pd.Series(np.nan, index=x.index)
        return (x - lo) / (hi - lo)

    mu, sd = valid.mean(), valid.std()
    if sd == 0 or not np.isfinite(sd):
        return pd.Series(0.0, index=x.index)
    z = (x - mu) / sd
    return z.clip(-clip, clip) if clip else z


# ---------------------------------------------------------------- 中性化

def _build_dummies(labels: pd.Series, min_size: int = 5) -> pd.DataFrame:
    """行业哑变量。样本数少于 min_size 的行业并入 'OTHER'，避免过拟合小行业。"""
    lb = labels.fillna("UNKNOWN").astype(str)
    counts = lb.value_counts()
    small = set(counts[counts < min_size].index)
    if small:
        lb = lb.where(~lb.isin(small), "OTHER")
    d = pd.get_dummies(lb, prefix="IND", dtype=float)
    # 去掉一列避免完全共线性（截距项已包含）
    if d.shape[1] > 1:
        d = d.iloc[:, 1:]
    return d


def neutralize(
    df: pd.DataFrame,
    factor_col: str,
    industry_col: Optional[str] = None,
    mv_col: Optional[str] = None,
    method: str = "ols",
    min_samples: int = 30,
) -> pd.Series:
    """单期截面中性化：factor ~ 行业哑变量 + ln(市值)，返回残差。

    df 是某一个交易日的截面数据。整个函数不做跨日期操作。
    """
    y = pd.to_numeric(df[factor_col], errors="coerce")

    if method == "demean":
        # 组内去均值：轻量版，只剥离行业均值
        if industry_col and industry_col in df.columns:
            grp = df[industry_col].fillna("UNKNOWN")
            return y - y.groupby(grp).transform("mean")
        return y - y.mean()

    feats: List[pd.Series] = []
    names: List[str] = []

    if industry_col and industry_col in df.columns:
        dummies = _build_dummies(df[industry_col])
        for c in dummies.columns:
            feats.append(dummies[c])
            names.append(c)

    if mv_col and mv_col in df.columns:
        mv = pd.to_numeric(df[mv_col], errors="coerce")
        feats.append(np.log(mv.where(mv > 0)))
        names.append("ln_mv")

    if not feats:
        return y - y.mean()

    X = pd.concat(feats, axis=1)
    X.columns = names

    mask = y.notna() & X.notna().all(axis=1)
    if int(mask.sum()) < min_samples:
        # 样本太少，回归不可靠，退化为去均值
        return y - y.mean()

    Xm = X.loc[mask].to_numpy(dtype=float)
    ym = y.loc[mask].to_numpy(dtype=float)

    # 加截距
    Xm = np.column_stack([np.ones(len(Xm)), Xm])
    try:
        beta, *_ = np.linalg.lstsq(Xm, ym, rcond=None)
        fitted = Xm @ beta
        resid = pd.Series(np.nan, index=df.index, dtype=float)
        resid.loc[mask] = ym - fitted
    except np.linalg.LinAlgError:
        resid = y - y.mean()

    return resid


# ---------------------------------------------------------------- 全流程

def preprocess_panel(
    panel: pd.DataFrame,
    factor_col: str,
    date_col: str = "trade_date",
    industry_col: Optional[str] = None,
    mv_col: Optional[str] = None,
    winsorize_cfg: Optional[dict] = None,
    standardize_cfg: Optional[dict] = None,
    neutralize_cfg: Optional[dict] = None,
    min_cross_section: int = 30,
) -> pd.DataFrame:
    """对整个面板（多期）做预处理，逐交易日进行。

    在原 DataFrame 上新增列：
        {factor}_wins  去极值后
        {factor}_z     标准化后
        {factor}_neut  中性化后（最终因子值）
    """
    w_cfg = winsorize_cfg or {}
    s_cfg = standardize_cfg or {}
    n_cfg = neutralize_cfg or {}

    out = panel.copy()
    out[f"{factor_col}_wins"] = np.nan
    out[f"{factor_col}_z"] = np.nan
    out[f"{factor_col}_neut"] = np.nan

    if date_col not in out.columns:
        logger.warning(f"面板缺少 {date_col} 列，按整体单期处理")
        groups = [(None, out)]
    else:
        groups = list(out.groupby(date_col, sort=True))

    for _, g in groups:
        if len(g) < min_cross_section:
            continue
        idx = g.index

        s_w = winsorize(g[factor_col], method=w_cfg.get("method", "mad"),
                        n=w_cfg.get("n", 5.0))
        s_z = standardize(s_w, method=s_cfg.get("method", "zscore"),
                          clip=s_cfg.get("clip", 3.0))

        tmp = g.copy()
        tmp[f"{factor_col}_wins"] = s_w
        tmp[f"{factor_col}_z"] = s_z
        s_n = neutralize(
            tmp, f"{factor_col}_wins",
            industry_col=industry_col, mv_col=mv_col,
            method=n_cfg.get("method", "ols"),
        )

        out.loc[idx, f"{factor_col}_wins"] = s_w.values
        out.loc[idx, f"{factor_col}_z"] = s_z.values
        # 中性化后重新标准化一次，保证各因子量纲一致、可直接相加
        out.loc[idx, f"{factor_col}_neut"] = standardize(
            s_n, method=s_cfg.get("method", "zscore"), clip=s_cfg.get("clip", 3.0)
        ).values

    return out


if __name__ == "__main__":
    rng = np.random.default_rng(42)
    n = 300
    df = pd.DataFrame({
        "trade_date": ["2023-01-31"] * n,
        "ts_code": [f"{i:06d}" for i in range(n)],
        "raw": np.append(rng.normal(0, 1, n - 3), [80.0, -90.0, 150.0]),  # 3 个极端值
        "industry": rng.choice(["银行", "地产", "医药", "电子"], n),
        "float_mv": rng.lognormal(20, 1.2, n),
    })
    res = preprocess_panel(df, "raw", industry_col="industry", mv_col="float_mv")
    print(res[["raw", "raw_wins", "raw_z", "raw_neut"]].describe().round(3).to_string())
    print("\n极端值是否被压制（原始 150 -> 截后）:")
    print(res.nlargest(3, "raw")[["raw", "raw_wins", "raw_neut"]].to_string())
