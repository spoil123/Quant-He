# -*- coding: utf-8 -*-
"""
impact_coef 实证回归（第 5 层执行层核心价值）。

目标：把 costs.yaml 里「经验值」的 impact_coef 变成「实证值」。
原材料：execution_trade 里的真实成交回报（委托价 vs 成交价）。

口径（与回测引擎保持一致，见 portfolio_backtest.py 的 liquidity 模型）：
    slip_bps = (deal_price - order_price) / order_price * 10000   # 买入取正
    participation = order_amount / (当日成交额 × participation_cap)
    模型: slip_bps = base_bps + impact_coef * sqrt(participation) * 100

回归：对 (participation, slip_bps) 拟合
    slip_bps = base + k * sqrt(participation)
    得 k = impact_coef × 100 → impact_coef = k / 100

注意：参与率需要「当日该股成交额」，从 daily_price 表（amount 列）关联取。
    成交额是金额（元），order_amount = deal_price × volume。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from src.common.db import read_sql
from src.common.logger import logger
from src.layer5_execution.trade_store import TradeStore

# 参与率公式里的分母系数（与 costs.yaml slippage.participation_cap 一致）
DEFAULT_PARTICIPATION_CAP = 0.10
DEFAULT_MIN_SAMPLES = 30


def _cfg_float(section: str, key: str, default: float) -> float:
    """从 execution.yaml 读参数，读不到用默认值。

    L3 修复（2026-09-07）：impact.min_samples / participation_cap 此前只有
    scripts/calibrate_impact.py 会读配置，本模块用硬编码常量 —— 改了 yaml
    只有一半代码认账，另一半悄悄用旧值，排查时极难发现。统一从这里读。
    """
    try:
        from src.common.config import get_config
        v = (get_config("execution").get(section, {}) or {}).get(key)
        return float(v) if v else float(default)
    except Exception:                                     # noqa: BLE001
        return float(default)

# 模拟撮合的 broker 标识（paper_trade_sim.py 造的样本，非真实成交回报）
_PAPER_BROKERS = {"paper", "sim", "simulated", "mock", "backtest", "sandbox"}


def sample_source(df: pd.DataFrame) -> Tuple[str, Dict[str, int]]:
    """判断回归样本来源 —— 决定结论能否回写 costs.yaml。

    返回 (来源, broker 分布)。来源取值：
      real      —— 全部是真实券商成交回报，可回写 costs.yaml
      simulated —— 全部是模拟撮合样本：只能证明回归链路能还原真值，
                   不等于实证冲击系数，回写会把造数时的设定又写回去（自我循环）
      mixed     —— 混合样本，回写时按最保守处理并告警
      unknown   —— 无 broker 列（老数据），按 unknown 处理

    为什么必须区分：模拟样本是用「设定的真值」生成的，回归它得到的系数
    是对已知真值的估计误差，不含任何真实市场冲击信息。把它写成"实证值"
    是自欺欺人 —— 配置里躺着一个看起来更精确（0.968）实则和 1.0 无差别
    的数，还会让人误以为已经完成实证校准。
    """
    if "broker" not in df.columns:
        return "unknown", {}

    counts: Dict[str, int] = {}
    for k, v in df["broker"].fillna("").astype(str).value_counts().items():
        counts[k or "(空)"] = int(v)
    if not counts:
        return "unknown", {}

    paper = {k: v for k, v in counts.items() if k.strip().lower() in _PAPER_BROKERS}
    if len(paper) == len(counts):
        return "simulated", counts
    if not paper:
        return "real", counts
    return "mixed", counts


def load_trades_with_amount(
    store: TradeStore,
    start: str | None = None,
    end: str | None = None,
    participation_cap: float | None = None,
) -> pd.DataFrame:
    """成交回报 + 当日成交额（关联 daily_price），算 participation。

    返回列：ts_code / trade_date / side / order_price / deal_price /
            volume / deal_amount / broker / amount(当日成交额) / participation

    broker 保留下来供 sample_source() 判断样本是真实成交还是模拟撮合。
    """
    if participation_cap is None:
        participation_cap = _cfg_float("impact", "participation_cap",
                                       DEFAULT_PARTICIPATION_CAP)
    df = store.load_trades(start=start, end=end)
    if df.empty:
        return df

    # 关联当日成交额：daily_price 存的是不复权(none)口径，金额与复权无关。
    # 按 trades 的日期范围下推，避免千万级全表扫描（分区表逐年分区）。
    where_amt = "adj_type = 'none'"
    amt_params: Dict = {}
    if not df.empty:
        dmin = df["trade_date"].min()
        dmax = df["trade_date"].max()
        if pd.notna(dmin) and pd.notna(dmax):
            where_amt += " AND trade_date BETWEEN :s AND :e"
            amt_params = {"s": str(dmin)[:10], "e": str(dmax)[:10]}
    amounts = read_sql(
        f"""SELECT trade_date, ts_code, amount
            FROM daily_price
            WHERE {where_amt}""",
        amt_params,
    )
    if not amounts.empty:
        amounts["trade_date"] = pd.to_datetime(amounts["trade_date"]).dt.date
        amounts["amount"] = pd.to_numeric(amounts["amount"], errors="coerce")
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.merge(amounts, on=["trade_date", "ts_code"], how="left")

    # participation = order_amount / (当日成交额 × cap)
    df["order_amount"] = df["deal_price"] * df["volume"]
    df["participation"] = df["order_amount"] / (df["amount"] * participation_cap)
    df = df[df["participation"].between(0, 1)]      # 排除异常（成交额缺失/为 0）
    return df


def compute_slip_bps(df: pd.DataFrame) -> pd.DataFrame:
    """委托价 -> 成交价的滑点（bps）。

    买入：成交价高于委托价 = 正滑点（冲击成本）
    卖出：成交价低于委托价 = 正滑点
    """
    out = df.copy()
    out["slip_bps"] = np.nan
    valid = out["order_price"].notna() & (out["order_price"] > 0)
    out.loc[valid, "slip_bps"] = (
        (out.loc[valid, "deal_price"] - out.loc[valid, "order_price"])
        / out.loc[valid, "order_price"] * 10000
    )
    # 卖出方向取反：成交更低 = 成本
    sell = out["side"] == "sell"
    out.loc[valid & sell, "slip_bps"] = -out.loc[valid & sell, "slip_bps"]
    return out


def regress_impact(
    df: pd.DataFrame,
    min_samples: int | None = None,
) -> Dict[str, Optional[float]]:
    """拟合 slip_bps ~ base + k*sqrt(participation)，返回 impact_coef。

    模型（与回测口径一致）：
        slip_bps = base_bps + impact_coef * sqrt(participation) * 100
    回归得 k = impact_coef × 100，故 impact_coef = k / 100。
    """
    if min_samples is None:
        min_samples = int(_cfg_float("impact", "min_samples",
                                     DEFAULT_MIN_SAMPLES))
    d = df.dropna(subset=["slip_bps", "participation"])
    d = d[np.isfinite(d["slip_bps"]) & np.isfinite(d["participation"])]
    if len(d) < min_samples:
        logger.warning(
            f"有效样本不足 {min_samples}（实际 {len(d)}），无法回归 —— 继续累积成交记录"
        )
        return {"n": len(d), "impact_coef": None, "base_bps": None,
                "r_squared": None, "method": None}

    x = np.sqrt(d["participation"].to_numpy(float))
    y = d["slip_bps"].to_numpy(float)
    # 带截距最小二乘：y = base + k*x
    A = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    base_bps, k = float(beta[0]), float(beta[1])

    # R²
    yhat = A @ beta
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan

    impact_coef = k / 100.0
    logger.info(
        f"impact 回归: n={len(d)}  base_bps={base_bps:.2f}  "
        f"impact_coef={impact_coef:.4f}  R²={r2:.3f}"
    )
    return {
        "n": len(d), "impact_coef": impact_coef, "base_bps": base_bps,
        "r_squared": r2, "method": "ols",
    }


def summarize(df: pd.DataFrame) -> Dict:
    """样本概览（未做回归时的诊断信息）。"""
    d = df.dropna(subset=["slip_bps"])
    out = {
        "n_trades": len(df),
        "n_with_slip": len(d),
        "mean_slip_bps": float(d["slip_bps"].mean()) if len(d) else None,
        "median_slip_bps": float(d["slip_bps"].median()) if len(d) else None,
        "p95_slip_bps": float(d["slip_bps"].quantile(0.95)) if len(d) else None,
        "mean_participation": float(d["participation"].mean()) if len(d) else None,
    }
    src, dist = sample_source(df)
    out["sample_source"] = src          # real / simulated / mixed / unknown
    out["broker_dist"] = dist
    if len(d):
        by_side = d.groupby("side")["slip_bps"].agg(["mean", "count"])
        out["by_side"] = by_side.to_dict("index")   # {'buy': {'mean':..,'count':..}, ...}
    return out


if __name__ == "__main__":
    # 冒烟：无真实成交时给出明确提示
    store = TradeStore()
    df = store.load_trades()
    if df.empty:
        print("当前无成交记录 —— 先跑 scripts/paper_trade_sim.py 造样本，"
              "或接入 QMT 模拟盘后自然积累")
    else:
        print(summarize(compute_slip_bps(load_trades_with_amount(store))))
