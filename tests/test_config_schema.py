# -*- coding: utf-8 -*-
"""config schema 校验测试（P1-5 产物，P1-3 补测）。

validate_schema 是"配置写错程序直接拒绝启动"的守门员，
枚举/权重和/布尔这三类规则必须有测试，防止校验被悄悄放宽。
"""

from __future__ import annotations

import copy

import pytest

from src.common.config import validate_schema


def _factors_cfg() -> dict:
    return {
        "composite": {"method": "custom",
                      "weights": {"turnover": 0.44, "reversal": 0.31,
                                  "value": 0.25, "profitability": 0.0,
                                  "momentum": 0.0},
                      "fillna": "zero"},
        "factors": {"momentum": {"enabled": True},
                    "reversal": {"enabled": True}},
    }


def _costs_cfg() -> dict:
    return {"slippage": {"model": "liquidity", "base_bps": 2.0,
                         "impact_coef": 15.0}}


def _risk_cfg() -> dict:
    return {"enabled": False,
            "stop_loss": {"enabled": False},
            "circuit_breaker": {"enabled": False}}


class TestEnumRules:
    def test_valid_method_passes(self):
        validate_schema("factors", _factors_cfg())   # 不抛即通过

    def test_unknown_method_rejected(self):
        c = _factors_cfg()
        c["composite"]["method"] = "ic_weight"
        with pytest.raises(ValueError, match="method"):
            validate_schema("factors", c)

    def test_unknown_slip_model_rejected(self):
        c = _costs_cfg()
        c["slippage"]["model"] = "magic"
        with pytest.raises(ValueError, match="slippage.model"):
            validate_schema("costs", c)

    def test_other_config_not_validated(self):
        """costs 配置在 factors 校验下应跳过（路径前缀过滤）。"""
        validate_schema("factors", _costs_cfg())     # 不抛


class TestWeightsRule:
    def test_weights_sum_must_be_close_to_one(self):
        c = _factors_cfg()
        c["composite"]["weights"]["turnover"] = 0.9     # 和变 1.46
        with pytest.raises(ValueError, match="权重之和"):
            validate_schema("factors", c)

    def test_weights_sum_one_passes(self):
        validate_schema("factors", _factors_cfg())

    def test_non_numeric_weight_rejected(self):
        c = _factors_cfg()
        c["composite"]["weights"]["turnover"] = "high"
        with pytest.raises(ValueError):
            validate_schema("factors", c)


class TestBoolRules:
    def test_risk_enabled_must_be_bool(self):
        c = _risk_cfg()
        c["enabled"] = "false"
        with pytest.raises(ValueError, match="enabled"):
            validate_schema("risk", c)

    def test_stop_loss_enabled_must_be_bool(self):
        c = _risk_cfg()
        c["stop_loss"]["enabled"] = 1
        with pytest.raises(ValueError):
            validate_schema("risk", c)

    def test_factor_enabled_must_be_bool(self):
        c = _factors_cfg()
        c["factors"]["reversal"]["enabled"] = "true"
        with pytest.raises(ValueError, match="enabled"):
            validate_schema("factors", c)

    def test_valid_booleans_pass(self):
        validate_schema("risk", _risk_cfg())
