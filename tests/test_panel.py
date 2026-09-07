# -*- coding: utf-8 -*-
"""panel 幸存者偏差对账逻辑测试（P0-1 产物，P1-3 补测）。

market_listed_count_by_year / coverage_report 是"面板覆盖率"口径的裁判，
逻辑错误会直接导致幸存者偏差漏检 —— 必须可测。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.layer3_strategy.panel import coverage_report, market_listed_count_by_year


def _listing() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001", "000002", "000003", "000004"],
        "list_date": pd.to_datetime(
            ["2000-01-01", "2010-01-01", "2015-06-01", "2018-12-31"]).date,
        "delist_date": pd.to_datetime(
            [None, "2017-06-01", None, None]).date,
        "is_delisted": [0, 1, 0, 0],
    })


class TestMarketListedCount:
    def test_counts_alive_at_year_end(self):
        s = market_listed_count_by_year(_listing(), [2016, 2017, 2018])
        # 2016: 000001 + 000002(2017-06 才退) + 000003(2015-06 上市) = 3
        assert s.loc[2016] == 3
        # 2017: 000001 + 000003（000002 已退市，000004 未上市）= 2
        assert s.loc[2017] == 2
        # 2018: 000001 + 000003 + 000004（年末当天上市算在市）= 3
        assert s.loc[2018] == 3

    def test_empty_listing(self):
        s = market_listed_count_by_year(pd.DataFrame(), [2020])
        assert s.loc[2020] == 0


class TestCoverageReport:
    def _panel(self):
        rows = []
        for y in (2017, 2018):
            for c in ("000001", "000002", "000003"):
                rows.append({"trade_date": f"{y}-06-30", "ts_code": c})
        return pd.DataFrame(rows)

    def test_coverage_rate_computed(self):
        rep = coverage_report(self._panel(), listing=_listing())
        assert "year" in rep.index.names or rep.index.name == "year"
        # 2018: 面板 3 只 / 当年上市 3 只 = 1.0
        assert rep.loc[2018, "coverage"] == pytest.approx(1.0, abs=1e-4)
        # 2017: 面板 3 只 / 当年上市 2 只 = 1.5（面板含次新股，可 >1，不告警）
        assert rep.loc[2017, "flag"] == "OK"

    def test_low_coverage_flags(self):
        # 面板只有 1 只票 → 2018 覆盖率 1/3 = 0.33 < 0.9 → LOW
        panel = pd.DataFrame({
            "trade_date": ["2018-06-30"], "ts_code": ["000001"],
        })
        rep = coverage_report(panel, listing=_listing())
        assert rep.loc[2018, "coverage"] == pytest.approx(0.3333, abs=1e-3)
        assert rep.loc[2018, "flag"] == "LOW"

    def test_empty_panel_returns_empty(self):
        rep = coverage_report(pd.DataFrame(), listing=_listing())
        assert rep.empty
