# -*- coding: utf-8 -*-
"""
多标的组合回测器（第 3 层核心）。

在单标的 Backtester 的思路上扩展到组合维度，但仍然是**向量化**的：
    1. 调仓日（每月末）按综合因子分选 Top N，按流通市值加权算出目标权重
    2. 目标权重在两次调仓之间保持不变（前向填充），形成「日期 × 股票」持仓矩阵
    3. 日收益用矩阵一次算完：
           无调仓日：ret = Σ w × (close/close_prev - 1)
           调仓日  ：ret = Σ [w_prev ×(exec/close_prev-1) + w_new ×(close/exec-1)]
                         - Σ|Δw| × 费率
    4. exec 按每只股票自身的买卖方向 ± 滑点（买入上滑、卖下滑）

内存优化：
    5912 只 × 2800 天 ≈ 1655 万个单元格，直接建全套矩阵要 1GB+。
    这里只对「曾经进入过持仓池」的股票建矩阵 —— 实际通常只有几百到一两千只，
    内存降到十分之一以下。

与单标的 Backtester 的关系：
    撮合口径、成本模型、T+1 规则完全一致，所以两边算出来的东西可以对得上。
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.common.config import get_config
from src.common.logger import logger
from src.layer2_backtest.backtester import STAMP_DUTY_SCHEDULE
from src.layer2_backtest.metrics import (
    annual_return,
    annual_volatility,
    beta_alpha,
    information_ratio,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    tracking_error,
)

TRADING_DAYS = 252


def _cost_defaults() -> Dict[str, float]:
    """成本默认值单一事实源：config/costs.yaml。

    修复：此前回测器全用硬编码（万3佣金/10bps滑点），costs.yaml 是摆设，
    改配置想调成本等于没改。现在默认值统一从 costs.yaml 读取，
    显式传入的参数仍可覆盖（测试/敏感性分析用）。
    """
    try:
        c = get_config("costs")
        slip = c.get("slippage", {}) or {}
        return {
            "commission": float(c["commission"]["rate"]),
            "min_commission": float(c["commission"]["min_fee"]),
            "transfer_fee": float(c["transfer_fee"]["rate"]),
            "stamp_duty": float(c["stamp_tax"]["rate"]),
            "slip_bps": float(slip.get("base_bps", 2.0)),
            "slip_model": str(slip.get("model", "fixed")),
            "impact_coef": float(slip.get("impact_coef", 15.0)),
            "participation_cap": float(slip.get("participation_cap", 0.10)),
            "slip_max_bps": float(slip.get("max_bps", 60.0)),
            "slip_min_bps": float(slip.get("min_bps", 1.0)),
        }
    except Exception:                                     # noqa: BLE001
        # costs.yaml 缺失/损坏时退回保守默认，不让回测器因此不可用
        return {"commission": 0.0003, "min_commission": 5.0,
                "transfer_fee": 1e-5, "stamp_duty": 0.001, "slip_bps": 10.0,
                "slip_model": "fixed", "impact_coef": 15.0,
                "participation_cap": 0.10, "slip_max_bps": 60.0,
                "slip_min_bps": 1.0}


def cost_tier_config(tier: str | None = None) -> Dict[str, object]:
    """读取成本档位配置（P0-3）。

    tier 为 None 时用 costs.yaml tiers.default（正式绩效=保守档）。
    返回 {"tier": 档位名, "slip_multiplier": float, "label": str}。
    """
    try:
        c = get_config("costs")
        tiers = c.get("tiers", {}) or {}
        name = tier or tiers.get("default", "conservative")
        cfg = tiers.get(name) or {}
        return {
            "tier": name,
            "slip_multiplier": float(cfg.get("slip_multiplier", 1.5)),
            "label": str(cfg.get("label", f"{name}档")),
        }
    except Exception:                                     # noqa: BLE001
        return {"tier": "conservative", "slip_multiplier": 1.5,
                "label": "保守档（滑点×1.5）"}


_COST = _cost_defaults()


class PortfolioBacktester:
    """月度调仓的多因子组合回测器。"""

    def __init__(
        self,
        initial_capital: float = 10_000_000,
        commission: float = _COST["commission"],
        min_commission: float = _COST["min_commission"],
        transfer_fee: float = _COST["transfer_fee"],
        stamp_duty: float = _COST["stamp_duty"],
        slip_bps: float = _COST["slip_bps"],
        slip_model: str = _COST["slip_model"],
        impact_coef: float = _COST["impact_coef"],
        participation_cap: float = _COST["participation_cap"],
        slip_max_bps: float = _COST["slip_max_bps"],
        slip_min_bps: float = _COST["slip_min_bps"],
        min_fee_floor: float = 10_000.0,
        use_historical_stamp: bool = True,
        cost_tier: str | None = None,
    ):
        self.initial_capital = float(initial_capital)
        self.commission = float(commission)
        self.min_commission = float(min_commission)
        self.transfer_fee = float(transfer_fee)
        self.stamp_duty = float(stamp_duty)
        self.slip = float(slip_bps) / 1e4
        # 动态滑点模型（costs.yaml: model=liquidity）
        # slip_bps = base + impact_coef * sqrt(participation) * 100
        # participation = order_value / (daily_amount * cap)
        self.slip_model = str(slip_model)
        self.impact_coef = float(impact_coef)
        self.participation_cap = float(participation_cap)
        self.slip_max_bps = float(slip_max_bps)
        self.slip_min_bps = float(slip_min_bps)
        # P0-3 成本档位：正式绩效用保守档（滑点 × slip_multiplier，默认 1.5），
        # 覆盖 impact_coef 未实证的误差。此档位会写入 metrics 标注。
        _tier = cost_tier_config(cost_tier)
        self.cost_tier = str(_tier["tier"])
        self.cost_tier_label = str(_tier["label"])
        self.slip_multiplier = float(_tier["slip_multiplier"])
        # 最低佣金豁免阈值：单只成交额低于该值只收比例佣金，不强制最低 5 元。
        # 修复：组合调仓中权重微调（成交额几千块）也强收 5 元，小额成本占比畸高
        self.min_fee_floor = float(min_fee_floor)
        self.use_historical_stamp = bool(use_historical_stamp)

    # ================================================================ 选股权重

    def build_weights(
        self,
        panel: pd.DataFrame,
        rebalance_dates: List,
        score_col: str = "composite_score",
        top_n: int = 50,
        weighting: str = "float_mv",
        max_weight: float = 0.05,
        min_weight: float = 0.002,
        mv_col: str = "float_mv",
        exclude_st: bool = True,
        exclude_suspended: bool = True,
        exclude_limit_up: bool = True,
        min_avg_amount: float = 10_000_000,
        min_price: float = 1.0,
        amount_col: str = "amount",
        price_col: str = "close",
        # P0-4 成交量约束：目标额 ≤ 20日均成交额 × 参与率，超限缩权/剔除
        liquidity_participation: float = 0.10,
        liquidity_cap: bool = True,
    ) -> pd.DataFrame:
        """在每个调仓日选股并计算权重。

        返回 DataFrame: trade_date / ts_code / weight（只在调仓日有行）
        """
        need = {score_col}
        if weighting in ("float_mv", "sqrt_mv", "total_mv"):
            need.add(mv_col)
        if min_avg_amount and amount_col:
            need.add(amount_col)

        # ---- 未来函数硬拦截（机制，不靠自觉）----
        fwd_cols = [c for c in panel.columns if str(c).startswith("fwd_ret_")]
        if fwd_cols:
            raise ValueError(
                f"面板包含未来收益列 {fwd_cols} —— 严禁用于选股/回测！"
                "add_forward_returns 只允许在因子评估脚本中使用。"
            )

        sub = panel[panel["trade_date"].isin(rebalance_dates)].copy()
        if sub.empty:
            logger.warning("没有落在调仓日上的数据")
            return pd.DataFrame(columns=["trade_date", "ts_code", "weight"])

        # ---- 股票池过滤 ----
        # ST 过滤（H4）：优先用「逐日 status 标记」（清洗层按当日名称打标，
        # status=2 ST / 3 退市整理，无前视）；历史段未打标时退回 stock_basic
        # 的当前 is_st 快照（近似 —— 数据源不提供历史 ST 名称序列，属已知局限，
        # 会把"今天才 ST"的股票整段历史剔除，方向是保守剔除而非虚增收益）。
        if exclude_st:
            if "status" in sub.columns and sub["status"].fillna(1).isin([2, 3]).any():
                sub = sub[~sub["status"].fillna(1).isin([2, 3])]
            elif "is_st" in sub.columns:
                sub = sub[sub["is_st"].fillna(0) == 0]
        if exclude_suspended and "is_suspended" in sub.columns:
            sub = sub[sub["is_suspended"].fillna(0) == 0]
        if exclude_limit_up and "is_limit_up" in sub.columns:
            # 涨停买不进，调仓日剔除
            sub = sub[sub["is_limit_up"].fillna(0) == 0]
        if min_price and price_col in sub.columns:
            sub = sub[pd.to_numeric(sub[price_col], errors="coerce") >= min_price]
        if amount_col in panel.columns:
            # 20 日日均成交额：P0-4 成交量约束的基准，与 min_avg_amount 过滤解耦。
            # 关键：rolling 必须在「全量日线」上算，再按调仓日取值。
            # 若在只有调仓日的子集上算，调仓日稀疏（一年仅 12 个），
            # min_periods=10 要攒 10 个调仓日（约 10 个月）才非空，
            # 导致回测前 10 个月全部被误过滤 → 组合长期空仓。
            # 解耦原因（2026-08-31 P0-4 修复）：此前 _amt20 挂在 min_avg_amount
            # 分支下，min_avg_amount=0（关闭过滤）时成交量约束也随之静默失效，
            # 等于「实盘买不到的量」又回来了。现在只要有 amount 就必算 _amt20，
            # 过滤和约束各自独立判断。
            amt20 = panel.groupby("ts_code", sort=False)[amount_col].transform(
                lambda x: x.rolling(20, min_periods=10).mean()
            )
            sub = sub.copy()
            sub["_amt20"] = amt20.reindex(sub.index)
            if min_avg_amount:
                sub = sub[sub["_amt20"].fillna(0.0) >= min_avg_amount]
            # P0-4：_amt20 保留到选股后做成交量约束，不再提前 drop

        sub = sub.dropna(subset=[score_col])

        # ---- 逐调仓日选 Top N 并加权 ----
        # M2 修复（2026-09-07）：成交量约束的分母必须是「当期组合规模」，
        # 不能是初始本金。组合从 1000 万涨到 2000 万后，同样的权重对应的真实
        # 买入金额翻倍，但用 initial_capital 算出的上限纹丝不动 —— 约束随时间
        # 系统性失效，越到后期越松，等于越涨越允许买入超出承接能力的量。
        # 这里用组合自身累计毛净值近似当期规模：等权持有上期 Top N，按
        # [上期调仓日 -> 本期调仓日] 的涨跌幅累乘（与 run() 的动态 nav_base
        # 同一口径）。
        nav_est = 1.0
        prev_date = None
        prev_picks: List[str] = []
        close_pv = None
        if price_col in panel.columns:
            try:
                # 只取调仓日做透视，避免对全量日线 pivot（大面板会爆内存）
                close_pv = (panel[panel["trade_date"].isin(rebalance_dates)]
                            .pivot_table(index="trade_date", columns="ts_code",
                                         values=price_col, aggfunc="last")
                            .sort_index())
            except Exception as e:                       # noqa: BLE001
                logger.debug(f"价格透视表构建失败，成交量约束退回固定本金: {e}")
                close_pv = None

        rows = []
        for d, g in sub.groupby("trade_date", sort=True):
            # 用上期持仓在本期的收益更新组合规模估计
            if prev_date is not None and prev_picks and close_pv is not None:
                try:
                    if prev_date in close_pv.index and d in close_pv.index:
                        p0 = close_pv.loc[prev_date].reindex(prev_picks)
                        p1 = close_pv.loc[d].reindex(prev_picks)
                        r = (p1 / p0 - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
                        if not r.empty:
                            nav_est = max(nav_est * float(1.0 + r.mean()), 1e-6)
                except (KeyError, TypeError, ValueError):
                    pass
            prev_date = d
            if len(g) < top_n:
                logger.debug(f"{d} 候选不足 {top_n} 只（{len(g)}），按实际数量建仓")
            top = g.nlargest(min(top_n, len(g)), score_col)
            if top.empty:
                continue

            if weighting in ("float_mv", "total_mv") and mv_col in top.columns:
                w = pd.to_numeric(top[mv_col], errors="coerce").fillna(0.0)
            elif weighting == "sqrt_mv" and mv_col in top.columns:
                w = np.sqrt(pd.to_numeric(top[mv_col], errors="coerce").fillna(0.0))
            elif weighting == "score":
                w = pd.to_numeric(top[score_col], errors="coerce").fillna(0.0).clip(lower=0)
            else:  # equal
                w = pd.Series(1.0, index=top.index)

            total = w.sum()
            if total <= 0:
                w = pd.Series(1.0 / len(top), index=top.index)
            else:
                w = w / total

            # 单票上限：超出部分按比例再分配给未超限的（迭代几轮收敛）
            w = self._cap_weights(w, max_weight)
            w = w[w >= min_weight]
            if w.sum() > 0:
                w = w / w.sum()

            # ---- P0-4 成交量约束：目标额 ≤ 20日均成交额 × 参与率 ----
            # 实盘纪律：单票目标金额 = weight × 组合规模，若超过该股 20 日均
            # 成交额 × 参与率（默认 10%），说明「权重表里有实盘买不到的量」——
            # 买入即冲击成本爆表，回测收益不可实现。超限缩权，缩后低于
            # min_weight 则剔除，避免出现小数点权重的伪持仓。
            # 注意：缩权后【不再归一化】—— 买不到的钱留现金，而不是重新
            # 摊给其他股票（否则再归一化会把缩掉的权重又顶回来，约束失效）。
            if liquidity_cap and "_amt20" in top.columns:
                cap_notional = (pd.to_numeric(top["_amt20"], errors="coerce")
                                * liquidity_participation)
                # 分母 = 当期组合规模（初始本金 × 累计毛净值），不是固定本金
                max_w = cap_notional / (self.initial_capital * nav_est)
                max_w = max_w.reindex(w.index).fillna(0.0)
                over = w > max_w
                n_over = int(over.sum())
                if n_over:
                    logger.debug(
                        f"{d}: {n_over} 只超成交量上限（目标额 > 20日均额×{liquidity_participation:.0%}）"
                    )
                w = w.clip(upper=max_w)
                keep = (w >= min_weight) & (w > 0)
                if not keep.all():
                    logger.debug(
                        f"{d}: 成交量约束剔除 {int((~keep).sum())} 只（缩权后低于 min_weight={min_weight}）"
                    )
                w = w[keep]
                # 不归一化：权重和 < 1 的部分即现金（买不到的仓位留给现金）

            rows.append(pd.DataFrame({
                "trade_date": d,
                "ts_code": top.loc[w.index, "ts_code"].values,
                "weight": w.values,
                "is_signal": True,      # 调仓信号日：收盘生成信号，次日开盘成交（T+1）
            }))
            prev_picks = [str(c) for c in top.loc[w.index, "ts_code"].tolist()]

        if not rows:
            return pd.DataFrame(columns=["trade_date", "ts_code", "weight", "is_signal"])
        return pd.concat(rows, ignore_index=True)

    @staticmethod
    def _cap_weights(w: pd.Series, max_weight: float, rounds: int = 6) -> pd.Series:
        """单票权重上限，超限部分按比例分给未超限的股票。"""
        w = w.astype(float).copy()
        if max_weight is None or max_weight <= 0 or len(w) * max_weight < 1:
            return w / w.sum() if w.sum() > 0 else w
        for _ in range(rounds):
            over = w > max_weight + 1e-12
            if not over.any():
                break
            excess = float((w[over] - max_weight).sum())
            w[over] = max_weight
            under = ~over
            if not under.any() or w[under].sum() <= 0:
                break
            w[under] = w[under] + excess * (w[under] / w[under].sum())
        return w

    # ================================================================ 回测

    def run(
        self,
        panel: pd.DataFrame,
        weights: pd.DataFrame,
        start_date: str | None = None,
        max_positions_note: bool = True,
        benchmark: pd.Series | None = None,
    ) -> dict:
        """向量化组合回测。

        panel  : 价格面板，含 trade_date / ts_code / open / close
                 （可选 is_suspended / is_limit_up / is_limit_down）
        weights: 权重表（trade_date / ts_code / weight / is_signal）
                 - is_signal=True  ：调仓信号日（月末收盘生成），T+1 次日开盘成交
                 - is_signal=False ：风控事件日（止损/熔断触发），当天生效，不再错位
                 缺省（旧表无 is_signal 列）：全部视为信号日，保持 T+1 语义。
        start_date: 回测起点（面板可以带 lookback 段用于因子历史，
                 净值从 start_date 起算并裁剪输出）。
        benchmark: 基准净值序列（如沪深300，归一化到 1 起点）。
                 传入后在 metrics 里补算 alpha / beta / 信息比率 /
                 超额收益 / 跟踪误差 —— 区分「beta 钱」和「alpha 钱」。
        """
        if weights is None or weights.empty:
            raise ValueError("权重为空，无法回测")

        # ---- 未来函数硬拦截：面板严禁携带未来收益列 ----
        fwd_cols = [c for c in panel.columns if str(c).startswith("fwd_ret_")]
        if fwd_cols:
            raise ValueError(
                f"面板包含未来收益列 {fwd_cols} —— 严禁用于信号生成/回测！"
                "add_forward_returns 只允许在因子评估脚本中使用。"
            )

        # 只对进入过持仓池的股票建矩阵（省内存）
        held = set(weights["ts_code"].astype(str).unique())
        px = panel[panel["ts_code"].astype(str).isin(held)].copy()
        px["ts_code"] = px["ts_code"].astype(str)
        logger.info(f"组合回测：持仓池 {len(held)} 只股票，价格行 {len(px):,}")

        dates = sorted(px["trade_date"].unique())
        codes = sorted(held)

        close = px.pivot_table(index="trade_date", columns="ts_code", values="close")
        open_ = px.pivot_table(index="trade_date", columns="ts_code", values="open")
        close = close.reindex(index=dates, columns=codes).astype(float)
        open_ = open_.reindex(index=dates, columns=codes).astype(float)

        # 停牌 / 涨跌停掩码
        def _mask(col: str) -> pd.DataFrame:
            if col not in px.columns:
                return pd.DataFrame(False, index=close.index, columns=close.columns)
            m = px.pivot_table(index="trade_date", columns="ts_code", values=col)
            return m.reindex(index=close.index, columns=close.columns).fillna(0).astype(bool)

        suspended = _mask("is_suspended")

        # ---- 开盘一字板判定（H5 修复：成交在次日开盘，挡单必须用开盘价口径）----
        # 旧实现用「当日收盘封板」挡「当日开盘成交」—— 既用了当日收盘才知道的
        # 信息（前视），又把"盘中打开涨停"的票误挡。正确口径：
        #   T+1 开盘能否买入/卖出，取决于开盘价相对昨收的跳空是否已触及板：
        #     开盘 gap >= 涨停阈值  → 一字涨停，买入单无法成交
        #     开盘 gap <= -跌停阈值 → 一字跌停，卖出单无法成交
        # qfq 序列 pre_close == 昨收（清洗层按自身序列补齐），可直接用。
        # 阈值按板块（主板 10% / 创业科创 20% / 北交所 30%），ST 5% 在清洗层
        # 已体现在 is_limit_*（此处用开盘 gap 兜底一字板场景）。
        def _limit_pct_for(code: str) -> float:
            from src.common.utils import board_of
            c = board_of(str(code))
            return {"创业板": 20.0, "科创板": 20.0, "北交所": 30.0}.get(c, 10.0)

        open_gap = (open_ / close.shift(1) - 1.0) * 100.0
        # 无昨收的日子（上市首日）gap 无意义 -> 置 0（不算一字板）
        open_gap = open_gap.where(close.shift(1).notna(), 0.0)
        limit_tol = 0.6    # 与清洗层容差一致：封板价常差零点几个百分点
        limit_pct_m = pd.DataFrame(
            {c_: _limit_pct_for(c_) for c_ in close.columns},
            index=close.index, columns=close.columns,
        ).astype(float)
        open_limit_up = (open_gap >= limit_pct_m - limit_tol)
        open_limit_down = (open_gap <= -(limit_pct_m - limit_tol))
        # 开盘一字 + 收盘仍封 = 全天买不进/卖不出（最严格场景）；
        # 开盘一字但盘中开板 = 开盘瞬间确实无法成交（市价单成交在开盘集合竞价）
        # 一字板判定用开盘跳空；结合收盘封板信息可进一步收紧，但开盘口径已消除前视

        # ---- 目标权重矩阵 ----
        w_target = (weights.pivot_table(index="trade_date", columns="ts_code",
                                        values="weight", aggfunc="sum")
                    .reindex(index=close.index, columns=close.columns))

        # 关键：调仓日里「没被选中」的股票，权重必须是 0 而不是 NaN。
        # 如果留 NaN，后面的 ffill 会把上一期的权重带下来 —— 持仓只增不减，
        # 十几期之后组合会膨胀到几百只，完全偏离 Top50 的设定。
        def _as_date(x):
            return x.date() if hasattr(x, "date") else x

        rb_set = {_as_date(d) for d in weights["trade_date"].unique()}
        idx_dates = [_as_date(d) for d in w_target.index]
        rb_rows = pd.Series(idx_dates).isin(rb_set).to_numpy()
        w_target = w_target.where(~(rb_rows[:, None] & w_target.isna().to_numpy()), 0.0)

        # ---- T+1 错位（只对「调仓信号日」生效）----
        # 信号在信号日（月末）收盘后生成，次日开盘才按新权重成交 —— 整体后移一天。
        # 但风控事件日（止损/熔断，is_signal=False）是"当日决策当日执行"，
        # 不能再 shift，否则止损被推迟两天、形同虚设。
        if "is_signal" in weights.columns:
            sig_days = {_as_date(d) for d in
                        weights.loc[weights["is_signal"] == True, "trade_date"]}  # noqa: E712
        else:
            sig_days = rb_set      # 旧表无标记：全部视为信号日（保持 T+1 语义）
        sig_mask = pd.Series(idx_dates).isin(sig_days).to_numpy()
        sig_arr = sig_mask[:, None]                 # (n,1) 广播到 (n,cols)
        w_np = w_target.to_numpy(dtype=float)
        w_sig = pd.DataFrame(np.where(sig_arr, w_np, np.nan),
                             index=w_target.index, columns=w_target.columns)
        w_evt = pd.DataFrame(np.where(~sig_arr, w_np, np.nan),
                             index=w_target.index, columns=w_target.columns)
        # 关键语义：
        #   - 信号日权重 shift(1) 后在次日生效，且**覆盖**当日事件权重
        #     （调仓 = 用新持仓替换旧持仓，而不是叠加。若相加，风控每日矩阵
        #      里"调仓日次日的旧持仓 ffill"会与新权重叠加成 2 倍杠杆）
        #   - 非信号日保留 w_evt 的 NaN（事件日权重当天生效；纯信号路径下
        #     无事件 = NaN，交给后面的 ffill 维持持仓）
        w_sig_shifted = w_sig.shift(1)
        w_target = w_sig_shifted.where(w_sig_shifted.notna(), w_evt)

        # 调仓日之外权重维持：前向填充；建仓前为 0
        w_target = w_target.ffill().fillna(0.0)

        # 数据缺失的日子（该股尚未上市 / 停牌无价）不持有
        tradable = close.notna() & open_.notna()
        w_target = w_target.where(tradable, np.nan).ffill().fillna(0.0)

        # ---- 受限处理：停牌 / 开盘一字涨停买不进 / 开盘一字跌停卖不出 ----
        # H5：挡单口径从「收盘封板」改为「开盘一字板」—— 成交发生在 T+1
        # 开盘，只有开盘价能反映当时能否成交。收盘是否封板是当天收盘才知道的
        # 信息，用它挡开盘单既前视又错位。
        w_prev_raw = w_target.shift(1).fillna(0.0)
        diff_raw = w_target - w_prev_raw
        blocked = suspended | (open_limit_up & (diff_raw > 0)) \
            | (open_limit_down & (diff_raw < 0))
        # 价格缺失也算受限（无法成交）
        blocked = blocked | ~tradable

        pos = w_target.copy()
        pos[blocked] = np.nan
        pos = pos.ffill().fillna(0.0)

        # ---- 日收益 ----
        close_prev = close.shift(1)
        pos_prev = pos.shift(1).fillna(0.0)
        diff = pos - pos_prev

        # ---- 毛净值（资金基数）：成本基数必须随净值增长 ----
        # 旧实现用 self.initial_capital 做 notional/participation 基数：组合复利
        # 到 2× 后，真实委托额/参与率翻倍，冲击成本与流动性约束被按 1× 低估，
        # 回测越后期越便宜（系统性虚增收益）。正确基数 = 当期组合净值。
        # 净值依赖收益、收益依赖冲击成本 —— 为避免循环，用「无成本毛净值」
        # 近似当期资金：成本占净值比例小（年化几个点），此近似误差为二阶。
        stock_ret = (close / close_prev - 1.0).fillna(0.0)
        ret_hold_gross = (pos_prev * stock_ret).sum(axis=1)
        exec_gross = open_.fillna(close)     # 无成本成交价（近似：开盘价成交）
        # 注（2026-09-09 第三轮复核）：sum(axis=1) 默认 skipna，缺价持仓的
        # 0×NaN 贡献被自动跳过，与持有路径 fillna(0) 口径天然一致，无需再防御。
        r_old_gross = (pos_prev * (exec_gross / close_prev - 1.0)).sum(axis=1)
        r_new_gross = (pos * (close / exec_gross - 1.0)).sum(axis=1)
        is_rb = diff.abs().sum(axis=1) > 1e-10
        gross_ret = ret_hold_gross.where(~is_rb, r_old_gross + r_new_gross).fillna(0.0)
        gross_ret.iloc[0] = 0.0
        equity_gross = (1.0 + gross_ret).cumprod() * self.initial_capital
        # 当日委托用「前一日收盘净值」（T+1 开盘成交时资金已知），首日按期初
        nav_base = equity_gross.shift(1).fillna(self.initial_capital)

        # ---- 滑点（支持动态流动性模型）----
        # model=liquidity: slip_bps = base + impact_coef*sqrt(participation)*100
        #   participation = 委托额 / (当日该股成交额 × cap)
        # 修复：此前只读 base_bps=2 固定滑点，costs.yaml 的冲击成本模型是死配置。
        # 对小成交额的边缘票系统性低估成本、虚增收益。
        if self.slip_model == "liquidity" and "amount" in px.columns:
            amount_m = px.pivot_table(index="trade_date", columns="ts_code",
                                      values="amount")
            amount_m = amount_m.reindex(index=close.index, columns=close.columns)
            # 委托额 = |Δw| × 当期净值（nav_base 是 Series，广播到各列）
            notional_m = (np.abs(diff).to_numpy()
                          * nav_base.to_numpy()[:, None])
            participation = notional_m / (amount_m.to_numpy() * self.participation_cap)
            slip_bps = (self.slip * 1e4
                        + self.impact_coef * np.sqrt(np.clip(participation, 0, None)) * 100.0)
            # L1 修复（2026-09-07）：成交额缺失或为 0 时 participation 是 NaN，
            # 而 NaN 会穿透 sqrt / clip 一路传到 exec_price，最后被下面的
            # fillna(open_) 按「开盘价原价成交」处理 —— 等于给这些股票发了一张
            # 零滑点通行证，越是数据不全的冷门票成本越低，方向完全反了。
            # 保守原则：流动性未知 = 按滑点上限计价（clip 上限），不是按下限。
            nan_slip = ~np.isfinite(slip_bps)
            if nan_slip.any():
                logger.warning(
                    f"滑点模型：{int(nan_slip.sum())} 个（日期×股票）因成交额缺失/为 0 "
                    f"无法计算参与率，已按保守上限 {self.slip_max_bps:.0f}bps 计价")
                slip_bps = np.where(nan_slip, self.slip_max_bps, slip_bps)
            slip_bps = np.clip(slip_bps, self.slip_min_bps, self.slip_max_bps)
            # P0-3：保守档滑点 × slip_multiplier（默认 1.5），覆盖冲击系数未实证误差
            slip_bps = slip_bps * self.slip_multiplier
            slip_m = pd.DataFrame(slip_bps / 1e4,
                                  index=close.index, columns=close.columns)
        else:
            slip_m = float(self.slip) * self.slip_multiplier * np.ones(close.shape)
            slip_m = pd.DataFrame(slip_m, index=close.index, columns=close.columns)

        # 成交价：买入上滑、卖下滑（逐元素判断方向）
        slip_up = (diff > 0)
        exec_price = open_ * (1 + slip_m)
        exec_price = exec_price.where(slip_up, open_ * (1 - slip_m))
        exec_price = exec_price.fillna(open_)

        # 调仓日的收益（带滑点）
        r_old = (pos_prev * (exec_price / close_prev - 1.0)).sum(axis=1)
        r_new = (pos * (close / exec_price - 1.0)).sum(axis=1)

        # 费率矩阵（日期 × 股票）：买入 = 佣金+过户费，卖出再加印花税。
        # 印花税按日期分段，先按日期取率再广播到所有列。
        if self.use_historical_stamp:
            stamp = pd.Series([self._stamp_for(d) for d in close.index],
                              index=close.index, dtype=float)
        else:
            stamp = pd.Series(float(self.stamp_duty), index=close.index, dtype=float)

        # ---- 真实成本模型 ----
        # 名义成交金额 = |Δw| × 当期净值（权重是分数，乘资金即换手金额）
        notional = (np.abs(diff.to_numpy()) * nav_base.to_numpy()[:, None])
        active = notional > 0
        # 佣金：成交额 × 费率，单笔最低 min_commission（A 股最低 5 元/笔）。
        # 但组合调仓中权重微调（成交额 < min_fee_floor）强收 5 元会让小额成本
        # 占比畸高 —— 低于阈值只收比例佣金。必须用 active 掩码防 0 成交额收 5 元。
        comm = np.maximum(
            notional * self.commission,
            np.where(active & (notional >= self.min_fee_floor),
                     self.min_commission, 0.0),
        )
        # 过户费（双边，按比例）+ 印花税（仅卖出，按日期分段）
        buy_m = np.full(close.shape, self.transfer_fee, dtype=float)
        sell_col = (self.transfer_fee + stamp).to_numpy()[:, None]
        sell_m = np.repeat(sell_col, close.shape[1], axis=1)
        other_m = np.where(diff.to_numpy() > 0, buy_m, sell_m)
        cost = pd.Series(
            np.nansum(comm + notional * other_m, axis=1), index=close.index
        )
        # 量纲：r_old/r_new 是收益率（无单位），cost 是绝对金额，必须转成比例。
        # 基数用当期净值（nav_base）而非固定期初 —— 成本占比 = 金额/当期资金。
        cost_ratio = cost / nav_base

        ret_rb = r_old + r_new - cost_ratio

        ret = ret_hold_gross.where(~is_rb, ret_rb).fillna(0.0)
        ret.iloc[0] = 0.0

        equity = (1.0 + ret).cumprod() * self.initial_capital
        equity.name = "equity"

        # ---- 换手与持仓数 ----
        turnover = diff.abs().sum(axis=1)
        n_holdings = (pos.abs() > 1e-10).sum(axis=1)

        # ---- 调仓次数（统计口径 v5）----
        # 调仓次数 = 落在回测区间内的计划信号日数（1 个信号 = 1 次调仓，
        # 与 T+1 顺延/是否末日无执行无关——计划口径，和月度数直接对得上）。
        # 顺延及风控生效日 = 持仓实际变化、又非信号生效日的日子（顺延/止损/熔断）。
        sig_days = rb_set
        if "is_signal" in weights.columns:
            sig_days = {_as_date(d) for d in
                        weights.loc[weights["is_signal"] == True, "trade_date"]}  # noqa: E712
        sig_t1_days: set = set()
        for d in sig_days:
            if d in dates:
                i = dates.index(d)
                if i + 1 < len(dates):
                    sig_t1_days.add(dates[i + 1])
        ev_days = {d for d, t in zip(idx_dates, turnover)
                   if t > 1e-10 and d not in sig_t1_days}
        n_rebalance = len(sig_days & set(idx_dates))
        n_risk_days = len(ev_days)

        # ---- 按回测起点裁剪（面板可带 lookback 段）----
        # lookback 段（回测起点之前）没有调仓信号，净值恒等于期初资金，
        # 直接裁剪掉，保证指标/年化按实际回测区间计算。
        if start_date:
            sd = pd.Timestamp(start_date)
            keep = pd.to_datetime(equity.index) >= sd
            if keep.any():
                equity = equity[keep]
                ret = ret[keep]
                turnover = turnover[keep]
                n_holdings = n_holdings[keep]
        years = len(equity) / TRADING_DAYS

        metrics = {
            "总收益率": total_return(equity),
            "年化收益率": annual_return(equity),
            "年化波动率": annual_volatility(equity),
            "夏普比率": sharpe_ratio(equity),
            "索提诺比率": sortino_ratio(equity),
            "最大回撤": max_drawdown(equity),
            "年化换手率": float(turnover.sum() / years) if years > 0 else np.nan,
            "平均持仓数": float(n_holdings.mean()),
            "调仓次数": n_rebalance,
            "顺延及风控生效日": n_risk_days,
            # P0-3：每份 metrics 明确标注成本档位（impact_coef 未实证，
            # 保守档滑点×1.5 是正式绩效口径，乐观档仅供敏感性对照）
            "成本档位": self.cost_tier_label,
        }

        # P1-9 基准指标：区分 beta 钱和 alpha 钱。
        # 传入 benchmark（归一化净值，如沪深300）后补算：
        #   alpha/beta（CAPM 回归）、信息比率、超额收益、跟踪误差。
        if benchmark is not None and not benchmark.empty:
            try:
                b = pd.to_numeric(benchmark, errors="coerce").dropna()
                if len(b) >= 20:
                    metrics["基准年化收益率"] = annual_return(b)
                    beta, alpha = beta_alpha(equity, b)
                    metrics["Beta"] = beta if np.isfinite(beta) else np.nan
                    metrics["年化Alpha"] = alpha if np.isfinite(alpha) else np.nan
                    metrics["超额年化收益率"] = (
                        annual_return(equity) - annual_return(b)
                        if np.isfinite(annual_return(b)) else np.nan
                    )
                    metrics["信息比率"] = information_ratio(equity, b)
                    metrics["跟踪误差"] = tracking_error(equity, b)
            except Exception:                              # noqa: BLE001
                logger.warning("基准指标计算失败（不影响主指标）")

        return {
            "equity_curve": equity,
            "daily_returns": ret.rename("daily_return"),
            "position": pos,
            "turnover": turnover.rename("turnover"),
            "n_holdings": n_holdings.rename("n_holdings"),
            "metrics": metrics,
        }

    # ================================================================ 成本

    def _stamp_for(self, d) -> float:
        """按日期取印花税率（2023-08-28 起由千一下调为万五）。"""
        dd = d.date() if hasattr(d, "date") else d
        for s, e, r in STAMP_DUTY_SCHEDULE:
            if s <= dd <= e:
                return r
        return self.stamp_duty


def monthly_rebalance_dates(calendar: List, start: Optional[str] = None,
                            end: Optional[str] = None) -> List:
    """从交易日历取每月最后一个交易日。"""
    s = pd.Series(sorted(calendar))
    s = pd.to_datetime(s)
    df = pd.DataFrame({"d": s})
    df["ym"] = df["d"].dt.strftime("%Y-%m")
    last = df.groupby("ym")["d"].max().tolist()
    out = [d.date() for d in last]
    if start:
        out = [d for d in out if d >= pd.Timestamp(start).date()]
    if end:
        out = [d for d in out if d <= pd.Timestamp(end).date()]
    return out


if __name__ == "__main__":
    print("该模块由 scripts/run_strategy.py 调用")
