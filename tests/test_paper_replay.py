# -*- coding: utf-8 -*-
"""真实行情回放撮合核心逻辑测试（scripts/paper_trade_sim.py v2）。

只测纯算法 _replay_one_day 的正确性（不依赖网络/新浪接口）：
  1. 按金额比例沿 bar 吸收，成交额守恒、VWAP 计算正确
  2. 小单只落在首根 bar（5 分钟粒度下 <10% 参与率跨不过单根 bar ——
     这是数据粒度边界，不是 bug）
  3. 价格路径决定滑点符号（上升路径买单吃正滑点）
  4. 异常输入返回 None

注意：不测「参与率-滑点单调」—— 5 分钟 bar 聚合数据不含订单簿深度，
参与率-冲击的实证斜率必须等 QMT 真实成交回报（见 paper_trade_sim 文档）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.paper_trade_sim import _replay_one_day


def _bars(opens: list, closes: list, vol_per_bar: float = 2_000_000):
    """构造单日 bar：给定每根 bar 的开/收价，模拟一段价格路径。

    单根 bar 成交额 ≈ 均价 × vol_per_bar。全天 n 根 bar。
    """
    n = len(opens)
    day = pd.Timestamp("2026-09-04")
    times = [day + pd.Timedelta(minutes=5 * i) for i in range(n)]
    rows = []
    for i, (o, c) in enumerate(zip(opens, closes)):
        rows.append({
            "day": times[i],
            "open": o, "high": max(o, c), "low": min(o, c), "close": c,
            "volume": vol_per_bar,
            "amount": (o + c) / 2 * vol_per_bar,    # VWAP ≈ 均价 × 量
        })
    return pd.DataFrame(rows)


class TestReplayOneDay:
    def test_absorb_full_day_keeps_amount_conservation(self):
        """目标 > 全天成交额时吃完全部 bar，成交额守恒、均价=全天 VWAP。"""
        bars = _bars([10.0, 10.0, 10.0, 10.0], [10.0, 10.0, 10.0, 10.0])
        daily = float(bars["amount"].sum())
        # 目标 = p × daily × cap，取 p=1.0 → target = daily × 0.1 < daily
        # 想要吃满需 target ≈ daily：直接用 cap=1.0 放大
        rec = _replay_one_day(bars, target_participation=1.0, cap=1.0)
        assert rec is not None
        # 全部 4 根 bar 均价 = 10.0（价格恒定）
        assert rec["deal_price"] == pytest.approx(10.0, abs=1e-6)
        # 金额守恒：吸收金额 ≤ 全天成交额，且 volume = 吸收金额/均价
        assert rec["deal_amount"] <= daily * (1 + 1e-6)
        assert rec["deal_amount"] == pytest.approx(rec["volume"] * rec["deal_price"],
                                                   rel=1e-3)

    def test_small_order_sits_in_first_bar(self):
        """小单（参与率 5%，cap=0.1 → 订单额=0.5% 日额）只落在首根 bar。

        5 分钟 bar 单根占全天 ~25%（4 根 bar 构造），0.5% 日额远小于单根
        bar —— 这是 5min 粒度的真实边界：小单吃不到跨 bar 路径。
        """
        # 首根 bar 价格 11，之后跌回 10 —— 若只吃首根，滑点取决于首根均价
        bars = _bars([11.0, 10.0, 10.0, 10.0], [11.0, 10.0, 10.0, 10.0])
        rec = _replay_one_day(bars, target_participation=0.05, cap=0.10)
        assert rec is not None
        # 订单额 = 0.05 × daily × 0.1 → 只吃首根 bar 的一小部分
        # 首根 VWAP = 11.0 → 滑点 ≈ (11-11)/11 ≈ 0（相对首根开盘）
        # 这里开盘价取首根 bar 的 open=11
        assert rec["slip_bps"] == pytest.approx(0.0, abs=1e-6)
        # participation 回到与目标一致（吸收没跨越 bar 界限）
        assert rec["participation"] == pytest.approx(0.05, rel=0.02)

    def test_rising_path_buy_slips_positive(self):
        """价格单边上升时，吃多根 bar 的订单成交均价 > 开盘 → 正滑点。"""
        # 24 根 bar 从 10 涨到 12，cap=0.1，参与率 0.95 → 订单额=9.5% 日额，
        # 跨越多根 bar，均价显著高于开盘 10
        opens = [10.0 + i * (2.0 / 24) for i in range(24)]
        closes = [10.0 + (i + 0.5) * (2.0 / 24) for i in range(24)]
        bars = _bars(opens, closes, vol_per_bar=2_000_000)
        rec = _replay_one_day(bars, target_participation=0.95, cap=0.10)
        assert rec is not None
        assert rec["deal_price"] > 10.0
        assert rec["slip_bps"] > 0

    def test_invalid_input_returns_none(self):
        assert _replay_one_day(pd.DataFrame(), 0.1) is None
        # 无 amount/volume 列
        bad = pd.DataFrame({"day": ["2026-09-04"], "open": [10.0]})
        assert _replay_one_day(bad, 0.1) is None
        # 零成交额
        empty_amt = _bars([10.0, 10.0], [10.0, 10.0])
        empty_amt["amount"] = 0.0
        assert _replay_one_day(empty_amt, 0.1) is None
        # 零成交量
        zero_vol = _bars([10.0, 10.0], [10.0, 10.0])
        zero_vol["volume"] = 0.0
        assert _replay_one_day(zero_vol, 0.1) is None

    def test_open_price_uses_first_bar(self):
        """委托价锚定当日首根 bar 的开盘价。"""
        bars = _bars([9.0, 10.0, 10.0, 10.0], [9.0, 10.0, 10.0, 10.0])
        rec = _replay_one_day(bars, target_participation=0.05, cap=0.10)
        assert rec is not None
        assert rec["order_price"] == pytest.approx(9.0)
