# -*- coding: utf-8 -*-
"""
第 4 层：风控闸门（可插拔，介于策略权重与回测撮合之间）。

三条闸门，按顺序执行：
    1. apply_position_constraints   仓位约束：行业集中度上限、现金下限
    2. apply_stop_loss              个股止损：成本回撤 / 移动止损 + 黑名单
    3. apply_circuit_breaker        组合熔断：净值回撤降仓 + 冷却 + 恢复

关键设计：
    - 产出仍是 weights 表（trade_date / ts_code / weight），与 PortfolioBacktester
      兼容 —— 风控就是"改订单"，被拦截的仓位变成 0 或缩水，然后走同一套撮合。
    - 止损/熔断按日粒度触发，在触发日的下一个交易日生效（杜绝未来函数），
      与原回测的 T+1 规则一致。
    - 熔断需要无风控净值（base_equity）来判断触发点：先裸跑一遍回测拿净值，
      再决定在哪几天降仓。两遍回测是刻意为之 —— 风控规则天然依赖路径。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.common.config import get_config
from src.common.logger import logger


class RiskManager:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or get_config("risk")
        pos = self.cfg.get("position", {}) or {}
        self.max_industry_weight = float(pos.get("max_industry_weight", 0.25))
        self.min_cash_ratio = float(pos.get("min_cash_ratio", 0.02))
        self.max_single_weight = float(pos.get("max_single_weight", 0.05))
        # L3 修复（2026-09-07）：这两个字段此前只在 effective_controls 里被打印，
        # 约束本身从未执行 —— 日志说「最大持仓 60 只」，实际想持多少持多少。
        # 0 或缺失 = 不限制。
        self.max_positions = int(pos.get("max_positions", 0) or 0)
        self.min_positions = int(pos.get("min_positions", 0) or 0)
        sl = self.cfg.get("stop_loss", {}) or {}
        self.stop_threshold = float(sl.get("single_stock_drawdown", -0.15))
        self.trailing_threshold = float(sl.get("trailing_stop", -0.20))
        self.blacklist_days = int(sl.get("blacklist_days", 10))
        self.sl_enabled = bool(sl.get("enabled", True))
        cb = self.cfg.get("circuit_breaker", {}) or {}
        self.cb_enabled = bool(cb.get("enabled", True))
        self.cb_trigger = float(cb.get("drawdown_trigger", -0.12))
        self.cb_reduce_to = float(cb.get("reduce_to", 0.5))
        self.cb_cooldown = int(cb.get("cooldown_days", 20))
        self.cb_recovery = float(cb.get("recovery_ratio", 0.5))

    def effective_controls(self) -> List[str]:
        """返回实际生效的风控项清单（P1-6）。

        run_top50 --risk 接入时打印此清单，让日志明确告诉你
        「风控实际拦了什么」—— 当前只有仓位/行业约束，止损熔断全 off。
        """
        items: List[str] = []
        pos = self.cfg.get("position", {}) or {}
        items.append(
            f"仓位约束: ON（单票≤{self.max_single_weight:.0%}、"
            f"行业≤{self.max_industry_weight:.0%}、现金≥{self.min_cash_ratio:.0%}、"
            f"最大持仓{pos.get('max_positions', 60)}只）"
        )
        items.append(
            f"个股止损: {'ON' if self.sl_enabled else 'OFF'}"
            f"（阈值 {self.stop_threshold:.0%} / 移动 {self.trailing_threshold:.0%}）"
        )
        items.append(
            f"组合熔断: {'ON' if self.cb_enabled else 'OFF'}"
            f"（回撤 {self.cb_trigger:.0%} 降仓至 {self.cb_reduce_to:.0%}）"
        )
        trad = self.cfg.get("tradability", {}) or {}
        items.append(
            f"交易过滤: ST{'×' if trad.get('exclude_st', True) else '✓'}"
            f" 停牌{'×' if trad.get('exclude_suspended', True) else '✓'}"
            f" 涨停{'×' if trad.get('exclude_limit_up', True) else '✓'}"
            f" 日均额≥{trad.get('min_avg_amount_20d', 10_000_000):,}元"
        )
        return items

    # ================================================================ 1. 仓位约束

    def apply_position_constraints(
        self,
        weights: pd.DataFrame,
        panel: pd.DataFrame,
    ) -> pd.DataFrame:
        """行业集中度 + 现金下限 + 单票上限（硬 cap 语义）。

        上限约束 = 硬 cap：超限的权重截断，超出部分留现金（不重新分配）。
        与 P0-4 成交量约束同款哲学 —— 缩权后不归一化，买不了/超限的钱
        就是现金，避免补给再分配把其它标的/行业推超（振荡）。

        修复（2026-09-06）：
          · 旧实现「超限行业缩到上限 + excess 补给未超限行业」在多个行业
            同时超限时补给到空集或来回振荡（A→B→A），总权重不守恒
            （实测双行业都超 25% 时掉到 0.5 后又弹回）。
          · 单票上限 max_single_weight 此前只打印不生效，现真正执行。
        修复（2026-08-31）：
          · excess 原用「缩放后总权重差」算，补给量虚高（总权重可能 >1），
            改「缩放前后差值」；本次重构为纯截断后该问题不再存在。
        """
        if weights.empty:
            return weights

        w = weights.copy()
        if "industry" not in w.columns:
            ind = panel[["trade_date", "ts_code", "industry"]].drop_duplicates(
                ["trade_date", "ts_code"]
            )
            w = w.merge(ind, on=["trade_date", "ts_code"], how="left")
        w["industry"] = w["industry"].fillna("UNKNOWN")

        rows: List[pd.DataFrame] = []
        for d, g in w.groupby("trade_date", sort=True):
            g = g.copy()
            # ---- 行业上限（按剩余空间补给，迭代收敛）----
            # 旧实现把超限行业的 excess 按「未超限行业当前权重比例」补给，
            # 会把临近上限的行业直接推超，多行业超限时补给到空集甚至振荡
            # （实测双行业都超 25% 时总权重掉到 0.5 不守恒）。正确做法：
            # 补给按「未超限行业的剩余空间」分配，最多补到其上限为止。
            for _ in range(10):
                ind_w = g.groupby("industry")["weight"].sum()
                over_ind = ind_w[ind_w > self.max_industry_weight + 1e-9]
                if over_ind.empty:
                    break
                # 1) 超限行业内部等比缩到上限
                for ind_name in over_ind.index:
                    sel = g["industry"] == ind_name
                    before = float(ind_w.loc[ind_name])
                    g.loc[sel, "weight"] = (
                        g.loc[sel, "weight"] * (self.max_industry_weight / before))
                # 2) 腾出的仓位补给「未超限且有空间」的行业
                excess = float((ind_w.loc[over_ind.index]
                                - self.max_industry_weight).sum())
                if excess <= 1e-12:
                    break
                ind_after = g.groupby("industry")["weight"].sum()   # 缩权后
                ok_ind = [i for i in ind_after.index
                          if i not in over_ind.index]
                space = {i: max(0.0, self.max_industry_weight
                                - float(ind_after.loc[i])) for i in ok_ind}
                space = {i: s for i, s in space.items() if s > 1e-12}
                if not space:
                    break          # 全部行业都顶到上限，无空间可补，余量留现金
                tot_space = sum(space.values())
                alloc = min(excess, tot_space)   # 只补到总空间用完为止
                for ind_name, sp in space.items():
                    sel = g["industry"] == ind_name
                    w_share = g.loc[sel, "weight"] / float(ind_after.loc[ind_name])
                    g.loc[sel, "weight"] += alloc * (sp / tot_space) * w_share
                # 补给后可能又有行业超限（原接近上限的），下一轮再缩 —— 收敛
            # ---- 单票硬 cap（行业缩权后仍可能超，如行业内单票集中）----
            if self.max_single_weight and self.max_single_weight < 1.0:
                over_w = g["weight"] > self.max_single_weight + 1e-9
                if over_w.any():
                    # 超限单票缩到上限，不补给（避免把别的票推超）——
                    # 这部分仓位即现金，与"行业 cap 后超限留现金"语义一致
                    g.loc[over_w, "weight"] = self.max_single_weight
            # ---- 持仓只数上限（L3 修复：此前只打印不执行）----
            # 保留权重最大的 max_positions 只，其余清零。与行业/单票 cap 同款
            # 哲学：超限部分直接留现金，不重新分配 —— 重新分配会把保留下来的
            # 标的推超单票上限，与硬 cap 语义自相矛盾。
            if self.max_positions > 0:
                held = g.index[g["weight"] > 1e-12]
                n_hold = len(held)
                if n_hold > self.max_positions:
                    drop = (g.loc[held, "weight"].sort_values(ascending=False)
                            .index[self.max_positions:])
                    g.loc[drop, "weight"] = 0.0
                    logger.info(f"{d}: 持仓 {n_hold} 只 > 上限 "
                                f"{self.max_positions}，剔除权重最小的 {len(drop)} 只")
            # 持仓只数下限：只告警不强行补仓 —— 集中可能是策略本身的选择，
            # 但低于阈值意味着分散化失效，值得在日志里显式留痕
            if self.min_positions > 0:
                n_left = int((g["weight"] > 1e-12).sum())
                if 0 < n_left < self.min_positions:
                    logger.warning(
                        f"{d}: 持仓 {n_left} 只 < 下限 {self.min_positions}，"
                        f"组合过度集中（分散化失效）")
            # 现金下限：权重和 > 1-cash → 整体等比缩（留现金）。最后做：
            # 前面 cap 已让权重和可能 <1，现金下限只在「还想满仓」时兜底。
            cap = 1.0 - self.min_cash_ratio
            total = float(g["weight"].sum())
            if total > cap and total > 1e-10:
                g["weight"] = g["weight"] * (cap / total)
            rows.append(g)

        out = pd.concat(rows, ignore_index=True)
        n_capped = int(out["weight"].eq(0).sum())
        if n_capped:
            logger.info(f"仓位约束: {n_capped} 个(股票,调仓日)权重被压到 0 或缩水")
        # is_signal：继承输入（调仓信号日 True），仓位约束不改变 T+1 语义
        if "is_signal" not in out.columns:
            out["is_signal"] = True
        # industry 若输入带列则保留：行业约束是否生效需要可验证（测试/审计用），
        # 下游回测器只取需要的列，多一列不影响兼容
        cols = ["trade_date", "ts_code", "weight", "is_signal"]
        if "industry" in out.columns:
            cols = ["trade_date", "ts_code", "industry", "weight", "is_signal"]
        return out[cols]

    # ================================================================ 2. 个股止损

    def apply_stop_loss(
        self,
        weights: pd.DataFrame,
        panel: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, List[dict]]:
        """个股止损（日粒度）+ 黑名单。

        对每只持仓股，在两次调仓之间逐日盯盘：
          - 从建仓成本（持仓段首日收盘）回撤 <= single_stock_drawdown → 止损
          - 从持仓期最高价回撤 <= trailing_stop → 移动止损
        触发后在【下一个交易日】把该股权重置 0，并禁买 blacklist_days 天。

        产出：每日权重表（原调仓日 + 止损触发日，run 支持任意日期记录）。
        """
        if not self.sl_enabled:
            return weights, []
        if weights.empty or panel.empty:
            return weights, []

        wm = weights.pivot_table(index="trade_date", columns="ts_code",
                                 values="weight", aggfunc="sum")
        px = panel.pivot_table(index="trade_date", columns="ts_code", values="close")
        dates = sorted(px.index.union(wm.index))
        wm = wm.reindex(index=dates)
        # 调仓日重置（关键）：调仓日当天未选中的股票权重必须为 0（已被替换清仓）。
        # 若直接 ffill，上一期持仓会被带下来，新旧叠加导致权重和爆到 2-3 倍。
        rb_days = set(weights["trade_date"].unique())
        rb_mask = wm.index.isin(rb_days)
        if rb_mask.any():
            wm.loc[rb_mask] = wm.loc[rb_mask].fillna(0.0)
        wm = wm.ffill().fillna(0.0)
        px = px.reindex(index=dates)

        events: List[dict] = []
        for code in wm.columns:
            w = wm[code]
            p = px[code]
            # 找持仓段：权重 > 0 的连续区间
            active = (w > 1e-10).to_numpy()
            i = 0
            n = len(dates)
            while i < n:
                if not active[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < n and active[j + 1]:
                    j += 1
                # 段 [i, j]
                seg_p = p.iloc[i:j + 1]
                cost = seg_p.iloc[0]
                peak = seg_p.cummax()
                dd_cost = seg_p / cost - 1.0
                dd_peak = seg_p / peak - 1.0
                trig = (dd_cost <= self.stop_threshold) | (dd_peak <= self.trailing_threshold)
                if trig.any():
                    t_idx = i + int(np.argmax(trig.to_numpy()))
                    t = dates[t_idx]
                    if t_idx + 1 < n:
                        t_next = dates[t_idx + 1]
                        # 黑名单窗口：t_next 起 blacklist_days 个交易日
                        black_end = min(t_idx + 1 + self.blacklist_days, n)
                        wm.loc[dates[t_idx + 1:black_end], code] = 0.0
                        events.append({
                            "date": str(t), "exec_date": str(t_next), "ts_code": code,
                            "type": "stop_loss",
                            "reason": "cost" if bool(dd_cost.iloc[t_idx - i] <= self.stop_threshold)
                                       else "trailing",
                            "dd": round(float(min(dd_cost.iloc[t_idx - i], dd_peak.iloc[t_idx - i])), 4),
                        })
                i = j + 1

        # 每日矩阵 → long 表
        out = wm.reset_index().melt(id_vars="trade_date", var_name="ts_code", value_name="weight")
        out = out[out["weight"].abs() > 1e-10]
        # is_signal 标记：调仓信号日 True（走 T+1），其余日子（含止损触发日）False
        # —— 止损是"当日决策当日执行"，回测器对 False 不再错位，杜绝双重 T+1
        if "is_signal" in weights.columns:
            sig_days = set(weights.loc[weights["is_signal"] == True, "trade_date"])  # noqa: E712
        else:
            sig_days = set(weights["trade_date"].unique())
        out["is_signal"] = out["trade_date"].isin(sig_days)
        if events:
            logger.info(f"个股止损: 触发 {len(events)} 笔（含移动止损），黑名单 {self.blacklist_days} 天")
        return out[["trade_date", "ts_code", "weight", "is_signal"]], events

    # ================================================================ 3. 组合熔断

    def apply_circuit_breaker(
        self,
        weights: pd.DataFrame,
        panel: pd.DataFrame,
        base_equity: pd.Series,
    ) -> Tuple[pd.DataFrame, List[dict]]:
        """组合级回撤熔断。

        净值从历史最高回撤 <= drawdown_trigger → 次日仓位整体 × reduce_to，
        冷却 cooldown_days 天；期间若回撤收窄到 recovery_ratio×|trigger|
        以内则提前解除（仓位恢复 ×1）。

        实现：算一条「每日仓位缩放因子」曲线，乘到每日权重矩阵上。
        """
        if not self.cb_enabled:
            return weights, []
        if base_equity is None or len(base_equity) < 2:
            return weights, []

        eq = pd.to_numeric(base_equity, errors="coerce").dropna()
        dd = eq / eq.cummax() - 1.0
        dates = sorted(dd.index)

        def _d(x):
            return x.date() if hasattr(x, "date") else x

        # ---- 逐位置状态机（修复：旧实现 scale[i:]=0.5 整段覆盖，触发一次永久锁死）----
        # 每个交易日独立判断：冷却期递减 → 恢复条件 → 触发条件 → 决定当日缩放。
        # 状态只在相邻日之间传递，不再"覆盖后半段"，冷却/恢复才能真正生效。
        scale = np.ones(len(dates))
        frozen = 0                 # 冷却期剩余交易日
        reduced = False            # 当前是否处于降仓状态
        trigger_abs = abs(self.cb_trigger)
        events: List[dict] = []

        # ---- 前视偏差修复（2026-08-31）----
        # 此前 scale[i] 在触发日 i 当天生效：dd.iloc[i] 是第 i 天【收盘后】才算得
        # 出来的回撤（含当天涨跌），当天就按半仓计算当天收益 = 用未来信息定当天
        # 仓位。2015 股灾期间每天"提前"半仓躲损，凭空多出 ~45pp 收益。
        # 正确语义（与实盘一致，也与止损模块一致）：第 i 天收盘触发 → 第 i+1 天
        # 开盘起降仓。恢复同理，次日生效。
        scale_eff = np.ones(len(dates))   # scale_eff[i] = 第 i 天仓位（由 i-1 收盘决定）
        for i in range(1, len(dates)):
            d_now = float(dd.iloc[i])
            if frozen > 0:
                # 冷却期内：保持当前降仓状态，不做任何触发/恢复判断
                frozen -= 1
            elif reduced:
                # 已降仓：只看是否恢复，不重复触发（触发一次、保持到恢复）
                if d_now > -trigger_abs * self.cb_recovery:
                    reduced = False
                    events.append({"date": str(_d(dates[i])), "type": "circuit_breaker_off",
                                   "dd": round(d_now, 4)})
            elif d_now <= self.cb_trigger:
                # 净值回撤超阈值 → 触发降仓 + 冷却（次日生效）
                reduced = True
                frozen = self.cb_cooldown
                events.append({"date": str(_d(dates[i])), "type": "circuit_breaker",
                               "dd": round(d_now, 4), "reduce_to": self.cb_reduce_to})
            # 收盘后更新「明天」的仓位
            scale_eff[min(i + 1, len(dates) - 1)] = self.cb_reduce_to if reduced else 1.0
        scale = scale_eff

        if not events:
            return weights, []

        # 每日权重矩阵 × 缩放因子
        wm = weights.pivot_table(index="trade_date", columns="ts_code",
                                 values="weight", aggfunc="sum")
        # 统一成 date 对象再排序/对齐：base_equity 的 index 可能是 Timestamp，
        # weights/panel 的 trade_date 是 date —— 直接混排会 TypeError
        # （Cannot compare Timestamp with datetime.date）
        all_dates = sorted({_d(x) for x in wm.index} | {_d(x) for x in dates})
        wm = wm.reindex(index=all_dates)
        # 调仓日重置（若输入还是调仓日粒度）：未选中股票当日权重 0，防 ffill 叠加
        rb_days = set(weights["trade_date"].unique())
        rb_mask = wm.index.isin(rb_days)
        if rb_mask.any():
            wm.loc[rb_mask] = wm.loc[rb_mask].fillna(0.0)
        wm = wm.ffill().fillna(0.0)
        scale_s = pd.Series(scale, index=[_d(x) for x in dates])
        sc = scale_s.reindex(all_dates).ffill().fillna(1.0).to_numpy()
        wm = wm.mul(sc, axis=0)

        out = wm.reset_index().melt(id_vars="trade_date", var_name="ts_code", value_name="weight")
        out = out[out["weight"].abs() > 1e-10]
        # is_signal 继承输入：熔断缩放不改变 T+1 语义
        if "is_signal" in weights.columns:
            out = out.merge(
                weights[["trade_date", "ts_code", "is_signal"]].drop_duplicates(),
                on=["trade_date", "ts_code"], how="left",
            )
            out["is_signal"] = out["is_signal"].fillna(False)
        else:
            out["is_signal"] = False
        logger.info(f"组合熔断: 触发 {sum(1 for e in events if e['type']=='circuit_breaker')} 次")
        return out[["trade_date", "ts_code", "weight", "is_signal"]], events

    # ================================================================ 一键

    def run_risk_controls(
        self,
        weights: pd.DataFrame,
        panel: pd.DataFrame,
        base_equity: Optional[pd.Series] = None,
    ) -> Tuple[pd.DataFrame, List[dict]]:
        """顺序执行：仓位约束 → 个股止损 → 组合熔断。

        返回 (风控后权重表, 风控事件列表)。
        """
        events: List[dict] = []
        w = self.apply_position_constraints(weights, panel)
        if self.sl_enabled:
            w, ev = self.apply_stop_loss(w, panel)
            events += ev
        if self.cb_enabled and base_equity is not None:
            w, ev = self.apply_circuit_breaker(w, panel, base_equity)
            events += ev
        logger.info(f"风控闸门完成: 触发事件 {len(events)} 条")
        return w, events
