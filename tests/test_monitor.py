# -*- coding: utf-8 -*-
"""monitor 体检逻辑测试（P1-3 补测）：不依赖数据库，monkeypatch read_sql。

report() 的核心价值是「把原始查询翻译成告警」，覆盖率阈值、过期判定、
质量告警的边界必须可测 —— 数据库查得对不对是另一层的事。
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.layer5_scheduler.monitor as mon_mod
from src.layer5_scheduler.monitor import DataMonitor


def _fake_read_sql(sql: str, params=None):
    """按 SQL 特征分发假数据。"""
    if "MAX(trade_date)" in sql:
        return pd.DataFrame({"d": ["2023-06-30"]})
    if "COUNT(*) AS n" in sql:
        return pd.DataFrame({"n": [100]})          # 全市场 100 只
    if "v_coverage_by_code" in sql:
        return pd.DataFrame({
            "ts_code": [f"{i:06d}" for i in range(80)],     # 只入库 80 只
            "first_date": ["2020-01-02"] * 80,
            # 78 只正常；2 只落后但仍在最近 30 个交易日窗口内（走真实对齐分支）
            "last_date": ["2023-06-30"] * 78 + ["2023-06-20", "2023-06-16"],
            "rows": [1000] * 80,
        })
    if "ts_code FROM stock_basic" in sql:
        return pd.DataFrame({"ts_code": [f"{i:06d}" for i in range(100)]})
    if "ORDER BY trade_date DESC LIMIT 30" in sql:
        cal = pd.bdate_range("2023-05-20", periods=30).strftime("%Y-%m-%d").tolist()
        return pd.DataFrame({"trade_date": cal})
    if "v_data_quality" in sql:
        return pd.DataFrame({"trade_date": ["2023-06-30"],
                             "ts_code": ["000001"],
                             "adj_type": ["qfq"],
                             "issue": ["abnormal_change"]})
    if "update_log" in sql:
        return pd.DataFrame({"ts_code": ["000002"], "message": ["timeout"],
                             "started_at": ["2023-06-30 10:00"]})
    raise AssertionError(f"未预期的 SQL: {sql[:80]}")


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    monkeypatch.setattr(mon_mod, "read_sql", _fake_read_sql)


class TestReport:
    def test_structure(self):
        r = DataMonitor().report()
        assert "error" not in r
        assert r["latest_trade_date"] == "2023-06-30"
        assert r["覆盖率"]["全市场"] == 100
        assert r["覆盖率"]["已入库"] == 80

    def test_low_coverage_alert(self):
        r = DataMonitor().report()
        # 覆盖率 80% < 95% → 告警
        assert any("覆盖率" in a for a in r["告警"])

    def test_stale_detection(self):
        """last_date 落后最近交易日超过阈值的票进 stale_codes。"""
        r = DataMonitor().report(stale_days=5)
        codes = [s["ts_code"] for s in r["stale_codes"]]  # stale_codes 是 records
        # 6/20 距 6/30 约 8 个交易日 > 5 → 过期；6/16 更早必过期
        assert "000078" in codes
        assert "000079" in codes

    def test_quality_issue_alert(self):
        r = DataMonitor().report()
        assert any("数据质量" in a for a in r["告警"])

    def test_recent_failed_captured(self):
        r = DataMonitor().report()
        assert len(r["recent_failed"]) == 1


class TestPrintReport:
    def test_error_path_no_crash(self, capsys):
        mon = DataMonitor()
        mon.print_report({"error": "trade_calendar 无数据"})
        out = capsys.readouterr().out
        assert "监控" in out
