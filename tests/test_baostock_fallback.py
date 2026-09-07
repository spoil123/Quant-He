# -*- coding: utf-8 -*-
"""
P1-4 问题3 补测：baostock 财务备源（独立于 akshare 生态）。

覆盖点：
  1. _bs_code 前缀规则（sh./sz./bj.）
  2. query_financial 字段映射 + 小数→百分数 + 去重 + debt_ratio 口径修正
  3. _fetch_baostock_financial 集成（fake baostock_client）
  4. fetch_financial 第三层降级：akshare 全挂 → baostock 兜底
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.layer1_data.fetcher import baostock_client as bs_client_mod
from src.layer1_data.fetcher import financial as fin_mod
from src.layer1_data.fetcher.baostock_client import BaostockClient, _bs_code


# ---------------------------------------------------------------- _bs_code

class TestBsCode:
    def test_sh_prefix(self):
        assert _bs_code("600000") == "sh.600000"
        assert _bs_code("601318") == "sh.601318"

    def test_sz_prefix(self):
        assert _bs_code("000001") == "sz.000001"
        assert _bs_code("300750") == "sz.300750"

    def test_bj_prefix(self):
        assert _bs_code("830799") == "bj.830799"
        assert _bs_code("430047") == "bj.430047"


# ---------------------------------------------------------------- query_financial

class FakeRs:
    """模拟 baostock 查询结果集。"""

    def __init__(self, rows, fields=None):
        self._rows = list(rows)
        self.fields = fields or (
            ["code", "pubDate", "statDate", "epsTTM", "roeAvg",
             "npMargin", "gpMargin", "liabilityToAsset"])
        self.error_code = "0"
        self.error_msg = ""

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        r = self._rows.pop(0)
        return [r.get(f, "") for f in self.fields]


class FakeBs:
    """模拟 baostock 模块（login/query_profit_data/query_balance_data）。"""

    def __init__(self, profit_rows, balance_rows=None):
        self._profit = list(profit_rows)
        self._balance = list(balance_rows or [])
        self.login_calls = 0

    def login(self):
        self.login_calls += 1
        return FakeRs([], fields=["error_code", "error_msg"])

    def query_profit_data(self, **kw):
        if not self._profit:
            return FakeRs([])
        return FakeRs([self._profit.pop(0)])

    def query_balance_data(self, **kw):
        if not self._balance:
            return FakeRs([])
        return FakeRs([self._balance.pop(0)])


@pytest.fixture
def fake_bs(monkeypatch):
    """把 baostock 模块换成 FakeBs。"""

    def _install(profit_rows, balance_rows=None):
        fake = FakeBs(profit_rows, balance_rows)
        # baostock 是顶层模块：替换 sys.modules 条目即可让函数内
        # `import baostock as bs` 拿到 fake（monkeypatch.setattr 不支持
        # 无点的模块名）
        monkeypatch.setitem(sys.modules, "baostock", fake)
        return fake

    return _install


def _profit_row(stat, pub, eps_ttm="1.5", roe="0.12", np_margin="0.25",
                gp_margin="0.40"):
    return {"statDate": stat, "pubDate": pub, "epsTTM": eps_ttm,
            "roeAvg": roe, "npMargin": np_margin, "gpMargin": gp_margin}


class TestQueryFinancial:
    def test_field_map_and_percent(self, fake_bs):
        """字段映射 + 小数比率转百分数。"""
        fake_bs([
            _profit_row("2023-12-31", "2024-03-29", eps_ttm="1.50",
                        roe="0.12", np_margin="0.25", gp_margin="0.40"),
        ])
        bs = BaostockClient()
        df = bs.query_financial("600000", start_year=2023)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["report_date"] == pd.Timestamp("2023-12-31").date()
        assert row["ann_date"] == pd.Timestamp("2024-03-29").date()
        assert row["eps_ttm"] == pytest.approx(1.50)
        assert row["roe"] == pytest.approx(12.0)        # 0.12 -> 12%
        assert row["net_margin"] == pytest.approx(25.0)
        assert row["gross_margin"] == pytest.approx(40.0)

    def test_dedupe_by_report_date(self, fake_bs):
        """同一报告期多次命中只保留一条。"""
        fake_bs([
            _profit_row("2023-12-31", "2024-03-29"),
            _profit_row("2023-12-31", "2024-03-30"),     # 重复报告期
            _profit_row("2024-06-30", "2024-08-30"),
        ])
        df = BaostockClient().query_financial("600000", start_year=2023)
        assert len(df) == 2
        assert df["report_date"].tolist() == [
            pd.Timestamp("2023-12-31").date(),
            pd.Timestamp("2024-06-30").date(),
        ]

    def test_debt_ratio_from_balance(self, fake_bs):
        """资产负债率取自 balance 接口，且修正 ×100 口径。"""
        fake_bs(
            [_profit_row("2023-12-31", "2024-03-29")],
            balance_rows=[{"statDate": "2023-12-31", "pubDate": "2024-03-29",
                           "liabilityToAsset": "0.0092"}],   # 0.92% 量级 -> 92%
        )
        df = BaostockClient().query_financial("600000", start_year=2023)
        assert df["debt_ratio"].iloc[0] == pytest.approx(0.92 * 100)   # 92.0

    def test_no_bps_returns_na(self, fake_bs):
        """baostock 不提供每股净资产 -> 显式 <NA>，不是 0。"""
        fake_bs([_profit_row("2023-12-31", "2024-03-29")])
        df = BaostockClient().query_financial("600000", start_year=2023)
        assert pd.isna(df["bps"].iloc[0])

    def test_empty_result(self, fake_bs):
        """无数据返回空表（北交所/未披露）。"""
        fake_bs([])
        df = BaostockClient().query_financial("830799", start_year=2023)
        assert df.empty

    def test_unsorted_input_sorted(self, fake_bs):
        """输出按报告期升序。"""
        fake_bs([
            _profit_row("2024-06-30", "2024-08-30"),
            _profit_row("2023-12-31", "2024-03-29"),
        ])
        df = BaostockClient().query_financial("600000", start_year=2023)
        assert df["report_date"].tolist() == [
            pd.Timestamp("2023-12-31").date(),
            pd.Timestamp("2024-06-30").date(),
        ]


# ---------------------------------------------------------------- _fetch_baostock_financial 集成

class FakeBaostockClient:
    """模拟 baostock_client.get_baostock() 返回的客户端。"""

    def __init__(self, df):
        self._df = df

    def query_financial(self, code, start_year="2014"):
        return self._df.copy()


def _fake_raw():
    return pd.DataFrame({
        "report_date": [pd.Timestamp("2023-12-31").date(),
                        pd.Timestamp("2024-06-30").date()],
        "ann_date": [pd.Timestamp("2024-03-29").date(),
                     pd.Timestamp("2024-08-30").date()],
        "eps_ttm": [1.5, 1.6],
        "roe": [12.0, 13.0],
        "net_margin": [25.0, 26.0],
        "gross_margin": [40.0, 41.0],
        "debt_ratio": [92.0, 91.0],
    })


class TestFetchBaostockFinancial:
    def test_integration(self, monkeypatch):
        """_fetch_baostock_financial 补 ts_code / report_type / 结构对齐。"""
        monkeypatch.setattr(bs_client_mod, "get_baostock",
                            lambda: FakeBaostockClient(_fake_raw()))
        df = fin_mod._fetch_baostock_financial("600000", "2023")
        assert list(df["ts_code"]) == ["600000", "600000"]
        assert "report_type" in df.columns
        assert "ann_date_source" in df.columns
        assert "roe_ttm" in df.columns
        # 缺失列显式留空
        assert df["bps"].isna().all()
        assert df["eps"].isna().all()

    def test_client_failure_returns_empty(self, monkeypatch):
        """baostock 客户端本身抛错 -> 返回空表而非中断。"""

        class Boom:
            def query_financial(self, code, start_year="2014"):
                raise RuntimeError("baostock 服务端不可用")

        monkeypatch.setattr(bs_client_mod, "get_baostock", lambda: Boom())
        assert fin_mod._fetch_baostock_financial("600000", "2023").empty


# ---------------------------------------------------------------- fetch_financial 三级降级

class TestThreeTierFallback:
    def test_akshare_down_baostock_rescues(self, monkeypatch):
        """akshare 主源+新浪备源全挂 -> 自动降级 baostock。"""
        monkeypatch.setattr(bs_client_mod, "get_baostock",
                            lambda: FakeBaostockClient(_fake_raw()))

        class DeadClient:
            def call(self, func_name, symbol=None, start_year="2014",
                     fallbacks=()):
                raise RuntimeError(f"{func_name} 不可用（模拟 akshare 全挂）")

        monkeypatch.setattr(fin_mod, "get_client", lambda: DeadClient())
        df = fin_mod.fetch_financial("600000", "2023")
        assert not df.empty
        assert "eps_ttm" in df.columns
        assert df["ts_code"].iloc[0] == "600000"

    def test_akshare_empty_baostock_rescues(self, monkeypatch):
        """akshare 返回空表 -> 降级 baostock。"""
        monkeypatch.setattr(bs_client_mod, "get_baostock",
                            lambda: FakeBaostockClient(_fake_raw()))

        class EmptyClient:
            def call(self, func_name, symbol=None, start_year="2014",
                     fallbacks=()):
                return pd.DataFrame()

        monkeypatch.setattr(fin_mod, "get_client", lambda: EmptyClient())
        df = fin_mod.fetch_financial("600000", "2023")
        assert not df.empty
