# -*- coding: utf-8 -*-
"""normalize.py 单元测试：去极值 / 标准化 / 中性化。

P1-7 验收：改预处理代码敢跑测试。
全部用内存数据，不碰数据库。
"""
import numpy as np
import pandas as pd
import pytest

from src.layer3_strategy.preprocessing.normalize import (
    winsorize,
    standardize,
    neutralize,
    preprocess_panel,
)


# ---------------------------------------------------------------- 去极值

class TestWinsorize:
    def test_mad_clips_extreme_values(self):
        """MAD 去极值：极端值被截断到界内，非极端值基本不动。"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 100.0, -90.0])
        out = winsorize(s, method="mad", n=5.0)
        # 100 / -90 是明显离群值，应被截断
        assert out.max() < 100.0
        assert out.min() > -90.0
        # 中间值保持不变
        assert out.iloc[2] == 3.0

    def test_quantile_method(self):
        s = pd.Series(np.arange(1.0, 101.0))
        out = winsorize(s, method="quantile", q=(0.05, 0.95))
        # 5% 分位≈6, 95% 分位≈95（pandas 线性插值），端点被截断
        assert out.min() == pytest.approx(5.95, abs=0.2)
        assert out.max() == pytest.approx(95.05, abs=0.2)

    def test_none_returns_unchanged(self):
        s = pd.Series([1.0, 2.0, 999.0])
        out = winsorize(s, method="none")
        pd.testing.assert_series_equal(out, s)


# ---------------------------------------------------------------- 标准化

class TestStandardize:
    def test_zscore_zero_mean_unit_std(self):
        rng = np.random.default_rng(7)
        s = pd.Series(rng.normal(10, 3, 500))
        out = standardize(s, method="zscore", clip=None)
        assert out.mean() == pytest.approx(0.0, abs=1e-9)
        assert out.std() == pytest.approx(1.0, abs=1e-9)

    def test_rank_maps_to_unit_range(self):
        s = pd.Series([5.0, 1.0, 3.0, 2.0, 4.0])
        out = standardize(s, method="rank")
        assert out.min() >= -1.0 and out.max() <= 1.0

    def test_constant_series_zero(self):
        """全同值序列：方差为 0，返回 0 而不是 NaN/炸掉。"""
        s = pd.Series([3.0, 3.0, 3.0, 3.0, 3.0])
        out = standardize(s, method="zscore")
        assert (out == 0.0).all()


# ---------------------------------------------------------------- 中性化

class TestNeutralize:
    def _make_cross_section(self, n=120):
        rng = np.random.default_rng(1)
        # 因子与行业强相关：银行=5, 医药=0, 电子=-5，让行业效应显著
        ind = rng.choice(["银行", "医药", "电子"], n)
        base = np.where(ind == "银行", 5.0, np.where(ind == "医药", 0.0, -5.0))
        return pd.DataFrame({
            "factor": base + rng.normal(0, 0.5, n),
            "industry": ind,
            "float_mv": rng.lognormal(20, 1, n),
        })

    def test_neutralize_removes_industry_effect(self):
        df = self._make_cross_section()
        resid = neutralize(df, "factor", industry_col="industry", mv_col="float_mv")
        # 残差与行业均值不应再显著相关：各行业残差均值应接近 0
        tmp = df.copy()
        tmp["resid"] = resid
        means = tmp.groupby("industry")["resid"].mean()
        assert means.abs().max() < 0.5, f"行业效应未剥离: {means.to_dict()}"

    def test_neutralize_mv_log(self):
        df = self._make_cross_section()
        resid = neutralize(df, "factor", industry_col=None, mv_col="float_mv")
        assert resid.notna().sum() > 0

    def test_demean_simple(self):
        df = self._make_cross_section(60)
        resid = neutralize(df, "factor", industry_col="industry", method="demean")
        tmp = df.copy()
        tmp["resid"] = resid
        means = tmp.groupby("industry")["resid"].mean()
        # 组内去均值后，各组均值应≈0
        assert means.abs().max() < 1e-9


# ---------------------------------------------------------------- 全流程

class TestPreprocessPanel:
    def test_panel_adds_three_columns(self):
        rng = np.random.default_rng(3)
        n = 200
        df = pd.DataFrame({
            "trade_date": ["2023-01-31"] * n,
            "ts_code": [f"{i:06d}" for i in range(n)],
            "raw": np.append(rng.normal(0, 1, n - 2), [50.0, -60.0]),
            "industry": rng.choice(["A", "B", "C", "D"], n),
            "float_mv": rng.lognormal(20, 1, n),
        })
        out = preprocess_panel(df, "raw", industry_col="industry", mv_col="float_mv")
        for col in ("raw_wins", "raw_z", "raw_neut"):
            assert col in out.columns
        # 极端值被压制
        assert out["raw_wins"].max() < 50.0

    def test_too_small_cross_section_skipped(self):
        """样本数低于 min_cross_section 的截面不计算，保持 NaN。"""
        df = pd.DataFrame({
            "trade_date": ["2023-01-31"] * 5,
            "ts_code": [f"{i:06d}" for i in range(5)],
            "raw": [1.0, 2.0, 3.0, 4.0, 5.0],
            "industry": ["A"] * 5,
            "float_mv": [1e9] * 5,
        })
        out = preprocess_panel(df, "raw", industry_col="industry",
                               mv_col="float_mv", min_cross_section=30)
        assert out["raw_neut"].isna().all()

    def test_does_not_cross_dates(self):
        """绝不跨日期计算：两天截面各自独立。"""
        rng = np.random.default_rng(5)
        n = 100
        d1 = pd.DataFrame({
            "trade_date": ["2023-01-31"] * n,
            "ts_code": [f"{i:06d}" for i in range(n)],
            "raw": rng.normal(0, 1, n),
            "industry": ["A"] * n,
            "float_mv": [1e9] * n,
        })
        d2 = d1.copy()
        d2["trade_date"] = "2023-02-28"
        d2["raw"] = d2["raw"] + 100.0  # 第二天整体抬升
        df = pd.concat([d1, d2], ignore_index=True)
        out = preprocess_panel(df, "raw", industry_col="industry",
                               mv_col="float_mv", min_cross_section=30)
        # 两天的中性化均值都应≈0（各自去均值），而不是混合均值
        m1 = out[out["trade_date"] == "2023-01-31"]["raw_neut"].mean()
        m2 = out[out["trade_date"] == "2023-02-28"]["raw_neut"].mean()
        assert abs(m1) < 0.5
        assert abs(m2) < 0.5
