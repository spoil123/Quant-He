# -*- coding: utf-8 -*-
"""
因子流水线：YAML 配置 -> 计算原始暴露 -> 预处理 -> 方向调整 -> 合成综合分。

新增一个因子的完整步骤（不需要改本文件）：
    1. config/factors.yaml 的 factors 下加一段配置
    2. 若需要的算子不存在，在 registry.py 里写个函数并 @register
    3. 跑回测，看 IC 和分层收益

合成方式（config.factors.composite.method）：
    equal_weight  各因子标准化后等权平均。最稳，不依赖历史 IC，无过拟合风险。
    ic_weight    用过去 N 期滚动 IC 均值加权。会向近期有效的因子倾斜，
                 但引入了新的超参数，样本外可能更差 —— 默认不用。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.common.config import get_config
from src.common.logger import logger
from src.layer3_strategy.factors.registry import compute_factor
from src.layer3_strategy.preprocessing.normalize import preprocess_panel


def compute_all_factors(
    panel: pd.DataFrame,
    factor_cfg: Optional[dict] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """计算 YAML 中启用因子的原始暴露。

    返回 (带 raw 列的 panel, 因子名列表)
    """
    cfg = factor_cfg or get_config("factors")
    factors = cfg.get("factors", {}) or {}
    out = panel.copy()

    enabled = [n for n, f in factors.items() if (f or {}).get("enabled", True)]
    if not enabled:
        logger.warning("没有启用任何因子")
        return out, []

    for name in enabled:
        f = factors[name] or {}
        col = f"f_{name}"
        out[col] = compute_factor(out, name, f)
        valid = int(out[col].notna().sum())
        logger.info(f"  因子 {name:14s} 原始值  有效样本 {valid:,} / {len(out):,}")

    return out, enabled


def preprocess_all_factors(
    panel: pd.DataFrame,
    factor_names: List[str],
    factor_cfg: Optional[dict] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """对每个因子做 去极值 -> 标准化 -> 中性化，并按 direction 调整方向。

    返回 (panel, 最终得分列名列表)
    """
    cfg = factor_cfg or get_config("factors")
    defaults = cfg.get("defaults", {}) or {}
    factors = cfg.get("factors", {}) or {}
    min_cs = int(defaults.get("min_cross_section", 30))

    out = panel.copy()
    industry_col = "industry" if "industry" in out.columns else None
    mv_col = "float_mv" if "float_mv" in out.columns else None

    score_cols: List[str] = []
    for name in factor_names:
        raw_col = f"f_{name}"
        if raw_col not in out.columns:
            continue

        f = factors.get(name) or {}
        w_cfg = dict(defaults.get("winsorize", {}))
        s_cfg = dict(defaults.get("standardize", {}))
        n_cfg = dict(defaults.get("neutralize", {}))
        # 因子级配置覆盖全局默认
        w_cfg.update(f.get("winsorize") or {})
        s_cfg.update(f.get("standardize") or {})
        n_cfg.update(f.get("neutralize") or {})

        out = preprocess_panel(
            out, raw_col,
            industry_col=industry_col, mv_col=mv_col,
            winsorize_cfg=w_cfg, standardize_cfg=s_cfg, neutralize_cfg=n_cfg,
            min_cross_section=min_cs,
        )

        # 方向调整：direction=-1 的因子取负，使得得分越大越优
        direction = float(f.get("direction", 1))
        score_col = f"{raw_col}_score"
        out[score_col] = direction * out[f"{raw_col}_neut"]
        score_cols.append(score_col)

        n_valid = int(out[score_col].notna().sum())
        logger.info(f"  因子 {name:14s} 处理后  有效 {n_valid:,}  direction={int(direction)}")

    return out, score_cols


def composite_score(
    panel: pd.DataFrame,
    score_cols: List[str],
    factor_cfg: Optional[dict] = None,
) -> pd.Series:
    """把多个因子得分合成为综合分。"""
    cfg = factor_cfg or get_config("factors")
    comp = cfg.get("composite", {}) or {}
    method = comp.get("method", "equal_weight")
    fillna = comp.get("fillna", "zero")

    if not score_cols:
        return pd.Series(np.nan, index=panel.index)

    mat = panel[score_cols].astype(float)
    # 缺失因子按 0（中性）处理，而不是剔除该股票 —— 剔除会让股票池在不同因子间不一致
    if fillna == "zero":
        mat = mat.fillna(0.0)

    if method == "equal_weight":
        score = mat.mean(axis=1)
    elif method == "custom":
        score = _custom_weighted(mat, score_cols, comp)
    else:
        # 2026-08-31：ic_weight/icir_weight 已删除——它们依赖面板里的未来收益列
        # fwd_ret_20d，而生产面板禁止携带该列（防未来函数硬拦截），
        # 生产路径永远走不到，是死配置。需要 IC 加权请在评估脚本里离线算权重、
        # 再写进 factors.yaml 的 custom.weights。
        logger.warning(f"未知合成方式 {method}，退回等权")
        score = mat.mean(axis=1)

    # 全缺失的行置为 NaN，不参与选股
    all_nan = panel[score_cols].isna().all(axis=1)
    score = score.mask(all_nan)

    if comp.get("orthogonalize", False):
        logger.debug("正交化暂未启用（按 factors 顺序施密特正交）")

    return score


def _custom_weighted(mat: pd.DataFrame, score_cols: List[str],
                     comp: dict) -> pd.Series:
    """按配置里的显式权重合成（自动归一化）。

    为什么要有这条路径：ic_weight 依赖面板里的未来收益列 fwd_ret_20d，
    而生产面板不允许携带该列（build_weights/run 有防未来函数硬拦截），
    所以 ic_weight 在生产里永远会静默退回等权 —— 是个死配置。
    显式权重没有这个矛盾：权重是配置常量，不含任何未来信息。
    """
    w_cfg = comp.get("weights", {}) or {}
    raw = {}
    for col in score_cols:
        name = col.replace("f_", "").replace("_score", "")
        raw[col] = float(w_cfg.get(name, 0.0))

    total = sum(raw.values())
    if total <= 0:
        logger.warning("custom 权重全为 0，退回等权")
        return mat.mean(axis=1)

    score = pd.Series(0.0, index=mat.index)
    for col, w in raw.items():
        if w > 0:
            score = score + mat[col] * (w / total)
    return score


def _ic_weighted(mat: pd.DataFrame, panel: pd.DataFrame,
                 score_cols: List[str], comp: dict) -> pd.Series:
    """【已废弃 2026-08-31】滚动 IC 加权——依赖未来收益列 fwd_ret_20d，
    生产面板禁止携带该列，此函数在生产路径不可达。保留签名防止外部引用报错，
    一律退回等权；需要 IC 加权请在评估脚本离线算权重后写进 custom.weights。
    """
    logger.warning("_ic_weighted 已废弃（依赖未来收益列，生产不可用），退回等权")
    return mat.mean(axis=1)


def run_factor_pipeline(
    panel: pd.DataFrame,
    factor_cfg: Optional[dict] = None,
) -> Tuple[pd.DataFrame, List[str], str]:
    """一键跑通：计算 -> 预处理 -> 合成。

    返回 (panel, score_cols, composite_col)
    """
    cfg = factor_cfg or get_config("factors")
    logger.info("因子流水线：计算原始暴露")
    panel, names = compute_all_factors(panel, cfg)

    logger.info("因子流水线：去极值 / 标准化 / 中性化")
    panel, score_cols = preprocess_all_factors(panel, names, cfg)

    # P2-7 数据质量闸门：因子有效样本率报告（低于阈值告警，缺失率突变可发现）
    try:
        from src.layer3_strategy.factors.coverage import factor_coverage_report
        factor_coverage_report(panel, score_cols)
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"因子覆盖率报告失败（不影响主流程）: {type(e).__name__}: {e}")

    logger.info(f"因子流水线：合成（{cfg.get('composite', {}).get('method', 'equal_weight')}）")
    panel["composite_score"] = composite_score(panel, score_cols, cfg)

    return panel, score_cols, "composite_score"
