# -*- coding: utf-8 -*-
"""指数日线备源接线测试（P1-4：src/layer1_data/fetcher/daily_price.py）。

client 层的降级机制已由 test_execution.py::TestFallback 覆盖，这里只保护
fetch_index_daily 的接线不退化：fallbacks 参数必须传给 client.call，
返回结构稳定（回测器直接消费该表）。
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.layer1_data.fetcher.baostock_client as bs_mod
import src.layer1_data.fetcher.daily_price as dp_mod
from src.layer1_data.fetcher.daily_price import fetch_index_daily


def _raw_index(n: int = 5) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "date": dates,
        "open": range(100, 100 + n),
        "high": range(101, 101 + n),
        "low": range(99, 99 + n),
        "close": [100 + 2 * i for i in range(n)],
        "volume": [1000] * n,
    })


class FakeClient:
    def __init__(self, raw, raise_err=False):
        self.raw, self.raise_err = raw, raise_err
        self.calls = []

    def call(self, func_name, **kwargs):
        self.calls.append((func_name, kwargs))
        if self.raise_err:
            raise ConnectionError("主源挂了")
        return self.raw


class TestFetchIndexDaily:
    def test_fallback_wired_to_client(self, monkeypatch):
        """fallbacks 参数必须传给 client.call（防接线退化）。"""
        fc = FakeClient(_raw_index())
        monkeypatch.setattr(dp_mod, "get_client", lambda: fc)
        df = fetch_index_daily("000300", "2023-01-01", "2023-12-31")
        assert fc.calls[0][0] == "stock_zh_index_daily"
        assert fc.calls[0][1]["fallbacks"] == ("stock_zh_index_daily_tx",)

    def test_output_structure(self, monkeypatch):
        """返回列稳定：回测器/指数基准直接消费。"""
        monkeypatch.setattr(dp_mod, "get_client", lambda: FakeClient(_raw_index()))
        df = fetch_index_daily("000300", "2023-01-01", "2023-12-31")
        assert list(df.columns) == ["trade_date", "index_code", "open", "high",
                                    "low", "close", "volume", "amount",
                                    "change_pct"]
        assert df["index_code"].tolist() == ["000300"] * len(df)
        assert df["trade_date"].iloc[0] == pd.Timestamp("2023-01-02").date()

    def test_date_range_filter(self, monkeypatch):
        """区间过滤生效：只留窗口内的交易日。"""
        monkeypatch.setattr(dp_mod, "get_client", lambda: FakeClient(_raw_index(10)))
        df = fetch_index_daily("000300", "2023-01-10", "2023-01-20")
        assert len(df) == 4          # 1/2~1/13 共 10 个工作日，1/10 起只有 4 个
        assert df["trade_date"].min() >= pd.Timestamp("2023-01-10").date()

    def test_main_source_error_returns_empty(self, monkeypatch):
        """主源抛异常 → 空表返回，不炸（备源降级由 client 层完成）。"""
        monkeypatch.setattr(dp_mod, "get_client",
                            lambda: FakeClient(None, raise_err=True))
        assert fetch_index_daily("000300").empty

    def test_empty_raw_returns_empty(self, monkeypatch):
        monkeypatch.setattr(dp_mod, "get_client", lambda: FakeClient(pd.DataFrame()))
        assert fetch_index_daily("000300").empty


# ---------------------------------------------------------------- 个股日线三级降级


def _sina_raw(n: int = 3) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "date": dates, "open": range(10, 10 + n), "high": range(11, 11 + n),
        "low": range(9, 9 + n), "close": [10 + i for i in range(n)],
        "volume": [1000] * n, "amount": [10000] * n,
        "turnover": [0.01] * n, "outstanding_share": [1000000] * n,
    })


def _bs_raw(n: int = 3) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "date": dates, "open": ["10"] * n, "high": ["11"] * n, "low": ["9"] * n,
        "close": [str(10 + i) for i in range(n)], "preclose": ["9"] * n,
        "volume": ["1000"] * n, "amount": ["10000"] * n,
        "tradestatus": ["1"] * n,
    })


class FakeBsClient:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def query_daily(self, ts_code, start_date="2015-01-01",
                    end_date="2050-01-01", adjust="qfq"):
        self.calls.append((ts_code, start_date, end_date, adjust))
        return self.raw.copy() if self.raw is not None else pd.DataFrame()


class TestFetchDailyBaostockFallback:
    def test_akshare_all_down_falls_to_baostock(self, monkeypatch):
        """sina/tx 都失败 → 第三层 baostock（独立生态）兜底。"""
        fc = FakeClient(pd.DataFrame(), raise_err=True)   # akshare 整体不可用
        monkeypatch.setattr(dp_mod, "get_client", lambda: fc)
        bs = FakeBsClient(_bs_raw())
        monkeypatch.setattr(bs_mod, "get_baostock", lambda: bs)

        df = dp_mod.fetch_daily("600000", "2023-01-01", "2023-01-10")
        assert len(df) == 3
        assert df["ts_code"].tolist() == ["600000"] * 3
        assert df["close"].tolist() == [10.0, 11.0, 12.0]
        # 无流通股本：float_share 为空（下游质量闸门会发现，不静默出错）
        assert df["float_share"].isna().all()
        # baostock 确实被调用（akshare 全挂时的第三层兜底）
        assert len(bs.calls) == 1 and bs.calls[0][0] == "600000"

    def test_baostock_tradestatus_maps_to_suspended(self, monkeypatch):
        """baostock tradestatus=0（停牌）→ is_suspended=1。"""
        monkeypatch.setattr(dp_mod, "get_client",
                            lambda: FakeClient(pd.DataFrame(), raise_err=True))
        raw = _bs_raw(3)
        raw["tradestatus"] = ["1", "0", "1"]
        monkeypatch.setattr(bs_mod, "get_baostock", lambda: FakeBsClient(raw))
        df = dp_mod.fetch_daily("600000", "2023-01-01", "2023-01-10")
        assert df["is_suspended"].tolist() == [0, 1, 0]

    def test_sina_ok_baostock_not_called(self, monkeypatch):
        """主源正常时 baostock 不应被调用（不浪费跨生态请求）。"""
        monkeypatch.setattr(dp_mod, "get_client",
                            lambda: FakeClient(_sina_raw(3)))
        bs = FakeBsClient(_bs_raw())
        monkeypatch.setattr(bs_mod, "get_baostock", lambda: bs)
        df = dp_mod.fetch_daily("600000", "2023-01-01", "2023-01-10")
        assert len(df) == 3 and bs.calls == []

    def test_baostock_empty_returns_empty(self, monkeypatch):
        monkeypatch.setattr(dp_mod, "get_client",
                            lambda: FakeClient(pd.DataFrame(), raise_err=True))
        monkeypatch.setattr(bs_mod, "get_baostock", lambda: FakeBsClient(pd.DataFrame()))
        assert dp_mod.fetch_daily("600000").empty
