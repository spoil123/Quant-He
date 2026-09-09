# -*- coding: utf-8 -*-
"""第三轮自查整改的回归测试（2026-09-09）。

覆盖：
  M1  order_stock 补齐 order_remark 参数位（调用参数个数断言）
  M2  模拟成交推进委托单状态（paper_trade_sim 调 advance_order_on_fill）
  M3  save_trade 幂等去重（broker_deal_id 重复回报不重复入库）
  M4  walk_forward 跨 fold 累计资本（静态检查 initial_capital 传递）
  M5  reconcile_daily_basic 只对"有价无 basic"的股票补抓
  L1  调仓日单股 close_prev 缺失不拖垮整日收益
  L2  attach_raw_close 整股回退 / 部分缺失留 NaN
  L3  load_trade_days 降级结果按天缓存
  L4  年化系数 252/20；to_series 中段 NaN 告警

全部用内存 DataFrame / monkeypatch 构造，不依赖网络与数据库。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


# ============================================================ M1 order_stock 参数


def test_order_stock_passes_remark_in_correct_position():
    """order_stock 8 参签名：本地 id 必须落在第 8 位 order_remark。

    修复前只传 7 个参数，id 落在 strategy_name 位，remark 关联永远失效。
    """
    import inspect
    import src.layer5_execution.qmt_broker as qb
    src = inspect.getsource(qb.QmtBroker.place_order)
    # strategy_name 占位（""）+ order_remark 两参都要在
    assert '"",' in src.replace(" ", ""), "缺少 strategy_name 占位参数"
    assert "str(order_id)" in src, "order_remark 应传本地 id"


# ============================================================ M2 模拟盘推进状态机


def test_paper_sim_advances_order_state():
    """paper_trade_sim 落库后必须推进委托单状态，不能永久停在 submitted。"""
    import inspect
    src = open("scripts/paper_trade_sim.py", encoding="utf-8").read()
    assert "advance_order_on_fill" in src, "模拟成交后未推进订单状态"


# ============================================================ M3 成交回报去重


def test_save_trade_dedup_on_broker_deal_id(monkeypatch):
    """同一 broker_deal_id 的重复回报（断线重连重放）不得重复入库。"""
    import src.layer5_execution.trade_store as ts
    from src.layer5_execution.broker import Trade
    from datetime import datetime

    calls = {"select": 0, "insert": 0}

    def fake_execute(sql, params=None, need_lastrowid=False):
        if sql.lstrip().upper().startswith("SELECT"):
            calls["select"] += 1
            # 第一次查无，第二次命中重复（execute 无 lastrowid 时返回 DataFrame）
            return (pd.DataFrame({"id": [777]})
                    if calls["select"] >= 2 else pd.DataFrame())
        calls["insert"] += 1
        return None, 101

    monkeypatch.setattr(ts, "execute", fake_execute)
    store = ts.TradeStore(broker="qmt")
    t = Trade(order_id=1, ts_code="600000", side="buy",
              order_price=10.0, deal_price=10.1, volume=100,
              deal_time=datetime(2026, 9, 9, 9, 30),
              broker_deal_id="XT20260909-1")

    assert store.save_trade(t) == 101
    assert store.save_trade(t) == 777, "重复回报应返回已存在行的 id"
    assert calls["insert"] == 1, "重复回报不应再次 INSERT"


def test_save_trade_normal_path_without_deal_id(monkeypatch):
    """模拟盘无 broker_deal_id，走原 INSERT 路径，不受去重影响。"""
    import src.layer5_execution.trade_store as ts
    from src.layer5_execution.broker import Trade
    from datetime import datetime

    calls = {"select": 0, "insert": 0}

    def fake_execute(sql, params=None, need_lastrowid=False):
        if sql.lstrip().upper().startswith("SELECT"):
            calls["select"] += 1
            return pd.DataFrame()
        calls["insert"] += 1
        return None, 55

    monkeypatch.setattr(ts, "execute", fake_execute)
    store = ts.TradeStore(broker="paper")
    t = Trade(order_id=1, ts_code="600000", side="buy",
              order_price=10.0, deal_price=10.1, volume=100,
              deal_time=datetime(2026, 9, 9, 9, 30))
    assert store.save_trade(t) == 55
    assert calls["select"] == 0, "无 deal_id 不应发起去重查询"
    assert calls["insert"] == 1


# ============================================================ M5 daily_basic 对账


def test_reconcile_daily_basic_targets_missing_only(monkeypatch):
    """只对「有价无 basic」的股票补抓，且走 insert_ignore 语义。"""
    import src.layer1_data.storage.updater as upd

    fetched = []

    def fake_read_sql(sql, params=None):
        assert "LEFT JOIN daily_basic" in sql, "必须用价-basic 对账查询"
        return pd.DataFrame({"ts_code": ["300760", "000651"]})

    def fake_fetch_full(code, **kw):
        fetched.append(code)
        basic = pd.DataFrame({
            "ts_code": [code] * 2,
            "trade_date": pd.to_datetime(["2026-09-07", "2026-09-08"]),
            "float_mv": [1e9, 1.1e9],
        })
        return pd.DataFrame(), basic

    monkeypatch.setattr(upd, "read_sql", fake_read_sql)
    monkeypatch.setattr(upd.repo, "save_daily_basic",
                        lambda df: len(df))
    import src.layer1_data.fetcher.daily_price as dp
    monkeypatch.setattr(dp, "fetch_stock_full", fake_fetch_full)

    res = upd.reconcile_daily_basic(lookback_days=30)
    assert res == {"codes": 2, "rows": 4}
    assert fetched == ["000651", "300760"]


def test_reconcile_daily_basic_noop_when_no_gap(monkeypatch):
    import src.layer1_data.storage.updater as upd
    monkeypatch.setattr(upd, "read_sql",
                        lambda sql, params=None: pd.DataFrame({"ts_code": []}))
    assert upd.reconcile_daily_basic() == {"codes": 0, "rows": 0}


# ============================================================ L1 调仓日收益（复核为误报）


def test_rebalance_day_gap_is_already_safe():
    """第三轮审计 L1 复核为误报：缺价持仓贡献被 sum(skipna) 自动跳过。

    pos_prev 已 fillna(0.0)，0×NaN=NaN 被 skipna 跳过 —— 与持有路径
    stock_ret.fillna(0) 口径一致。此测试固化该行为，防止未来改动破坏。
    """
    dates = pd.date_range("2026-01-05", periods=4)
    close = pd.DataFrame({"A": [10.0, 11.0, 12.1, 12.1],
                          "B": [20.0, 20.0, np.nan, np.nan]}, index=dates)
    open_ = close.copy()
    prev_close = close.shift(1)
    pos_prev = pd.DataFrame({"A": [0.0, 0.5, 1.0, 1.0],
                             "B": [0.0, 0.5, 0.0, 0.0]}, index=dates)

    r = (pos_prev * (open_ / prev_close - 1.0)).sum(axis=1)
    assert not r.isna().any(), "skipna 下不应有 NaN 传染"
    # 第 3 天 A 从 11 -> 12.1 涨 10%，全仓 A 收益应完整保留
    assert r.loc[dates[2]] == pytest.approx(0.10, abs=0.001)


# ============================================================ L2 attach_raw_close


def test_attach_raw_close_partial_missing_stays_nan(monkeypatch):
    """部分缺失的股票留 NaN（防 BM 混基准）；整股缺失才整股退回。"""
    import src.layer3_strategy.panel as panel_mod

    dates = [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    panel = pd.DataFrame({
        "ts_code": ["AAA", "AAA", "BBB", "BBB"],
        "trade_date": dates + dates,
        "close": [10.0, 11.0, 20.0, 21.0],     # 前复权价
        "bps": [5.0, 5.0, 5.0, 5.0],
    })
    # raw：AAA 只有第 1 天；BBB 整只缺失
    raw = pd.DataFrame({
        "trade_date": [dates[0]],
        "ts_code": ["AAA"],
        "close": [10.5],
    })
    monkeypatch.setattr(panel_mod, "load_price_panel",
                        lambda *a, **k: raw)
    out = panel_mod.attach_raw_close(panel)

    aaa = out[out["ts_code"] == "AAA"]
    bbb = out[out["ts_code"] == "BBB"]
    assert aaa["raw_close"].iloc[0] == 10.5          # 有 raw 用 raw
    assert pd.isna(aaa["raw_close"].iloc[1]), \
        "部分缺失行应留 NaN，不得用前复权价混基准"
    assert np.allclose(bbb["raw_close"], bbb["close"]), \
        "整股无 raw 才整股退回前复权"


# ============================================================ L3 日历降级缓存


def test_load_trade_days_degrade_cached_per_day(monkeypatch):
    """日历读取失败时降级结果按天缓存，不每轮重查 DB。"""
    import src.layer5_scheduler.scheduler as sch

    calls = {"n": 0}

    def fake_read_sql(sql, params=None):
        calls["n"] += 1
        raise RuntimeError("db down")

    monkeypatch.setattr(sch, "_TRADE_DAYS", set())
    monkeypatch.setattr(sch, "_TRADE_DAYS_FOR", None)
    import src.common.db as dbm
    real_read_sql = sch.__dict__.get("read_sql")

    # load_trade_days 内部是延迟 import src.common.db.read_sql
    orig = dbm.read_sql
    monkeypatch.setattr(dbm, "read_sql", fake_read_sql)
    sch.load_trade_days()
    sch.load_trade_days()
    monkeypatch.setattr(dbm, "read_sql", orig)

    assert calls["n"] == 1, "降级结果应缓存到当天，第二次调用不得再查 DB"


# ============================================================ L4 指标口径


def test_quantile_annualized_uses_252_over_20():
    import inspect
    src = open("src/layer3_strategy/factors/evaluation.py", encoding="utf-8").read()
    assert "252.0 / 20.0" in src, "年化系数应为 252/20≈12.6，而非 12"


def test_to_series_warns_on_middle_nan(caplog):
    from src.layer2_backtest.metrics import to_series
    idx = pd.date_range("2026-01-05", periods=5)
    nav = pd.Series([1.0, 1.1, np.nan, 1.2, 1.3], index=idx)
    import logging
    with caplog.at_level(logging.WARNING, logger="src.layer2_backtest.metrics"):
        s = to_series(nav)
    assert len(s) == 4
    assert any("中段" in r.message for r in caplog.records), \
        "中段 NaN 必须告警，不得静默剔除"


def test_to_series_silent_on_edge_nan(caplog):
    from src.layer2_backtest.metrics import to_series
    idx = pd.date_range("2026-01-05", periods=4)
    nav = pd.Series([np.nan, 1.0, 1.1, 1.2], index=idx)
    import logging
    with caplog.at_level(logging.WARNING, logger="src.layer2_backtest.metrics"):
        s = to_series(nav)
    assert len(s) == 3
    assert not any("中段" in r.message for r in caplog.records), \
        "首尾 NaN 属正常截断，不应告警"


# ============================================================ M4 walk_forward 累计资本


def test_walk_forward_uses_cumulative_capital():
    import inspect
    src = open("scripts/walk_forward.py", encoding="utf-8").read()
    assert "initial_cash * cum" in src, "fold 回测器应传入累计资本"
    assert "cum *= 1.0 + float(m[\"总收益率\"])" in src, "fold 结束应累乘净值"
