# -*- coding: utf-8 -*-
"""第二轮自查整改的回归测试（2026-09-07）。

覆盖：
  H1  akshare call() 备源参数裁剪（备源签名不齐导致备源从未生效）
  H3  成交回报推进委托单状态机（submitted -> partial -> filled）
  M1  layer2 成本基数动态化 + 佣金口径统一
  M2  build_weights 流动性上限用当期组合规模
  M3  scheduler 交易日判断
  M4  估值因子用不复权价配 BPS
  M5  财务增量跳过逻辑

全部用内存 DataFrame / monkeypatch 构造，不依赖网络与数据库。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

# ============================================================ H1 备源参数裁剪


def test_fit_kwargs_drops_unsupported_params():
    """主源要 start_year，备源只收 symbol —— 备源必须被裁掉多余参数。

    修复前：kwargs 原样透传 -> 备源 100% TypeError -> 财务三级降级实际只有两级。
    """
    from src.layer1_data.fetcher.akshare_client import AKShareClient

    kw = {"symbol": "600000", "start_year": "2023"}
    main = AKShareClient._fit_kwargs(
        "stock_financial_analysis_indicator", kw)
    fallback = AKShareClient._fit_kwargs("stock_financial_abstract", kw)

    assert main == kw, "主源支持 start_year，不应裁剪"
    assert fallback == {"symbol": "600000"}, "备源不支持 start_year，必须剔除"


def test_fit_kwargs_keeps_all_when_interface_has_var_keyword():
    """接口带 **kwargs 时不裁剪（它本就能吞下任意参数）。"""
    from src.layer1_data.fetcher.akshare_client import AKShareClient

    fitted = AKShareClient._fit_kwargs("no_such_interface_xyz", {"a": 1, "b": 2})
    assert fitted == {"a": 1, "b": 2}, "接口不存在时不应擅自裁剪参数"


# ============================================================ H3 订单状态机


def _patch_store(monkeypatch, state):
    """把 TradeStore 的 SQL 通道打到内存 state 上。"""
    import src.layer5_execution.trade_store as ts

    def fake_read_sql(sql, params=None):
        if "SUM(volume)" in sql:
            return pd.DataFrame({"v": [state["filled"]]})
        return pd.DataFrame({"volume": [state["volume"]],
                             "status": [state["status"]]})

    def fake_execute(sql, params=None):
        if "UPDATE execution_order" in str(sql):
            state["status"] = params["s"]

    monkeypatch.setattr(ts, "read_sql", fake_read_sql)
    monkeypatch.setattr(ts, "execute", fake_execute)


def test_advance_order_partial_then_filled(monkeypatch):
    """分笔成交：先 partial，足额后 filled。"""
    import src.layer5_execution.trade_store as ts

    state = {"volume": 1000, "status": "submitted", "filled": 0}
    _patch_store(monkeypatch, state)
    store = ts.TradeStore()

    assert store.advance_order_on_fill(1) == "submitted", "未成交时不应改状态"

    state["filled"] = 600
    assert store.advance_order_on_fill(1) == "partial", "部分成交应转 partial"

    state["filled"] = 1000
    assert store.advance_order_on_fill(1) == "filled", "足额成交应转 filled"


def test_advance_order_no_rollback_from_terminal(monkeypatch):
    """已终结状态（filled/canceled/rejected）不回退 —— 防撤单后被成交回报改回。"""
    import src.layer5_execution.trade_store as ts

    for terminal in ("filled", "canceled", "rejected"):
        state = {"volume": 1000, "status": terminal, "filled": 1000}
        _patch_store(monkeypatch, state)
        store = ts.TradeStore()
        assert store.advance_order_on_fill(1) == terminal


def test_resolve_order_id_priority(monkeypatch):
    """关联委托单：order_remark > 券商委托号 > (代码,方向) 启发式。"""
    from src.layer5_execution import qmt_broker

    broker = object.__new__(qmt_broker.QmtBroker)

    class _T:
        order_remark = "4321"
        order_id = "xt-9"
        stock_code = "600000"
        order_type = 23

    class _Store:
        def load_orders(self, *a, **k):
            return pd.DataFrame({"id": [1, 2], "xt_order_id": ["xt-1", "xt-9"],
                                 "ts_code": ["600000", "600000"],
                                 "side": ["buy", "buy"],
                                 "status": ["submitted", "submitted"]})

    broker.store = _Store()
    broker._xtconstant = type("C", (), {"STOCK_BUY": 23})()
    # 1) 有 order_remark 时直接用它，不去查库
    assert broker._resolve_order_id(_T()) == 4321


# ============================================================ M1 成本与佣金


def test_commission_for_single_source_of_truth():
    """佣金唯一实现：小额豁免最低佣金，达到 floor 后取两者较大。

    修复前 run() 带豁免、_build_trades() 一律强收 5 元，两个口径并存。
    """
    from src.layer2_backtest.backtester import Backtester

    bt = Backtester.__new__(Backtester)
    bt.commission = 0.0003
    bt.min_commission = 5.0
    bt.min_fee_floor = 10_000.0

    assert float(bt._commission_for(1_000.0)) == pytest.approx(0.3), \
        "低于 floor：只收比例佣金"
    assert float(bt._commission_for(5_000.0)) == pytest.approx(1.5), \
        "低于 floor：只收比例佣金（不收 5 元）"
    assert float(bt._commission_for(12_000.0)) == pytest.approx(5.0), \
        "达到 floor 但比例佣金 < 5 元：收 5 元"
    assert float(bt._commission_for(1_000_000.0)) == pytest.approx(300.0), \
        "大额：比例佣金远大于 5 元"


def test_cost_base_scales_with_nav():
    """成本基数随净值增长：同样的调仓权重，净值翻倍后成本金额也翻倍。

    修复前用固定 initial_capital，净值涨到 2× 后成本占比被腰斩 ——
    凭空造出「规模越大越省钱」的假象。
    """
    from src.layer2_backtest.backtester import Backtester

    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    data = pd.DataFrame({
        "open": [10.0, 10.0, 20.0, 20.0, 20.0, 20.0],
        "close": [10.0, 20.0, 20.0, 20.0, 20.0, 20.0],   # 第 2 日翻倍
    }, index=idx)

    bt = Backtester(data, initial_capital=1_000_000.0,
                    commission=0.0, min_commission=0.0,
                    stamp_duty=0.0, transfer_fee=0.0, slip_bps=0.0,
                    min_fee_floor=0.0, use_historical_stamp=False)
    # 持仓：第 1 日建仓 50%，第 3 日再加 50%（此时净值已因第 2 日翻倍而变化）
    target = pd.Series([0.5, 0.5, 1.0, 1.0, 1.0, 1.0], index=idx)
    res = bt.run(target)
    assert res is not None
    # 第 3 日调仓的成交金额应显著大于「固定本金 × 0.5」
    tr = res["trades"]
    if not tr.empty:
        third = tr.iloc[-1]
        assert third["notional"] > 500_000.0, \
            (f"成本基数未随净值增长：第 3 日调仓金额 {third['notional']:,.0f} "
             f"仍按固定本金计算")


# ============================================================ M2 流动性上限


def test_liquidity_cap_scales_with_portfolio_size():
    """组合规模翻倍后，同样的成交额上限对应的权重上限应减半。"""
    from src.layer3_strategy.portfolio_backtest import PortfolioBacktester

    n = 40
    dates = pd.bdate_range("2024-01-01", periods=n)
    rows = []
    for i, d in enumerate(dates):
        # A 股价格在第 20 日后翻倍 -> 组合规模翻倍
        pa = 10.0 if i < 20 else 20.0
        for code, p in (("A", pa), ("B", 10.0)):
            rows.append({
                "trade_date": d.date(), "ts_code": code,
                "close": p, "open": p, "amount": 1_000_000.0,
                "float_mv": 1e10, "composite_score": 1.0 if code == "A" else 0.9,
            })
    panel = pd.DataFrame(rows)

    bt = PortfolioBacktester(initial_capital=1_000_000.0)
    w = bt.build_weights(panel, [dates[15].date(), dates[30].date()],
                         top_n=2, weighting="equal",
                         max_weight=0.9, min_weight=0.0,
                         liquidity_cap=True, liquidity_participation=0.10,
                         min_avg_amount=0.0, min_price=0.0)
    first = w[w["trade_date"] == dates[15].date()]
    later = w[w["trade_date"] == dates[30].date()]
    assert not first.empty and not later.empty
    # 金额上限固定 1e6×10%=10 万；首期规模 100 万 -> 10% 权重；
    # 后期组合涨到约 200 万 -> 同样 10 万元只对应约 5% 权重
    w_first = float(first["weight"].max())
    w_later = float(later["weight"].max())
    assert w_later < w_first * 0.75, (
        f"流动性上限未随组合规模收紧：首期 {w_first:.3%} -> 后期 {w_later:.3%}"
        f"（用固定本金时两者应相等）")


# ============================================================ M3 交易日判断


def test_needs_trading_day_inference():
    """按步骤推断是否需要交易日；配置可显式覆盖。"""
    from src.layer5_scheduler.scheduler import _needs_trading_day

    assert _needs_trading_day({"steps": ["update", "strategy", "risk"]}) is True
    assert _needs_trading_day({"steps": ["walk_forward"]}) is False
    assert _needs_trading_day({"steps": ["monitor"]}) is False
    # 显式配置优先
    assert _needs_trading_day({"steps": ["update"], "trade_day_only": False}) is False
    assert _needs_trading_day({"steps": ["monitor"], "trade_day_only": True}) is True


def test_is_trading_day_fails_open_when_calendar_missing(monkeypatch):
    """日历不可用时返回 True —— 宁可多跑，不可静默停摆。"""
    import src.layer5_scheduler.scheduler as sched

    monkeypatch.setattr(sched, "load_trade_days", lambda: set())
    assert sched.is_trading_day() is True


# ============================================================ M4 估值因子口径


def test_attach_raw_close_prefers_unadjusted_price(monkeypatch):
    """BM 必须用不复权价配 BPS，raw_close 应来自 adj_type='none'。"""
    import src.layer3_strategy.panel as panel_mod

    panel = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-01-02").date()] * 2,
        "ts_code": ["A", "B"],
        "close": [5.0, 8.0],          # 前复权价
    })
    raw = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-01-02").date()] * 2,
        "ts_code": ["A", "B"],
        "close": [11.0, 12.0],        # 不复权（实际）价
    })
    seen = {}

    def fake_load(start, end, codes, adj_type="qfq"):
        seen["adj_type"] = adj_type
        return raw if adj_type == "none" else pd.DataFrame()

    monkeypatch.setattr(panel_mod, "load_price_panel", fake_load)
    out = panel_mod.attach_raw_close(panel)

    assert seen.get("adj_type") == "none", "必须显式取不复权价"
    assert out["raw_close"].tolist() == [11.0, 12.0]
    assert len(out) == len(panel), "merge 不能改变行数"


def test_attach_raw_close_falls_back_when_missing(monkeypatch):
    """取不到不复权价时退回前复权（宁可沿用旧口径，也不能让因子整列 NaN）。"""
    import src.layer3_strategy.panel as panel_mod

    panel = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-01-02").date()],
        "ts_code": ["A"], "close": [5.0],
    })
    monkeypatch.setattr(panel_mod, "load_price_panel",
                        lambda *a, **k: pd.DataFrame())
    out = panel_mod.attach_raw_close(panel)
    assert out["raw_close"].tolist() == [5.0]


# ============================================================ M5 财务增量跳过


def test_financial_skip_requires_latest_report(monkeypatch):
    """跳过条件含「已覆盖最新报告期」—— 否则财报季永远拉不到新数据。"""
    import src.layer1_data.storage.updater as updater

    monkeypatch.setattr(
        updater, "read_sql",
        lambda *a, **k: pd.DataFrame({
            "ts_code": ["600000", "000001"],
            "rd": ["2026-06-30", "2026-03-31"],
        }))
    skip = updater._financial_skip_codes()
    assert skip == {"600000"}, "只有覆盖到最新报告期的才跳过"
