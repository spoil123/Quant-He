# -*- coding: utf-8 -*-
"""walk_forward 关键逻辑测试：ic_weights 定权、缓存键、列名转换。

P1-3：walk_forward 出过 3 处隐蔽 bug（键名带前缀 lookup miss、训练切片
内存爆、测试期只截下界），这些都是"改坏了没保护"的典型 —— 必须有测试。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.walk_forward import _bare, _panel_cache_key, ic_weights


def _make_panel(seed: int = 0) -> pd.DataFrame:
    """构造带未来收益结构的训练面板。

    f_a_score = 未来 20 日收益本身（IC=1，正向因子）
    f_b_score = 未来 20 日收益取负（IC=-1，负向因子）
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=300)
    codes = [f"{i:06d}" for i in range(40)]

    rows = []
    for c in codes:
        px = 10.0 + np.cumsum(rng.normal(0, 0.3, len(dates)))
        close = pd.Series(px)
        fwd = close.shift(-20) / close - 1.0
        for j, d in enumerate(dates):
            rows.append({
                "trade_date": d.date(), "ts_code": c, "close": close.iloc[j],
                "f_a_score": fwd.iloc[j], "f_b_score": -fwd.iloc[j],
            })
    return pd.DataFrame(rows)


class TestBareName:
    def test_strip_prefix_and_suffix(self):
        assert _bare("f_reversal_score") == "reversal"
        assert _bare("f_turnover_score") == "turnover"
        assert _bare("f_a_score") == "a"


class TestPanelCacheKey:
    def test_same_args_same_key(self):
        k1 = _panel_cache_key("2014-04-01", "2023-12-31", "qfq", True, True)
        k2 = _panel_cache_key("2014-04-01", "2023-12-31", "qfq", True, True)
        assert k1 == k2

    def test_diff_range_diff_key(self):
        k1 = _panel_cache_key("2014-04-01", "2023-12-31", "qfq", True, True)
        k2 = _panel_cache_key("2015-01-01", "2023-12-31", "qfq", True, True)
        assert k1 != k2


class TestIcWeights:
    def test_positive_ic_normalized_negative_zeroed(self):
        """正 IC 因子归一化得 1.0，负 IC 因子置 0。"""
        panel = _make_panel()
        w = ic_weights(panel, ["f_a_score", "f_b_score"],
                       "2020-01-01", "2020-12-31")
        assert w["a"] == pytest.approx(1.0, abs=1e-6)
        assert w["b"] == 0.0
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)

    def test_all_negative_ic_falls_back_equal(self):
        """训练期全负 IC → 无信号 → 退化等权，绝不产出全 0 权重。"""
        panel = _make_panel()
        # 两列都设成 -fwd_ret（原始 f_a_score 就是 fwd_ret）→ 训练期 IC 全负。
        # 注意：不能对 f_b 取反（原始 f_b = -fwd_ret，取反反而变正 IC）
        neg = -panel["f_a_score"]
        panel["f_a_score"] = neg
        panel["f_b_score"] = neg
        w = ic_weights(panel, ["f_a_score", "f_b_score"],
                       "2020-01-01", "2020-12-31")
        assert w["a"] == pytest.approx(0.5)
        assert w["b"] == pytest.approx(0.5)

    def test_keys_are_bare_names(self):
        """返回键必须是裸名（合成模块 _custom_weighted 按裸名查权重）。"""
        panel = _make_panel()
        w = ic_weights(panel, ["f_a_score", "f_b_score"],
                       "2020-01-01", "2020-12-31")
        assert set(w.keys()) == {"a", "b"}
