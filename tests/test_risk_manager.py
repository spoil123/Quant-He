# -*- coding: utf-8 -*-
"""RiskManager 三条闸门测试：仓位约束 / 个股止损 / 组合熔断。

P1-3：风控是"改订单"的逻辑，最容易出 T+1 语义、黑名单、触发次日生效
这类隐蔽 bug（此前熔断就出过"触发一次永久锁死"和"当天就用未来回撤"）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.layer4_risk.risk_manager import RiskManager


def _cfg(**over) -> dict:
    base = {
        "position": {"max_industry_weight": 0.25, "min_cash_ratio": 0.02,
                     "max_single_weight": 0.05, "max_positions": 60},
        "stop_loss": {"enabled": True, "single_stock_drawdown": -0.15,
                      "trailing_stop": -0.20, "blacklist_days": 5},
        "circuit_breaker": {"enabled": True, "drawdown_trigger": -0.12,
                            "reduce_to": 0.5, "cooldown_days": 3,
                            "recovery_ratio": 0.5},
        "tradability": {"exclude_st": True, "exclude_suspended": True,
                        "exclude_limit_up": True, "min_avg_amount_20d": 10_000_000},
    }
    base.update(over)
    return base


class TestEffectiveControls:
    def test_lists_all_gates(self):
        rm = RiskManager(_cfg())
        items = rm.effective_controls()
        assert len(items) == 4
        joined = "\n".join(items)
        assert "仓位约束" in joined
        assert "个股止损" in joined
        assert "组合熔断" in joined
        assert "交易过滤" in joined

    def test_reflects_enabled_state(self):
        rm_on = RiskManager(_cfg())
        rm_off = RiskManager(_cfg(**{"stop_loss": {"enabled": False},
                                     "circuit_breaker": {"enabled": False}}))
        assert "个股止损: ON" in rm_on.effective_controls()[1]
        assert "个股止损: OFF" in rm_off.effective_controls()[1]
        assert "组合熔断: OFF" in rm_off.effective_controls()[2]


class TestPositionConstraints:
    def _w(self):
        """A 行业 0.6 超上限、B 行业 0.2 未超。"""
        d = pd.Timestamp("2023-06-30").date()
        return pd.DataFrame({
            "trade_date": [d] * 5,
            "ts_code": ["000001", "000002", "000003", "000004", "000005"],
            "weight": [0.30, 0.20, 0.10, 0.15, 0.05],
            "industry": ["A", "A", "A", "B", "B"],
            "is_signal": [True] * 5,
        })

    def test_industry_cap_scales_down_over_limit(self):
        """行业 A 权重 0.6 > 上限 0.25 → 等比缩到 0.25，腾出仓位补给 B。"""
        rm = RiskManager(_cfg(**{"position": {"max_industry_weight": 0.25,
                                              "min_cash_ratio": 0.0,
                                              "max_single_weight": 0.3,
                                              "max_positions": 60}}))
        out = rm.apply_position_constraints(self._w(), pd.DataFrame())
        ind_sum = out.groupby("industry")["weight"].sum()
        # A: 0.60 → 0.25（缩到上限）
        assert ind_sum["A"] == pytest.approx(0.25, abs=1e-9)
        # B: 0.20 + 补给 0.35（B 空间 0.05 全用 + 剩 0.30 留现金）
        assert ind_sum["B"] == pytest.approx(0.25, abs=1e-9)
        # 总权重 = 0.25 + 0.25 = 0.50（余 0.50 现金，不虚增）
        assert out["weight"].sum() == pytest.approx(0.50, abs=1e-9)
        # 单票不超上限
        assert out["weight"].max() <= 0.3 + 1e-9

    def test_single_weight_cap_enforced(self):
        """max_single_weight 真正生效（此前只打印不执行）。"""
        w = self._w()
        # 让 B 的 000004 独占 0.4（超单票上限 0.3 但不超行业）
        w.loc[w["ts_code"] == "000004", "weight"] = 0.35
        w.loc[w["ts_code"] == "000005", "weight"] = 0.05
        w.loc[w["industry"] == "A", "weight"] = 0.30 / 3   # A 0.1×3 不超行业
        w["weight"] = w["weight"] / w["weight"].sum() * 1.0  # 归一
        rm = RiskManager(_cfg(**{"position": {"max_industry_weight": 1.0,
                                              "min_cash_ratio": 0.0,
                                              "max_single_weight": 0.3,
                                              "max_positions": 60}}))
        out = rm.apply_position_constraints(w, pd.DataFrame())
        assert out["weight"].max() <= 0.3 + 1e-9, \
            f"单票上限未生效: max={out['weight'].max():.3f}"

    def test_cash_floor_caps_total(self):
        """权重和 1.0 > 1-cash(0.98) → 整体缩到 0.98。"""
        w = self._w()
        w["weight"] = w["weight"] / 0.80   # _w 总和 0.80，归一回 1.0
        rm = RiskManager(_cfg(**{"position": {"max_industry_weight": 1.0,
                                              "min_cash_ratio": 0.02,
                                              "max_single_weight": 1.0,
                                              "max_positions": 60}}))
        out = rm.apply_position_constraints(w, pd.DataFrame())
        assert out["weight"].sum() == pytest.approx(0.98, abs=1e-9)

    def test_keeps_is_signal(self):
        rm = RiskManager(_cfg(**{"position": {"max_industry_weight": 1.0,
                                              "min_cash_ratio": 0.0,
                                              "max_single_weight": 1.0,
                                              "max_positions": 60}}))
        out = rm.apply_position_constraints(self._w(), pd.DataFrame())
        assert out["is_signal"].all()


class TestStopLoss:
    def _data(self):
        d0 = pd.Timestamp("2023-06-30").date()
        dates = [pd.Timestamp("2023-06-30").date() + pd.Timedelta(days=i) for i in range(7)]
        weights = pd.DataFrame({
            "trade_date": [d0], "ts_code": ["600000"], "weight": [1.0],
            "is_signal": [True],
        })
        panel = pd.DataFrame({
            "trade_date": dates,
            "ts_code": ["600000"] * 7,
            "close": [10.0, 9.5, 9.0, 8.8, 8.5, 8.2, 8.0],   # 第 4 天 -15% 触发
        })
        return weights, panel

    def test_trigger_then_next_day_execute(self):
        """止损触发后【下一交易日】生效 + 黑名单窗口权重置 0。"""
        rm = RiskManager(_cfg())
        out, events = rm.apply_stop_loss(*self._data())
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "stop_loss"
        # 触发日 D4，exec_date = D5
        assert str(ev["exec_date"]) == "2023-07-05"
        # D5 之后（黑名单窗口）该票权重为 0
        after = out[out["trade_date"] >= pd.Timestamp("2023-07-05").date()]
        assert after["weight"].sum() == 0.0
        # 触发日当天（D4）仍满仓 —— 当日决策当日不改，杜绝未来函数
        d4 = out[out["trade_date"] == pd.Timestamp("2023-07-04").date()]
        assert d4["weight"].iloc[0] == pytest.approx(1.0)

    def test_disabled_returns_input(self):
        rm = RiskManager(_cfg(**{"stop_loss": {"enabled": False}}))
        w, p = self._data()
        out, events = rm.apply_stop_loss(w, p)
        assert events == []
        assert len(out) == len(w)


class TestCircuitBreaker:
    def _data(self):
        d0 = pd.Timestamp("2023-01-03").date()
        dates = [pd.Timestamp("2023-01-03").date() + pd.Timedelta(days=i) for i in range(6)]
        weights = pd.DataFrame({
            "trade_date": [d0], "ts_code": ["600000"], "weight": [1.0],
            "is_signal": [True],
        })
        panel = pd.DataFrame({
            "trade_date": dates, "ts_code": ["600000"] * 6, "close": [10.0] * 6,
        })
        # 净值 D2 回撤 -12% 触发
        equity = pd.Series([1.0, 0.95, 0.88, 0.85, 0.90, 0.95],
                           index=pd.to_datetime(dates))
        return weights, panel, equity

    def test_trigger_applies_next_day(self):
        """触发日收盘算回撤 → 次日才降仓（修复前：当天就用未来回撤凭空躲损）。"""
        rm = RiskManager(_cfg())
        out, events = rm.apply_circuit_breaker(*self._data())
        assert any(e["type"] == "circuit_breaker" for e in events)
        d2 = out[out["trade_date"] == pd.Timestamp("2023-01-05").date()]   # 触发日
        d3 = out[out["trade_date"] == pd.Timestamp("2023-01-06").date()]   # 次日
        assert d2["weight"].iloc[0] == pytest.approx(1.0)
        assert d3["weight"].iloc[0] == pytest.approx(0.5)

    def test_disabled_returns_input(self):
        rm = RiskManager(_cfg(**{"circuit_breaker": {"enabled": False}}))
        w, p, eq = self._data()
        out, events = rm.apply_circuit_breaker(w, p, eq)
        assert events == []
        assert len(out) == len(w)
