# -*- coding: utf-8 -*-
"""
第 4 层：组合风险度量（回测后）。

对一条净值曲线（或日收益序列）做风险管理视角的体检：
    VaR / CVaR      历史模拟法，组合单日最大可能损失
    最大回撤/卡玛    与 metrics.py 口径一致（252 交易日）
    压力测试         给定极端情景，估算组合损失
    回撤止损模拟     组合回撤超过阈值时降仓，输出"风控后"净值

设计要点：
    1. 一切输入都是净值序列（回测器产物），不依赖具体策略实现，可插拔。
    2. VaR/CVaR 用历史模拟（真实收益分布），不假设正态 —— 金融收益厚尾，
       正态假设会系统性低估尾部风险。
    3. 回撤止损模拟是"事后近似"：真实盘中止损有滑点与无法成交的问题，
       这里给出理想化结果（阈值触发次日按收盘降仓），用于评估规则价值。
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.layer2_backtest.metrics import (
    TRADING_DAYS,
    annual_return,
    annual_volatility,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    to_series,
)


class RiskMetrics:
    """组合风险度量。输入净值曲线，输出风险指标与压力测试。"""

    def __init__(self, nav: pd.Series):
        self.nav = to_series(nav)
        if len(self.nav) < 5:
            raise ValueError(f"净值序列过短（{len(self.nav)}），无法计算风险指标")
        self.ret = self.nav.pct_change().dropna()

    # ================================================================ 尾部风险

    def var_cvar(self, level: float = 0.95) -> Dict[str, float]:
        """历史模拟 VaR / CVaR（单日，正数表示损失）。

        VaR(95%)  = 收益分布 5% 分位数的绝对值：100 个交易日里约 5 天
                    损失会超过这个数。
        CVaR(95%) = 损失超过 VaR 的那些天的平均损失（更保守）。
        """
        q = 1.0 - level
        r = self.ret.dropna().to_numpy()
        if len(r) < 20:
            return {"var": np.nan, "cvar": np.nan}
        var = float(-np.percentile(r, q * 100))
        tail = r[r <= -var]
        cvar = float(-tail.mean()) if len(tail) > 0 else var
        return {"var": round(var, 6), "cvar": round(cvar, 6)}

    def annualized_var(self, level: float = 0.95, days: int = 20) -> Dict[str, float]:
        """按持有期放大 VaR/CVaR（√t 缩放，假设 iid）。"""
        v = self.var_cvar(level)
        scale = np.sqrt(days)
        return {
            f"var_{days}d": round(v["var"] * scale, 6),
            f"cvar_{days}d": round(v["cvar"] * scale, 6),
        }

    # ================================================================ 汇总

    def summary(self) -> Dict[str, float]:
        """常规绩效+风险汇总（口径与 layer2 metrics 一致）。"""
        m = {
            "总收益率": float(self.nav.iloc[-1] / self.nav.iloc[0] - 1.0),
            "年化收益率": annual_return(self.nav),
            "年化波动率": annual_volatility(self.nav),
            "夏普比率": sharpe_ratio(self.nav),
            "索提诺比率": sortino_ratio(self.nav),
            "最大回撤": max_drawdown(self.nav),
            "卡玛比率": self._calmar(),
        }
        m.update(self.var_cvar(0.95))
        m.update(self.var_cvar(0.99))
        return {k: round(v, 6) if isinstance(v, float) else v for k, v in m.items()}

    def _calmar(self) -> float:
        ar = annual_return(self.nav)
        mdd = max_drawdown(self.nav)
        return float(ar / mdd) if mdd and mdd > 1e-10 else np.nan

    # ================================================================ 压力测试

    def stress_test(self, scenarios: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """情景压力测试：给定单日组合跌幅，估算对净值的冲击与恢复天数。

        注意：这是"组合 beta 已隐含在净值里"的直接冲击，不假设个股持仓。
        """
        sc = scenarios or {
            "大盘单日-3%": -0.03,
            "大盘单日-5%": -0.05,
            "连续5日每日-2%": None,  # 特殊处理
        }
        out: Dict[str, float] = {}
        for name, shock in sc.items():
            if shock is None:
                cum = (1 - 0.02) ** 5 - 1.0
                out[f"{name} 冲击"] = round(cum, 6)
            else:
                out[f"{name} 冲击"] = round(shock, 6)
            # 恢复所需天数：按年化波动估算的日均收益反推（简化）
            sigma_d = annual_volatility(self.nav) / np.sqrt(TRADING_DAYS)
            loss = abs(shock) if shock is not None else abs((1 - 0.02) ** 5 - 1.0)
            recover = int(np.ceil(loss / max(sigma_d, 1e-6))) if sigma_d > 0 else np.nan
            out[f"{name} 恢复天数(估)"] = recover
        return out

    # ================================================================ 回撤止损模拟

    def drawdown_stop_curve(
        self,
        trigger: float = -0.12,
        reduce_to: float = 0.5,
        cooldown_days: int = 20,
        recovery_ratio: float = 0.5,
    ) -> pd.Series:
        """组合级回撤熔断模拟。

        净值从历史最高回撤达到 trigger → 次日按 reduce_to 降仓（净值的
        波动也同步缩小到 reduce_to 倍）；冷却 cooldown_days 天后，若回撤
        已恢复到 recovery_ratio×|trigger| 以内则解除，否则继续降仓。

        简化假设：降仓期间收益 = reduce_to × 原收益（现金部分无收益）。
        """
        nav = self.nav.copy()
        peak = nav.cummax()
        dd = nav / peak - 1.0

        out = nav.copy()
        scale = 1.0           # 当前仓位缩放
        frozen = 0            # 冷却期剩余交易日
        trigger_abs = abs(trigger)

        for i in range(1, len(nav)):
            d_now = dd.iloc[i]
            if frozen > 0:
                frozen -= 1
            elif scale < 1.0 and d_now > -trigger_abs * recovery_ratio:
                scale = 1.0   # 回撤收窄，解除熔断
            elif d_now <= trigger:
                scale = reduce_to
                frozen = cooldown_days

            r_today = self.ret.iloc[i - 1]   # ret 比 nav 少一行（pct_change 首行 NaN）
            out.iloc[i] = out.iloc[i - 1] * (1.0 + scale * r_today)

        out.name = "equity_after_stop"
        return out

    def report(self, scenarios: Optional[Dict[str, float]] = None) -> pd.DataFrame:
        """一键输出风控报告（DataFrame，便于打印/落盘）。"""
        rows = []
        for k, v in self.summary().items():
            rows.append(("风险度量", k, v))
        for k, v in self.stress_test(scenarios).items():
            rows.append(("压力测试", k, v))
        return pd.DataFrame(rows, columns=["类别", "指标", "数值"])


if __name__ == "__main__":
    # 自测：合成一条有回撤的净值
    rng = np.random.default_rng(1)
    r = rng.normal(0.0003, 0.012, 1200)
    r[600:650] -= 0.03          # 人为制造一段回撤
    nav = (1.0 + r).cumprod()
    nav.index = pd.bdate_range("2018-01-02", periods=len(nav))
    rm = RiskMetrics(nav)
    print(rm.report().to_string(index=False))
