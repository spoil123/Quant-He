# -*- coding: utf-8 -*-
"""pipeline 解析逻辑测试（P1-5 补测）。

run_walk_forward 的 wf_summary 解析曾因 startswith("WF总绩效") 匹配不上
实际输出的 "- WF总绩效(...)" 行首前缀而永远为空 —— 调度器告警拿不到
WF 绩效摘要。parse_wf_summary 已改为正则解析，这里锁定行为。
"""

from __future__ import annotations

import pytest

from src.layer5_scheduler.pipeline import parse_wf_summary


class TestParseWfSummary:
    def test_parses_real_output_shape(self):
        """真实输出行（带行首 '- ' 前缀和尾部调仓次数）能正确解析。"""
        out = (
            "  2023 测试期: 收益 -0.36%  夏普 -0.125  回撤 22.04%\n"
            "================================================================\n"
            "        - WF总绩效(2017-2023)                                     "
            " - -0.1347 -0.0212 -0.1965 0.4383 -0.0484     0\n"
            "================================================================\n"
        )
        s = parse_wf_summary(out)
        assert s == {"测试期": "2017-2023", "总收益": "-0.1347",
                     "年化": "-0.0212", "夏普": "-0.1965",
                     "回撤": "0.4383", "卡玛": "-0.0484"}

    def test_parses_positive_values(self):
        """正收益不丢负号语义。"""
        out = "- WF总绩效(2019-2023) - 0.2315 0.2399 0.9090 0.2901 0.8270 12\n"
        s = parse_wf_summary(out)
        assert s["总收益"] == "0.2315"
        assert s["卡玛"] == "0.8270"

    def test_no_match_returns_empty(self):
        """无 WF 总绩效行 -> 空 dict（不抛错）。"""
        assert parse_wf_summary("") == {}
        assert parse_wf_summary("  2023 测试期: 收益 -0.36%") == {}

    def test_malformed_line_ignored(self):
        """指标数不足的行被忽略而非报错。"""
        out = "- WF总绩效(2017-2023) - -0.1347\n"
        assert parse_wf_summary(out) == {}

    def test_negative_year_range_handled(self):
        """测试期含连字符不干扰指标提取。"""
        out = "- WF总绩效(2015-2024) - 0.10 -0.05 0.3 0.4 0.25 8\n"
        s = parse_wf_summary(out)
        assert s["测试期"] == "2015-2024"
        assert len(s) == 6
