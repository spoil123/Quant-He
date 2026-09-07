# -*- coding: utf-8 -*-
"""财务备源与 PIT 对齐测试（P1-4 核心交付：src/layer1_data/fetcher/financial.py）。

主源（同花顺）无公告日 → 保守对齐（法定最迟披露日）；
备源（新浪）带真实公告日 → precise 对齐；备源2（baostock）跨生态兜底。
三个策略算错一个，财务因子就会提前/延后可用，直接污染回测 —— 必须可测。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.layer1_data.fetcher.financial import (
    _annualize_factor,
    _fetch_baostock_financial,
    _normalize_financial,
    _parse_cn_date,
    _source_of,
)


class TestParseCnDate:
    def test_dash_format(self):
        assert _parse_cn_date("2015-03-31") == date(2015, 3, 31)

    def test_cn_format(self):
        assert _parse_cn_date("2015年03月31日") == date(2015, 3, 31)

    def test_slash_format(self):
        assert _parse_cn_date("2015/3/31") == date(2015, 3, 31)

    def test_empty_and_null(self):
        assert _parse_cn_date(None) is None
        assert _parse_cn_date(float("nan")) is None
        assert _parse_cn_date("") is None

    def test_garbage_and_invalid(self):
        assert _parse_cn_date("abc") is None
        assert _parse_cn_date("2015-13-99") is None   # 非法日期 → ValueError 分支


class TestAnnualizeFactor:
    def test_quarterly_annualization(self):
        assert _annualize_factor("2015-03-31") == pytest.approx(4.0)   # Q1 ×4
        assert _annualize_factor("2015-06-30") == pytest.approx(2.0)   # 半年 ×2
        assert _annualize_factor("2015-09-30") == pytest.approx(4 / 3)
        assert _annualize_factor("2015-12-31") == pytest.approx(1.0)   # 年报 ×1

    def test_unknown_date_returns_one(self):
        assert _annualize_factor("2015-01-15") == 1.0
        assert _annualize_factor(None) == 1.0


class TestSourceOf:
    def test_sina_tagged(self):
        raw = pd.DataFrame()
        raw.attrs["_source"] = "stock_financial_abstract"
        assert _source_of(raw) == "sina"

    def test_default_ths(self):
        assert _source_of(pd.DataFrame()) == "ths"
        raw = pd.DataFrame()
        raw.attrs["_source"] = "stock_financial_analysis_indicator"
        assert _source_of(raw) == "ths"


def _raw_ths() -> pd.DataFrame:
    """主源：同花顺中文列（无公告日）。"""
    return pd.DataFrame({
        "日期": ["2015-03-31", "2015-06-30", "2015-12-31"],
        "摊薄每股收益(元)": [0.1, 0.3, 0.6],
        "每股净资产_调整后(元)": [3.0, 3.2, 3.5],
        "净资产收益率(%)": [4.0, 8.0, 15.0],
    })


def _raw_sina() -> pd.DataFrame:
    """备源：新浪财务摘要（含真实公告日期，中文日期格式）。"""
    return pd.DataFrame({
        "报告期": ["2015年03月31日", "2015年12月31日"],
        "公告日期": ["2015-04-28", "2016-04-29"],
        "基本每股收益": [0.1, 0.6],
        "每股净资产": [3.0, 3.5],
        "净资产收益率": [4.0, 15.0],
        "销售毛利率": [30.0, 35.0],
        "资产负债率": [50.0, 45.0],
    })


class TestNormalizeFinancial:
    def test_ths_conservative_alignment(self):
        """主源：无公告日 → ann_date=法定最迟披露日（保守），source=0。"""
        df = _normalize_financial(_raw_ths(), "600000", source="ths")
        assert len(df) == 3
        assert df["ts_code"].tolist() == ["600000"] * 3
        # 保守对齐：Q1→4/30，半年→8/31，年报→次年 4/30
        ann = df.set_index("report_date")["ann_date"]
        assert ann.loc[date(2015, 3, 31)] == date(2015, 4, 30)
        assert ann.loc[date(2015, 6, 30)] == date(2015, 8, 31)
        assert ann.loc[date(2015, 12, 31)] == date(2016, 4, 30)
        assert df["ann_date_source"].tolist() == [0, 0, 0]
        # 字段映射
        assert "eps" in df.columns and "bps" in df.columns
        assert df.loc[0, "eps"] == pytest.approx(0.1)
        # ROE TTM 年化：Q1 4.0×4=16，半年 8×2=16，年报 15×1=15
        assert df.loc[0, "roe_ttm"] == pytest.approx(16.0)
        assert df.loc[1, "roe_ttm"] == pytest.approx(16.0)
        assert df.loc[2, "roe_ttm"] == pytest.approx(15.0)
        # 主源没有的列补 None（gross_margin/debt_ratio）
        assert df["gross_margin"].isna().all()
        assert df["debt_ratio"].isna().all()

    def test_sina_precise_alignment(self):
        """备源：真实公告日沿用（precise），缺失才用法定日兜底。"""
        df = _normalize_financial(_raw_sina(), "600000", source="sina")
        assert len(df) == 2
        ann = df.set_index("report_date")["ann_date"]
        # 真实公告日优先
        assert ann.loc[date(2015, 3, 31)] == date(2015, 4, 28)
        assert ann.loc[date(2015, 12, 31)] == date(2016, 4, 29)
        assert df["ann_date_source"].tolist() == [1, 1]
        # 备源值全部解析
        assert df.loc[0, "eps"] == pytest.approx(0.1)
        assert df.loc[0, "gross_margin"] == pytest.approx(30.0)
        assert df.loc[0, "debt_ratio"] == pytest.approx(50.0)

    def test_sina_missing_ann_date_falls_back_statutory(self):
        """备源某期缺公告日 → 该期用法定最迟披露日兜底，source=0。"""
        raw = _raw_sina().copy()
        raw.loc[1, "公告日期"] = None
        df = _normalize_financial(raw, "600000", source="sina")
        ann = df.set_index("report_date")["ann_date"]
        # 有公告日的沿用
        assert ann.loc[date(2015, 3, 31)] == date(2015, 4, 28)
        # 缺失的兜底为法定日（次年 4/30）
        assert ann.loc[date(2015, 12, 31)] == date(2016, 4, 30)

    def test_missing_report_date_returns_empty(self):
        raw = pd.DataFrame({"基本每股收益": [0.1]})   # 无报告期列
        assert _normalize_financial(raw, "600000", source="sina").empty

    def test_duplicate_report_dates_deduplicated(self):
        """同一报告期多条记录（如修正值）→ 保留首条。"""
        raw = pd.DataFrame({
            "日期": ["2015-03-31", "2015-03-31"],
            "摊薄每股收益(元)": [0.1, 0.12],
        })
        df = _normalize_financial(raw, "600000", source="ths")
        assert len(df) == 1
        assert df.loc[0, "eps"] == pytest.approx(0.1)

    def test_eps_ttm_computed(self):
        """TTM 需要去年年报/同报告期数据：年报行可算，缺去年数据的前两行 NaN 正常。"""
        df = _normalize_financial(_raw_ths(), "600000", source="ths")
        assert "eps_ttm" in df.columns
        # 年报：TTM = 当年累计 = 0.6
        annual = df[df["report_date"] == date(2015, 12, 31)]
        assert annual.iloc[0]["eps_ttm"] == pytest.approx(0.6)
        # Q1/半年：缺 2014 年数据 → NaN（不是 0，是数据不足的显式缺失）
        assert pd.isna(df["eps_ttm"].iloc[0])


# ---------------------------------------------------------------- baostock 备源2


class _FakeBs:
    """伪造 baostock 客户端：query_financial 返回 baostock 口径的 DataFrame。

    baostock_client.query_financial 返回列：
    report_date / ann_date / eps_ttm / roe / net_margin / gross_margin /
    debt_ratio（比率已转百分数，与主源同构），无 bps。
    """

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def query_financial(self, code, start_year=2014):
        return self._df.copy()


def _raw_baostock() -> pd.DataFrame:
    return pd.DataFrame({
        "report_date": [date(2023, 3, 31), date(2023, 6, 30), date(2023, 12, 31)],
        "ann_date": [date(2023, 4, 29), date(2023, 8, 31), date(2024, 4, 27)],
        "eps_ttm": [1.62, 1.50, 1.60],
        "roe": [2.24, 3.28, 12.5],            # 已是百分数口径
        "net_margin": [33.6, 25.9, 30.0],
        "gross_margin": [None, None, None],
        "debt_ratio": [91.86, 91.93, 90.5],
    })


class TestBaostockFallback:
    def test_output_structure_and_precise_alignment(self, monkeypatch):
        """baostock 自带真实公告日 → precise 对齐沿用，ann_date_source=1。"""
        import src.layer1_data.fetcher.baostock_client as bsc
        monkeypatch.setattr(bsc, "get_baostock", lambda: _FakeBs(_raw_baostock()))
        df = _fetch_baostock_financial("600000", start_year="2023")
        assert len(df) == 3
        assert df["ts_code"].tolist() == ["600000"] * 3
        # 真实公告日沿用（precise）
        ann = df.set_index("report_date")["ann_date"]
        assert ann.loc[date(2023, 3, 31)] == date(2023, 4, 29)
        assert ann.loc[date(2023, 12, 31)] == date(2024, 4, 27)
        assert df["ann_date_source"].tolist() == [1, 1, 1]
        # ROE 年化（baostock 是年初至今累计口径）：Q1 2.24×4、半年 3.28×2、年报 12.5×1
        assert df.loc[0, "roe_ttm"] == pytest.approx(8.96)
        assert df.loc[1, "roe_ttm"] == pytest.approx(6.56)
        assert df.loc[2, "roe_ttm"] == pytest.approx(12.5)
        # eps_ttm 直接用（baostock 只有 TTM 口径），eps/bps 显式留空
        assert df.loc[0, "eps_ttm"] == pytest.approx(1.62)
        assert df["eps"].isna().all()
        assert df["bps"].isna().all()
        # report_type 由报告期推导（字符串枚举）
        rt = df.set_index("report_date")["report_type"]
        assert rt.loc[date(2023, 3, 31)] == "quarter1"
        assert rt.loc[date(2023, 12, 31)] == "annual"

    def test_baostock_unavailable_returns_empty(self, monkeypatch):
        """baostock 备源本身不可用（网络/未装）→ 返回空表，不抛异常。"""
        import src.layer1_data.fetcher.baostock_client as bsc

        def _boom(code, start_year=2014):
            raise RuntimeError("baostock 服务不可达")

        monkeypatch.setattr(bsc, "get_baostock", lambda: _FakeBs(None))
        # get_baostock 返回的对象 query_financial 抛异常
        class _Bad:
            def query_financial(self, code, start_year=2014):
                raise RuntimeError("连接失败")

        monkeypatch.setattr(bsc, "get_baostock", lambda: _Bad())
        assert _fetch_baostock_financial("600000").empty

    def test_empty_result_returns_empty(self, monkeypatch):
        import src.layer1_data.fetcher.baostock_client as bsc
        monkeypatch.setattr(bsc, "get_baostock", lambda: _FakeBs(pd.DataFrame()))
        assert _fetch_baostock_financial("600000").empty
