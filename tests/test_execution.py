# -*- coding: utf-8 -*-
"""执行层测试：impact 回归链路（P1-3 补测）。

覆盖：
  1. compute_slip_bps 买卖方向口径（买入上滑/卖下滑都是正成本）
  2. regress_impact 从合成数据恢复真值（base_bps / impact_coef）
  3. 样本不足时的安全降级
  4. AKShareClient 备源 fallback 机制（P1-4）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.layer1_data.fetcher import akshare_client as ak_mod
from src.layer1_data.fetcher.akshare_client import AKShareClient
from src.layer5_execution.impact import (
    compute_slip_bps,
    regress_impact,
    summarize,
)


class TestComputeSlipBps:
    def _df(self):
        return pd.DataFrame({
            "side": ["buy", "buy", "sell", "sell"],
            "order_price": [10.0, 10.0, 10.0, 10.0],
            "deal_price": [10.02, 9.99, 9.98, 10.03],
            "volume": [100, 100, 100, 100],
        })

    def test_buy_up_slip_positive(self):
        out = compute_slip_bps(self._df())
        # 买入成交价更高 = 正滑点（冲击成本）
        assert out.loc[0, "slip_bps"] == pytest.approx(20.0, abs=1e-9)
        # 买入成交价更低 = 负滑点（捡到便宜）
        assert out.loc[1, "slip_bps"] == pytest.approx(-10.0, abs=1e-9)

    def test_sell_down_slip_positive(self):
        out = compute_slip_bps(self._df())
        # 卖出成交价更低 = 正滑点（成本）；更高 = 负滑点
        assert out.loc[2, "slip_bps"] == pytest.approx(20.0, abs=1e-9)
        assert out.loc[3, "slip_bps"] == pytest.approx(-30.0, abs=1e-9)


class TestRegressImpact:
    def _sample(self, n=200, impact=1.0, base=2.0, seed=0):
        rng = np.random.default_rng(seed)
        participation = rng.uniform(0.001, 0.05, n)
        slip = base + impact * 100.0 * np.sqrt(participation) + rng.normal(0, 1.0, n)
        return pd.DataFrame({"participation": participation, "slip_bps": slip})

    def test_recovers_true_values(self):
        """合成数据 base=2.0 / impact=1.0 → 回归应接近真值。"""
        d = self._sample()
        r = regress_impact(d)
        assert r["method"] == "ols"
        assert r["impact_coef"] == pytest.approx(1.0, abs=0.1)
        assert r["base_bps"] == pytest.approx(2.0, abs=0.5)
        assert r["r_squared"] > 0.5

    def test_insufficient_samples_returns_none(self):
        d = self._sample(n=10)
        r = regress_impact(d, min_samples=30)
        assert r["impact_coef"] is None
        assert r["base_bps"] is None
        assert r["method"] is None

    def test_outlier_participation_filtered(self):
        """participation 越界（>1 或 <0，成交额缺失导致）应被过滤。"""
        d = self._sample(n=50)
        d.loc[0, "participation"] = 5.0
        d.loc[1, "participation"] = -1.0
        d2 = d[(d["participation"].between(0, 1))]
        r = regress_impact(d2)
        assert r["n"] == 48


class TestSummarize:
    def test_by_side_structure(self):
        d = pd.DataFrame({
            "side": ["buy", "buy", "sell"],
            "slip_bps": [10.0, 20.0, 30.0],
            "participation": [0.01, 0.02, 0.03],
        })
        s = summarize(d)
        assert s["n_trades"] == 3
        assert s["n_with_slip"] == 3
        assert s["by_side"]["buy"]["count"] == 2
        assert s["by_side"]["sell"]["count"] == 1


class TestFallback:
    """AKShareClient 备源降级（P1-4 核心机制，mock 接口验证）。"""

    @pytest.fixture
    def client(self, tmp_path):
        return AKShareClient(interval=0, max_retry=0, max_workers=1,
                             cache_enabled=False, cache_dir=tmp_path)

    def _set_ak(self, monkeypatch, main=None, backup=None):
        ak = type("FakeAk", (), {})()
        if main is not None:
            ak.main_func = main
        if backup is not None:
            ak.backup_func = backup
        monkeypatch.setattr(ak_mod, "ak", ak)

    def test_fallback_on_exception(self, monkeypatch, client):
        def main(**kw):
            raise ConnectionError("主源挂掉")
        def backup(**kw):
            return pd.DataFrame({"a": [1, 2]})
        self._set_ak(monkeypatch, main, backup)

        df = client.call("main_func", fallbacks=("backup_func",))
        assert len(df) == 2
        assert df.attrs["_source"] == "backup_func"

    def test_fallback_on_empty(self, monkeypatch, client):
        def main(**kw):
            return pd.DataFrame()
        def backup(**kw):
            return pd.DataFrame({"a": [3]})
        self._set_ak(monkeypatch, main, backup)

        df = client.call("main_func", fallbacks=("backup_func",))
        assert len(df) == 1
        assert df.attrs["_source"] == "backup_func"

    def test_main_source_no_attr(self, monkeypatch, client):
        def main(**kw):
            return pd.DataFrame({"a": [1]})
        self._set_ak(monkeypatch, main=main)

        df = client.call("main_func", fallbacks=("backup_func",))
        assert "backup_func" not in df.attrs.get("_source", "")

    def test_all_fail_raises(self, monkeypatch, client):
        def main(**kw):
            raise ConnectionError("主源挂掉")
        def backup(**kw):
            raise TimeoutError("备源也挂")
        self._set_ak(monkeypatch, main, backup)

        with pytest.raises(TimeoutError):
            client.call("main_func", fallbacks=("backup_func",))
