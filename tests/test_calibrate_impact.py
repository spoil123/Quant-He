# -*- coding: utf-8 -*-
"""校准闭环测试：样本来源门控 + costs.yaml 回写 + 留档去重。

背景（这些用例防的是真实踩过的坑）：
1. 模拟撮合样本(broker=paper)回归出的系数曾被当成"实证值"写进配置 ——
   那只是对造数真值的估计误差，写进去是自我循环 + 假实证标签。
2. AUTO-CALIB 块替换时 END 标记缩进丢失（顶格），且重复回写会叠加缩进。
3. impact_calib 留档没有 upsert，同参数跑两次就产生两条内容相同的行，
   一年后没人知道哪条是真的。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import scripts.calibrate_impact as ci
from src.layer5_execution.impact import sample_source


# ================================================================ 样本来源判定

def _df(brokers: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"broker": brokers, "slip_bps": [1.0] * len(brokers)})


class TestSampleSource:
    def test_all_paper_is_simulated(self):
        src, dist = sample_source(_df(["paper"] * 5))
        assert src == "simulated"
        assert dist == {"paper": 5}

    def test_all_qmt_is_real(self):
        src, _ = sample_source(_df(["qmt"] * 5))
        assert src == "real"

    def test_mixed_is_conservative(self):
        src, dist = sample_source(_df(["qmt", "paper"]))
        assert src == "mixed"
        assert len(dist) == 2

    def test_missing_column_is_unknown(self):
        src, dist = sample_source(pd.DataFrame({"slip_bps": [1.0]}))
        assert src == "unknown"
        assert dist == {}

    def test_empty_is_unknown(self):
        src, _ = sample_source(pd.DataFrame({"broker": []}))
        assert src == "unknown"

    def test_case_insensitive(self):
        src, _ = sample_source(_df(["PAPER", "Sim"]))
        assert src == "simulated"


# ================================================================ costs.yaml 回写

@pytest.fixture
def costs_copy(tmp_path, monkeypatch):
    """把真实 costs.yaml 复制到 tmp，让回写测试不碰项目文件。"""
    src = Path("config/costs.yaml").read_text(encoding="utf-8")
    dst = tmp_path / "costs.yaml"
    dst.write_text(src, encoding="utf-8")
    monkeypatch.setattr(ci, "COSTS_YAML", dst)
    return dst


def _result(coef: float = 2.2, base: float = 3.5,
            n: int = 200, r2: float = 0.96) -> dict:
    return {"impact_coef": coef, "base_bps": base, "n": n,
            "r_squared": r2, "method": "ols"}


class TestReplaceHelpers:
    def test_scalar_keeps_trailing_comment(self):
        text = "  impact_coef: 1.0        # 冲击系数（状态见上方）\n"
        new, n = ci._replace_scalar(text, "impact_coef", 2.218226)
        assert n == 1
        assert "impact_coef: 2.218226" in new
        assert "# 冲击系数（状态见上方）" in new      # 注释保留

    def test_scalar_not_found(self):
        new, n = ci._replace_scalar("  foo: 1\n", "impact_coef", 2.0)
        assert n == 0

    def test_block_missing_markers_returns_empty(self):
        assert ci._replace_block("no markers here", ["x"]) == ""


class TestWriteBack:
    def test_updates_coef_and_base(self, costs_copy):
        assert ci.write_back_costs(_result(), "real", {"qmt": 200}, "2026-07")
        import re
        text = costs_copy.read_text(encoding="utf-8")
        # 关键：配置行（非注释）的数值必须真的变了
        # 注意不能只搜 "1.0 not in text" —— AUTO-CALIB 块会记录"1.0 -> 2.2"的沿革
        m = re.search(r"^  impact_coef:\s*([\d.]+)", text, re.M)
        assert m is not None, "未找到 impact_coef 配置行"
        assert float(m.group(1)) == pytest.approx(2.2)
        m2 = re.search(r"^  base_bps:\s*([\d.]+)", text, re.M)
        assert m2 is not None and float(m2.group(1)) == pytest.approx(3.5)

    def test_records_sample_source_and_r2(self, costs_copy):
        ci.write_back_costs(_result(), "real", {"qmt": 200}, "2026-07")
        text = costs_copy.read_text(encoding="utf-8")
        assert "真实券商成交回报" in text
        assert "R²=0.9600" in text
        assert "n=200" in text

    def test_simulated_sample_is_flagged(self, costs_copy):
        """模拟样本被强制作废标注 —— 不允许看起来像实证值。"""
        ci.write_back_costs(_result(), "simulated", {"paper": 300}, "2026-08")
        text = costs_copy.read_text(encoding="utf-8")
        assert "⚠" in text
        assert "不是实证冲击系数" in text

    def test_repeated_write_back_no_indent_drift(self, costs_copy):
        """回写两次：缩进不能一次比一次深（曾经 END 标记丢缩进）。"""
        ci.write_back_costs(_result(coef=2.0), "real", {"qmt": 1}, "p1")
        first = costs_copy.read_text(encoding="utf-8")
        ci.write_back_costs(_result(coef=2.5), "real", {"qmt": 2}, "p2")
        second = costs_copy.read_text(encoding="utf-8")

        for line in second.splitlines():
            if "END AUTO-CALIB" in line or "BEGIN AUTO-CALIB" in line:
                assert line.startswith("  #"), f"标记行缩进漂移: {line!r}"
                assert not line.startswith("   #"), f"缩进叠加: {line!r}"
        # 两次回写后仍只有一对标记
        assert second.count("BEGIN AUTO-CALIB") == 1
        assert second.count("END AUTO-CALIB") == 1
        assert "2.5" in second and second != first

    def test_creates_backup(self, costs_copy, tmp_path):
        ci.write_back_costs(_result(), "real", {"qmt": 1}, "p")
        baks = list(tmp_path.glob("costs.yaml.bak.*"))
        assert len(baks) == 1

    def test_dry_run_leaves_file_untouched(self, costs_copy):
        before = costs_copy.read_text(encoding="utf-8")
        assert ci.write_back_costs(_result(), "real", {"qmt": 1}, "p",
                                   dry_run=True)
        assert costs_copy.read_text(encoding="utf-8") == before

    def test_invalid_result_refuses(self, costs_copy):
        before = costs_copy.read_text(encoding="utf-8")
        bad = {"impact_coef": None, "base_bps": None, "n": 3,
               "r_squared": None, "method": None}
        assert ci.write_back_costs(bad, "real", {"qmt": 1}, "p") is False
        assert costs_copy.read_text(encoding="utf-8") == before

    def test_yaml_still_parses_after_write_back(self, costs_copy, monkeypatch):
        """回写不能把配置文件写成非法 YAML。"""
        import yaml
        ci.write_back_costs(_result(), "real", {"qmt": 200}, "2026-07")
        doc = yaml.safe_load(costs_copy.read_text(encoding="utf-8"))
        assert doc["slippage"]["impact_coef"] == pytest.approx(2.2)
        assert doc["slippage"]["base_bps"] == pytest.approx(3.5)
        assert doc["tiers"]["default"] == "conservative"


# ================================================================ 留档去重

def test_prune_reports_no_dup_on_clean_table(monkeypatch, capsys):
    """表内无重复时 prune 不误删（真实表当前只应有一条留档）。"""
    monkeypatch.setattr(
        ci, "read_sql",
        lambda *a, **k: pd.DataFrame({
            "id": [4], "period_start": ["2026-08-01"], "period_end": ["2026-08-28"],
            "method": ["ols"], "b": [2.226969], "ic": [0.968292], "r2": [0.746851],
        }),
    )
    assert ci.prune_duplicates(dry_run=True) == 0
    assert "无重复记录" in capsys.readouterr().out


def test_prune_detects_duplicates_without_deleting(monkeypatch, capsys):
    """同内容两行 → 识别出后一条为重复；dry-run 不执行删除。"""
    monkeypatch.setattr(
        ci, "read_sql",
        lambda *a, **k: pd.DataFrame({
            "id": [4, 9],
            "period_start": ["2026-08-01", "2026-08-01"],
            "period_end": ["2026-08-28", "2026-08-28"],
            "method": ["ols", "ols"],
            "b": [2.226969, 2.226969],
            "ic": [0.968292, 0.968292],
            "r2": [0.746851, 0.746851],
        }),
    )
    deleted = []
    monkeypatch.setattr(ci, "execute",
                        lambda sql, *a, **k: deleted.append(sql))
    assert ci.prune_duplicates(dry_run=True) == 1
    assert deleted == []                                  # dry-run 不删
    out = capsys.readouterr().out
    assert "id=[9]" in out                                # 保留 id 小的 4，删 9
    assert "保留 id=[4]" in out


def test_prune_deletes_when_not_dry_run(monkeypatch):
    monkeypatch.setattr(
        ci, "read_sql",
        lambda *a, **k: pd.DataFrame({
            "id": [4, 9],
            "period_start": ["2026-08-01"] * 2, "period_end": ["2026-08-28"] * 2,
            "method": ["ols"] * 2, "b": [2.226969] * 2,
            "ic": [0.968292] * 2, "r2": [0.746851] * 2,
        }),
    )
    deleted = []
    monkeypatch.setattr(ci, "execute",
                        lambda sql, *a, **k: deleted.append(sql))
    assert ci.prune_duplicates(dry_run=False) == 1
    assert len(deleted) == 1 and "DELETE FROM impact_calib" in deleted[0]
    assert "9" in deleted[0] and "4" not in deleted[0]
