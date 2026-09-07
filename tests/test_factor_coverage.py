# -*- coding: utf-8 -*-
"""因子有效样本率报告测试（P2-7 核心交付：factors/coverage.py）。

缺失率是 P2-7 的原问题（40~58 万行/年 vs 全量 113 万行/年），报告模块
必须能正确区分「合理缺失」与「数据退化」，否则闸门形同虚设。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.layer3_strategy.factors.coverage import (
    DEFAULT_MIN_RATE,
    factor_coverage_report,
    warn_if_degraded,
)


def _panel(n_years: int = 3, per_year: int = 10, factor: str = "f_reversal_score"):
    """构造 n_years × per_year 行的面板，可指定因子列缺失率。"""
    rows = []
    for y in range(2018, 2018 + n_years):
        for i in range(per_year):
            rows.append({"trade_date": f"{y}-06-{i + 1:02d}", "ts_code": f"{i:06d}"})
    df = pd.DataFrame(rows)
    df[factor] = 1.0
    return df


class TestFactorCoverageReport:
    def test_rate_computed_per_year(self):
        panel = _panel()
        rep = factor_coverage_report(panel, ["f_reversal_score"])
        assert list(rep.index) == [2018, 2019, 2020]
        # 全有效 → 覆盖率 1.0
        assert rep.loc[2018, "reversal"] == pytest.approx(1.0)
        assert rep["total_rows"].tolist() == [10, 10, 10]

    def test_nan_reduces_rate(self):
        panel = _panel()
        # 2019 年因子 60% 缺失 → 覆盖率 0.4
        mask = panel["trade_date"].astype(str).str.startswith("2019")
        panel.loc[mask, "f_reversal_score"] = float("nan")
        panel.loc[mask & (panel.index % 10 < 4), "f_reversal_score"] = 1.0
        rep = factor_coverage_report(panel, ["f_reversal_score"])
        assert rep.loc[2019, "reversal"] == pytest.approx(0.4, abs=1e-4)

    def test_below_threshold_flagged(self):
        panel = _panel()
        mask = panel["trade_date"].astype(str).str.startswith("2020")
        panel.loc[mask, "f_reversal_score"] = float("nan")
        panel.loc[mask & (panel.index % 10 < 2), "f_reversal_score"] = 1.0  # 20%
        rep = factor_coverage_report(panel, ["f_reversal_score"],
                                     min_rate=0.30)
        assert rep.loc[2020, "reversal"] == pytest.approx(0.2, abs=1e-4)

    def test_zero_year_detected(self):
        panel = _panel()
        mask = panel["trade_date"].astype(str).str.startswith("2019")
        panel.loc[mask, "f_reversal_score"] = float("nan")   # 全年无有效样本
        rep = factor_coverage_report(panel, ["f_reversal_score"])
        assert rep.loc[2019, "reversal"] == 0.0

    def test_key_is_bare_name(self):
        panel = _panel()
        panel["f_momentum_score"] = 1.0   # 额外因子列
        rep = factor_coverage_report(panel, ["f_momentum_score"])
        assert "momentum" in rep.columns
        assert "f_momentum_score" not in rep.columns

    def test_empty_panel_returns_empty(self):
        assert factor_coverage_report(pd.DataFrame(), ["f_reversal_score"]).empty
        assert factor_coverage_report(_panel(), []).empty


class TestWarnIfDegraded:
    def test_zero_year_triggers(self):
        panel = _panel()
        mask = panel["trade_date"].astype(str).str.startswith("2019")
        panel.loc[mask, "f_reversal_score"] = float("nan")
        assert warn_if_degraded(panel, ["f_reversal_score"]) is True

    def test_healthy_panel_passes(self):
        assert warn_if_degraded(_panel(), ["f_reversal_score"]) is False

    def test_empty_panel_not_degraded(self):
        assert warn_if_degraded(pd.DataFrame(), ["f_reversal_score"]) is False

    def test_default_threshold_constant(self):
        assert DEFAULT_MIN_RATE == 0.30
