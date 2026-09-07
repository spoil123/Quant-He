# -*- coding: utf-8 -*-
"""composite.py 单元测试：custom 权重合成。

P1-7 验收：合成方式有回归保护（含权重归一化、0 权重因子、缺失填充）。
全部用内存数据，不碰数据库。
"""
import numpy as np
import pandas as pd
import pytest

from src.layer3_strategy.factors.composite import composite_score, _custom_weighted


def _make_panel(n=100):
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "trade_date": ["2023-01-31"] * n,
        "ts_code": [f"{i:06d}" for i in range(n)],
        "f_a_score": rng.normal(0, 1, n),   # 已预处理因子得分
        "f_b_score": rng.normal(0, 1, n),
        "f_c_score": rng.normal(0, 1, n),
    })


class TestCustomWeighted:
    def test_weights_normalized(self):
        """权重自动归一化：配置 (1,1) 等效 (0.5,0.5)。"""
        panel = _make_panel()
        cols = ["f_a_score", "f_b_score"]
        w1 = _custom_weighted(panel[cols], cols, {"weights": {"a": 1.0, "b": 1.0}})
        w2 = _custom_weighted(panel[cols], cols, {"weights": {"a": 0.5, "b": 0.5}})
        pd.testing.assert_series_equal(w1, w2)

    def test_zero_weight_factor_ignored(self):
        """权重为 0 的因子不参与合成（momentum/profitability 置 0 的场景）。"""
        panel = _make_panel()
        cols = ["f_a_score", "f_b_score", "f_c_score"]
        w_with_c = _custom_weighted(panel[cols], cols, {
            "weights": {"a": 0.5, "b": 0.5, "c": 0.0},
        })
        w_without_c = _custom_weighted(panel[cols], cols, {
            "weights": {"a": 0.5, "b": 0.5},
        })
        # c 权重 0 时，与「根本没配置 c」结果一致
        pd.testing.assert_series_equal(w_with_c, w_without_c)

    def test_all_zero_weights_falls_back_to_equal(self):
        """权重全 0 时退回等权，而不是全 0 或报错。"""
        panel = _make_panel()
        cols = ["f_a_score", "f_b_score"]
        w = _custom_weighted(panel[cols], cols, {"weights": {"a": 0.0, "b": 0.0}})
        equal = panel[cols].mean(axis=1)
        pd.testing.assert_series_equal(w, equal)

    def test_missing_weight_treated_as_zero(self):
        """未配置权重的因子按 0 处理（不是 NaN）。"""
        panel = _make_panel()
        cols = ["f_a_score", "f_b_score"]
        w = _custom_weighted(panel[cols], cols, {"weights": {"a": 1.0}})
        assert w.equals(panel["f_a_score"])


class TestCompositeScore:
    def test_custom_method_uses_weights(self):
        panel = _make_panel()
        cfg = {"composite": {"method": "custom",
                             "weights": {"a": 0.5, "b": 0.5},
                             "fillna": "zero"}}
        score = composite_score(panel, ["f_a_score", "f_b_score"], cfg)
        expected = (panel["f_a_score"] + panel["f_b_score"]) / 2.0
        pd.testing.assert_series_equal(score, expected)

    def test_equal_weight_mean(self):
        panel = _make_panel()
        cfg = {"composite": {"method": "equal_weight", "fillna": "zero"}}
        score = composite_score(panel, ["f_a_score", "f_b_score", "f_c_score"], cfg)
        expected = panel[["f_a_score", "f_b_score", "f_c_score"]].mean(axis=1)
        pd.testing.assert_series_equal(score, expected)

    def test_fillna_zero_keeps_missing_rows(self):
        """缺失因子填 0：股票不因单因子缺失被剔除（股票池一致性）。"""
        panel = _make_panel()
        panel.loc[0, "f_a_score"] = np.nan
        cfg = {"composite": {"method": "equal_weight", "fillna": "zero"}}
        score = composite_score(panel, ["f_a_score", "f_b_score"], cfg)
        # 第 0 行：a=0(NaN填充), b 有值 → 合成 = b/2，不是 NaN
        assert not pd.isna(score.iloc[0])

    def test_all_missing_row_is_nan(self):
        """全缺失的行合成结果必须是 NaN，不参与选股。"""
        panel = _make_panel()
        panel.loc[0, ["f_a_score", "f_b_score"]] = np.nan
        cfg = {"composite": {"method": "equal_weight", "fillna": "zero"}}
        score = composite_score(panel, ["f_a_score", "f_b_score"], cfg)
        assert pd.isna(score.iloc[0])

    def test_unknown_method_falls_back_equal(self):
        """未知合成方式（如已删除的 ic_weight）退回等权并告警，不崩溃。"""
        panel = _make_panel()
        cfg = {"composite": {"method": "ic_weight", "fillna": "zero"}}
        score = composite_score(panel, ["f_a_score", "f_b_score"], cfg)
        expected = panel[["f_a_score", "f_b_score"]].mean(axis=1)
        pd.testing.assert_series_equal(score, expected)
