# -*- coding: utf-8 -*-
"""
绩效指标计算（不依赖 backtrader，纯 pandas/numpy）。

年化基准：A 股一年约 252 个交易日。
无风险利率：默认 2%（接近国内货币基金/国债的长期中枢，可在调用时改）。

几个容易算错的口径，这里统一说明：
    年化收益率  用几何法 (终值/初值)^(252/天数) - 1，不是「日收益均值 × 252」。
                后者在波动大时会系统性高估。
    夏普比率    (年化收益 - 无风险) / 年化波动。波动用日收益标准差 × sqrt(252)。
    最大回撤    nav / nav.cummax() - 1 的最小值，是「从历史最高点回落的最大幅度」，
                不是「期末相对期初的跌幅」。
    超额收益    用净值比 (策略净值/基准净值) 计算，而不是简单相减 ——
                相减在复利下没有意义。
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DEFAULT_RF = 0.02


def to_series(nav) -> pd.Series:
    """统一成按日期排序的 float Series。"""
    if isinstance(nav, pd.Series):
        s = nav.copy()
    else:
        s = pd.Series(nav)
    s.index = pd.to_datetime(s.index)
    s = pd.to_numeric(s, errors="coerce").dropna()
    return s.sort_index()


def total_return(nav: pd.Series) -> float:
    s = to_series(nav)
    if len(s) < 2 or s.iloc[0] <= 0:
        return np.nan
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def annual_return(nav: pd.Series) -> float:
    """几何年化收益率（统一按 A 股 252 交易日口径）。

    与 annual_volatility / sharpe_ratio / 换手率年化一致，都用 TRADING_DAYS。
    之前用自然日 365 年化，与其余指标（252）口径分裂，导致非整年区间
    年化收益与年化波动不可比 —— 已统一为交易日口径。
    """
    s = to_series(nav)
    if len(s) < 2 or s.iloc[0] <= 0:
        return np.nan
    n = len(s)  # 交易日数
    if n <= 0:
        return np.nan
    years = n / float(TRADING_DAYS)
    return float((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0)


def annual_volatility(nav: pd.Series) -> float:
    s = to_series(nav)
    r = s.pct_change().dropna()
    if len(r) < 2:
        return np.nan
    return float(r.std() * np.sqrt(TRADING_DAYS))


def max_drawdown(nav: pd.Series) -> float:
    """最大回撤（正数表示回撤幅度，如 0.12 表示回撤 12%）。"""
    s = to_series(nav)
    if len(s) < 2:
        return np.nan
    peak = s.cummax()
    dd = s / peak - 1.0
    return float(-dd.min())


def drawdown_series(nav: pd.Series) -> pd.Series:
    s = to_series(nav)
    return s / s.cummax() - 1.0


def sharpe_ratio(nav: pd.Series, rf: float = DEFAULT_RF) -> float:
    s = to_series(nav)
    ar, vol = annual_return(s), annual_volatility(s)
    if not np.isfinite(ar) or not np.isfinite(vol) or vol == 0:
        return np.nan
    return float((ar - rf) / vol)


def sortino_ratio(nav: pd.Series, rf: float = DEFAULT_RF) -> float:
    """索提诺：只惩罚下行波动。"""
    s = to_series(nav)
    r = s.pct_change().dropna()
    if len(r) < 2:
        return np.nan
    downside = r[r < 0].std() * np.sqrt(TRADING_DAYS)
    if not np.isfinite(downside) or downside == 0:
        return np.nan
    return float((annual_return(s) - rf) / downside)


def calmar_ratio(nav: pd.Series) -> float:
    ar, mdd = annual_return(nav), max_drawdown(nav)
    if not np.isfinite(ar) or not np.isfinite(mdd) or mdd == 0:
        return np.nan
    return float(ar / mdd)


# ---------------------------------------------------------------- 相对基准

def excess_nav(nav: pd.Series, benchmark: pd.Series) -> pd.Series:
    """超额净值曲线 = 策略 / 基准（两者需在同一日期索引上对齐后重采样）。"""
    s, b = to_series(nav), to_series(benchmark)
    common = s.index.intersection(b.index)
    if len(common) < 2:
        return pd.Series(dtype=float)
    return (s.loc[common] / b.loc[common])


def beta_alpha(nav: pd.Series, benchmark: pd.Series,
               rf: float = DEFAULT_RF) -> tuple[float, float]:
    """返回 (beta, 年化 alpha)。"""
    s, b = to_series(nav), to_series(benchmark)
    common = s.index.intersection(b.index)
    if len(common) < 20:
        return np.nan, np.nan
    rs = s.loc[common].pct_change().dropna()
    rb = b.loc[common].pct_change().dropna()
    idx = rs.index.intersection(rb.index)
    if len(idx) < 20:
        return np.nan, np.nan
    cov = np.cov(rs.loc[idx], rb.loc[idx])
    var = rb.loc[idx].var()
    if var == 0:
        return np.nan, np.nan
    beta = float(cov[0, 1] / var)
    alpha_daily = rs.loc[idx].mean() - beta * rb.loc[idx].mean()
    alpha_annual = float(alpha_daily * TRADING_DAYS)
    return beta, alpha_annual


def tracking_error(nav: pd.Series, benchmark: pd.Series) -> float:
    s, b = to_series(nav), to_series(benchmark)
    common = s.index.intersection(b.index)
    if len(common) < 20:
        return np.nan
    rs = s.loc[common].pct_change().dropna()
    rb = b.loc[common].pct_change().dropna()
    idx = rs.index.intersection(rb.index)
    if len(idx) < 20:
        return np.nan
    return float((rs.loc[idx] - rb.loc[idx]).std() * np.sqrt(TRADING_DAYS))


def information_ratio(nav: pd.Series, benchmark: pd.Series) -> float:
    ex = excess_nav(nav, benchmark)
    if len(ex) < 2:
        return np.nan
    ar = annual_return(ex)
    te = tracking_error(nav, benchmark)
    if not np.isfinite(ar) or not np.isfinite(te) or te == 0:
        return np.nan
    return float(ar / te)


def win_rate(nav: pd.Series, benchmark: Optional[pd.Series] = None) -> Dict[str, float]:
    """胜率：日胜率 / 月胜率（相对基准时算超额胜率）。"""
    s = to_series(nav)
    r = s.pct_change().dropna()
    out = {}
    if benchmark is None:
        out["daily"] = float((r > 0).mean()) if len(r) else np.nan
        m = s.resample("ME").last().pct_change().dropna()
        out["monthly"] = float((m > 0).mean()) if len(m) else np.nan
        return out
    b = to_series(benchmark)
    common = s.index.intersection(b.index)
    rs = s.loc[common].pct_change().dropna()
    rb = b.loc[common].pct_change().dropna()
    idx = rs.index.intersection(rb.index)
    out["daily"] = float((rs.loc[idx] > rb.loc[idx]).mean()) if len(idx) else np.nan
    ms = s.loc[common].resample("ME").last().pct_change().dropna()
    mb = b.loc[common].resample("ME").last().pct_change().dropna()
    midx = ms.index.intersection(mb.index)
    out["monthly"] = float((ms.loc[midx] > mb.loc[midx]).mean()) if len(midx) else np.nan
    return out


# ---------------------------------------------------------------- 汇总

def calc_metrics(
    nav: pd.Series,
    benchmark: Optional[pd.Series] = None,
    rf: float = DEFAULT_RF,
    turnover: Optional[float] = None,
) -> Dict[str, float]:
    """一次性算出全部核心指标。"""
    s = to_series(nav)
    m: Dict[str, float] = {
        "起始日期": str(s.index[0].date()) if len(s) else "",
        "结束日期": str(s.index[-1].date()) if len(s) else "",
        "交易日数": int(len(s)),
        "总收益率": total_return(s),
        "年化收益率": annual_return(s),
        "年化波动率": annual_volatility(s),
        "夏普比率": sharpe_ratio(s, rf),
        "索提诺比率": sortino_ratio(s, rf),
        "最大回撤": max_drawdown(s),
        "卡玛比率": calmar_ratio(s),
    }

    if benchmark is not None:
        b = to_series(benchmark)
        m["基准年化收益率"] = annual_return(b)
        m["基准最大回撤"] = max_drawdown(b)
        ex_ar = annual_return(s) - annual_return(b)
        m["超额年化收益率"] = ex_ar
        m["信息比率"] = information_ratio(s, b)
        m["跟踪误差"] = tracking_error(s, b)
        beta, alpha = beta_alpha(s, b, rf)
        m["Beta"] = beta
        m["年化Alpha"] = alpha
        wr = win_rate(s, b)
        m["日胜率"] = wr.get("daily", np.nan)
        m["月胜率"] = wr.get("monthly", np.nan)

    if turnover is not None:
        m["年化双边换手率"] = float(turnover)

    return m


def format_metrics(m: Dict[str, float]) -> str:
    """把指标字典格式化成易读文本。"""
    pct_keys = {"总收益率", "年化收益率", "年化波动率", "最大回撤", "基准年化收益率",
                "基准最大回撤", "超额年化收益率", "跟踪误差", "年化Alpha"}
    lines = []
    for k, v in m.items():
        if isinstance(v, float):
            if k in pct_keys:
                lines.append(f"{k:16s} {v * 100:>10.2f}%")
            else:
                lines.append(f"{k:16s} {v:>11.4f}")
        else:
            lines.append(f"{k:16s} {str(v):>11s}")
    return "\n".join(lines)


if __name__ == "__main__":
    rng = np.random.default_rng(3)
    n = 252 * 3
    idx = pd.bdate_range("2021-01-04", periods=n)
    r = rng.normal(0.0005, 0.011, n)
    nav = pd.Series(1_000_000 * np.cumprod(1 + r), index=idx)
    bm_r = rng.normal(0.0002, 0.013, n)
    bm = pd.Series(3000 * np.cumprod(1 + bm_r), index=idx)

    m = calc_metrics(nav, bm, turnover=3.2)
    print(format_metrics(m))
