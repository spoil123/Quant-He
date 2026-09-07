# -*- coding: utf-8 -*-
"""
因子注册表 + 计算引擎（YAML 驱动）。

设计目标：**新增因子不改 Python 代码**。
    在 config/factors.yaml 里加一段：
        my_factor:
            enabled: true
            handler: momentum        # 已注册的函数名
            params: {window: 120}
            direction: 1
    即可。若要的算子不存在，才需要来这里写一个函数并用 @register 注册。

所有因子函数的约定：
    输入  panel: 包含 trade_date / ts_code 及各类原始字段的 DataFrame
    输出  Series，索引与 panel 对齐，值为原始因子暴露（未做去极值/标准化）
    时序计算必须用 groupby(ts_code) 后再 shift/rolling，绝不跨股票。

关于 direction：
    1  = 因子值越大越好（动量、EP、ROE）
    -1 = 因子值越小越好（反转、换手率）
    方向在 composite 阶段统一处理，因子函数只负责算出原始值。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from src.common.logger import logger

FACTOR_REGISTRY: Dict[str, Callable] = {}


def register(name: str) -> Callable:
    """注册因子函数。"""
    def deco(fn: Callable) -> Callable:
        if name in FACTOR_REGISTRY:
            logger.warning(f"因子 {name} 被重复注册，后者覆盖前者")
        FACTOR_REGISTRY[name] = fn
        return fn
    return deco


# ================================================================ 价量因子

@register("momentum")
def momentum(panel: pd.DataFrame, window: int = 250, skip: int = 21,
             price_field: str = "close", **kw) -> pd.Series:
    """动量：过去一年收益，剔除最近一月。

    为什么跳过最近 skip 天：A 股短期存在显著反转效应，最近一个月的涨幅
    与未来收益负相关。把它混进动量因子会相互抵消，所以要剥掉。
    这是 Jegadeesh-Titman 的经典做法。
    """
    c = panel.groupby("ts_code", sort=False)[price_field]
    return c.shift(skip) / c.shift(skip + window) - 1.0


@register("reversal")
def reversal(panel: pd.DataFrame, window: int = 20,
             price_field: str = "close", **kw) -> pd.Series:
    """反转：过去 N 日收益率（因子方向为 -1，涨得多的扣分）。"""
    c = panel.groupby("ts_code", sort=False)[price_field]
    return panel[price_field] / c.shift(window) - 1.0


@register("turnover")
def turnover(panel: pd.DataFrame, window: int = 20, field: str = "turnover_rate",
             **kw) -> pd.Series:
    """换手率：过去 N 日平均换手率（方向 -1，低换手更优）。"""
    if field not in panel.columns:
        # 兜底：用成交量 / 流通股本自行计算（此时 field 需为百分数口径）
        if {"volume", "float_share"}.issubset(panel.columns):
            vol = panel["volume"]
            share = panel["float_share"].replace(0, np.nan)
            raw = vol / share * 100
        else:
            logger.warning("换手率：缺少 turnover_rate 且无法兜底计算")
            return pd.Series(np.nan, index=panel.index)
    else:
        raw = panel[field]

    s = raw.copy()
    s.index = panel.index
    return s.groupby(panel["ts_code"], sort=False).transform(
        lambda x: x.rolling(window, min_periods=max(5, window // 2)).mean()
    )


@register("volatility")
def volatility(panel: pd.DataFrame, window: int = 60,
               price_field: str = "close", **kw) -> pd.Series:
    """波动率：过去 N 日收益率标准差（方向 -1，低波更优）。"""
    c = panel.groupby("ts_code", sort=False)[price_field]
    ret = panel[price_field] / c.shift(1) - 1.0
    return ret.groupby(panel["ts_code"], sort=False).transform(
        lambda x: x.rolling(window, min_periods=max(20, window // 2)).std()
    )


# ================================================================ 基本面因子

@register("ep_ratio")
def ep_ratio(panel: pd.DataFrame, numerator: str = "eps_ttm",
             denominator: str = "close", **kw) -> pd.Series:
    """盈利收益率 EP = 每股收益TTM / 股价。

    等价于「净利润 / 总市值」，因为分子分母的股本约掉了。
    这样不需要总股本，也不依赖市值接口 —— 数据源依赖最少。
    """
    if numerator not in panel.columns:
        logger.warning(f"EP 因子缺少列 {numerator}")
        return pd.Series(np.nan, index=panel.index)
    num = pd.to_numeric(panel[numerator], errors="coerce")
    den = pd.to_numeric(panel[denominator], errors="coerce").replace(0, np.nan)
    return num / den


@register("bp_ratio")
def bp_ratio(panel: pd.DataFrame, numerator: str = "bps",
             denominator: str = "close", **kw) -> pd.Series:
    """账面市值比 BP = 每股净资产 / 股价（PB 的倒数）。"""
    if numerator not in panel.columns:
        return pd.Series(np.nan, index=panel.index)
    num = pd.to_numeric(panel[numerator], errors="coerce")
    den = pd.to_numeric(panel[denominator], errors="coerce").replace(0, np.nan)
    return num / den


@register("roe_ttm")
def roe_ttm(panel: pd.DataFrame, source_field: str = "roe_ttm", **kw) -> pd.Series:
    """ROE(TTM)：数据层已按「年初至今」口径年化，直接取用。"""
    if source_field not in panel.columns:
        logger.warning(f"ROE 因子缺少列 {source_field}")
        return pd.Series(np.nan, index=panel.index)
    return pd.to_numeric(panel[source_field], errors="coerce")


@register("growth")
def growth(panel: pd.DataFrame, source_field: str = "net_profit_growth", **kw) -> pd.Series:
    """成长：净利润同比增速。"""
    if source_field not in panel.columns:
        return pd.Series(np.nan, index=panel.index)
    return pd.to_numeric(panel[source_field], errors="coerce")


# ================================================================ 计算引擎

def compute_factor(panel: pd.DataFrame, name: str, cfg: dict) -> pd.Series:
    """按配置计算单个因子。返回原始暴露（未预处理）。"""
    handler = cfg.get("handler") or name
    params = dict(cfg.get("params") or {})

    if "expr" in cfg and cfg["expr"]:
        # 表达式模式：直接在已有列上做四则运算
        try:
            return panel.eval(cfg["expr"])
        except Exception as e:                            # noqa: BLE001
            logger.error(f"因子 {name} 表达式求值失败: {e}")
            return pd.Series(np.nan, index=panel.index)

    fn = FACTOR_REGISTRY.get(handler)
    if fn is None:
        logger.error(f"因子 {name} 的 handler '{handler}' 未注册，"
                     f"已注册: {sorted(FACTOR_REGISTRY)}")
        return pd.Series(np.nan, index=panel.index)

    try:
        s = fn(panel, **params)
    except Exception as e:                                # noqa: BLE001
        logger.error(f"因子 {name} 计算失败: {type(e).__name__}: {e}")
        return pd.Series(np.nan, index=panel.index)

    if isinstance(s, pd.Series):
        s.index = panel.index
        return pd.to_numeric(s, errors="coerce")
    return pd.Series(np.nan, index=panel.index)


def list_factors() -> List[str]:
    return sorted(FACTOR_REGISTRY.keys())


if __name__ == "__main__":
    print("已注册因子:", list_factors())
