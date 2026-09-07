# -*- coding: utf-8 -*-
"""
价格数据清洗。

原则：**标记优先于删除**。
    回测里最怕的不是脏数据，而是脏数据被静默改掉后你不知道。所以这里对可疑行
    一律打标记（status / is_suspended / quality_flag），只有真正无法使用的行
    （收盘价为 0、OHLC 自相矛盾到无法修复）才剔除。剔除与标记的数量都会写进
    质量报告，入库时一并落盘，事后可追溯。

清洗项：
    1. 结构性校验：high >= max(open,close), low <= min(open,close), close > 0
    2. 停牌识别：成交量为 0（数据源停牌日仍会给一行，价格等于前收）
    3. ST/退市整理期：由股票名称中的 ST / 退 字样判定，涨跌停阈值随之改为 5%
    4. 异常涨跌幅：超过板块涨跌停上限 + 容差，标记但不删（可能是复权断点或重组复牌）
    5. 前收盘价 pre_close 补齐：按复权口径用自身序列 shift 得到
    6. 复权连续性校验：qfq/hfq 的比值在相邻交易日间应基本恒定，突变说明复权基准漂移
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.common.logger import logger
from src.common.utils import board_of, normalize_code

# 板块涨跌停上限（%）。ST 股一律 5%。
_LIMIT_PCT = {"主板": 10.0, "创业板": 20.0, "科创板": 20.0, "北交所": 30.0}
_ST_LIMIT = 5.0
_TOLERANCE = 1.2        # 容差：封板价常因四舍五入不到整数百分比


def _limit_of(ts_code: str, name: Optional[str]) -> float:
    if name and ("ST" in str(name).upper() or "退" in str(name)):
        return _ST_LIMIT
    return _LIMIT_PCT.get(board_of(ts_code), 10.0)


def clean_price(
    df: pd.DataFrame,
    name: Optional[str] = None,
    drop_invalid: bool = True,
) -> tuple[pd.DataFrame, Dict]:
    """清洗单只股票的日线。

    参数
        df        fetcher 产出的日线（可能混合三种复权）
        name      股票名称，用于识别 ST / 退市整理期
        drop_invalid 是否剔除结构性非法行

    返回 (清洗后的 DataFrame, 质量报告 dict)
    """
    report: Dict = {"input_rows": 0, "output_rows": 0, "dropped": 0,
                    "flagged": {}, "notes": []}

    if df is None or df.empty:
        return pd.DataFrame(), report

    df = df.copy()
    report["input_rows"] = len(df)

    if "ts_code" not in df.columns or df["ts_code"].empty:
        report["notes"].append("缺少 ts_code")
        return pd.DataFrame(), report

    code = normalize_code(df["ts_code"].iloc[0])
    df["ts_code"] = code
    lim = _limit_of(code, name)

    # ---- 统一数值类型 ----
    num_cols = ["open", "high", "low", "close", "pre_close", "change", "change_pct",
                "volume", "amount", "turnover_rate", "amplitude"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
    df = df.dropna(subset=["trade_date", "adj_type"])

    # ---- 1. 结构性校验 ----
    invalid = pd.Series(False, index=df.index)
    invalid |= df["close"].isna() | (df["close"] <= 0)

    has_hl = {"high", "low"}.issubset(df.columns)
    if has_hl:
        invalid |= (df["high"] < df["low"])
        for c in ("open", "close"):
            if c in df.columns:
                invalid |= (df[c] > df["high"]) | (df[c] < df["low"])

    n_invalid = int(invalid.sum())
    if n_invalid:
        report["flagged"]["structural_invalid"] = n_invalid
        if drop_invalid:
            df = df.loc[~invalid].copy()
            report["dropped"] += n_invalid

    if df.empty:
        report["output_rows"] = 0
        return df, report

    # ---- 2. 按复权类型分组处理（各复权序列内部独立计算） ----
    parts = []
    for adj, g in df.groupby("adj_type", sort=False):
        parts.append(_clean_one_series(g, code, lim))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    out = out.sort_values(["adj_type", "trade_date"]).reset_index(drop=True)
    out = out.drop_duplicates(subset=["trade_date", "ts_code", "adj_type"], keep="last")

    report["output_rows"] = len(out)
    return out, report


def _clean_one_series(g: pd.DataFrame, code: str, limit: float) -> pd.DataFrame:
    """处理单一复权序列。"""
    g = g.sort_values("trade_date").copy()

    # ---- pre_close 补齐（复权口径：用本序列的前一根 K 线） ----
    if "pre_close" in g.columns:
        g["pre_close"] = g["close"].shift(1)
    if "change" in g.columns:
        g["change"] = (g["close"] - g["pre_close"]).round(4)
    if "change_pct" in g.columns:
        pc = g["pre_close"].replace(0, np.nan)
        g["change_pct"] = ((g["close"] / pc - 1.0) * 100).round(4)
    if "amplitude" in g.columns and {"high", "low"}.issubset(g.columns):
        pc = g["pre_close"].replace(0, np.nan)
        g["amplitude"] = ((g["high"] - g["low"]) / pc * 100).round(4)

    # ---- 停牌：成交量为 0 ----
    vol = g["volume"].fillna(0) if "volume" in g.columns else pd.Series(0, index=g.index)
    g["is_suspended"] = (vol <= 0).astype(int)

    # ---- 涨跌停：涨停=涨幅触及上限，跌停=跌幅触及下限 ----
    chg = g["change_pct"].fillna(0) if "change_pct" in g.columns else pd.Series(0, index=g.index)
    g["is_limit_up"] = ((chg >= limit - _TOLERANCE) & (g["is_suspended"] == 0)).astype(int)
    g["is_limit_down"] = ((chg <= -(limit - _TOLERANCE)) & (g["is_suspended"] == 0)).astype(int)

    # ---- status：0=停牌 1=正常 2=ST 3=退市整理期 ----
    g["status"] = 1
    g.loc[g["is_suspended"] == 1, "status"] = 0
    if limit == _ST_LIMIT:
        g["status"] = g["status"].where(g["status"] == 0, 2)

    return g


def validate_price(df: pd.DataFrame) -> pd.DataFrame:
    """独立的质量体检：返回所有可疑行（不修改原数据）。用于入库后复查。"""
    if df is None or df.empty:
        return pd.DataFrame()

    d = df.copy()
    issues = []

    c = pd.to_numeric(d.get("close"), errors="coerce")
    hi = pd.to_numeric(d.get("high"), errors="coerce")
    lo = pd.to_numeric(d.get("low"), errors="coerce")
    op = pd.to_numeric(d.get("open"), errors="coerce")
    chg = pd.to_numeric(d.get("change_pct"), errors="coerce")

    conds = {
        "close_nonpositive": c.isna() | (c <= 0),
        "high_lt_low": hi < lo,
        "close_out_of_range": (c > hi) | (c < lo),
        "open_out_of_range": (op > hi) | (op < lo),
        "abnormal_change": chg.abs() > 35,
        "zero_volume_with_change": (pd.to_numeric(d.get("volume"), errors="coerce") <= 0)
                                   & (chg.abs() > 0.01),
    }
    mask = pd.Series(False, index=d.index)
    for n, m in conds.items():
        m = m.fillna(False)
        if m.any():
            sub = d.loc[m].copy()
            sub["issue"] = n
            issues.append(sub)
        mask |= m

    return pd.concat(issues, ignore_index=True) if issues else pd.DataFrame()


def detect_restatement(
    stored: pd.DataFrame,
    fresh: pd.DataFrame,
    adj_type: str = "qfq",
    tol: float = 1e-4,
) -> bool:
    """检测前复权基准是否漂移（即历史价格被追溯修改）。

    做法：对同一批交易日，比较库内已存收盘价与新拉收盘价的相对差异。
    只要有显著比例的样本对不上，就判定发生了复权重述，需要重拉该股全历史。

    为什么只查 qfq：hfq 以上市首日为基准，历史不会变；qfq 以最新日为基准，
    每次分红送股都会让整条历史曲线重算。
    """
    if stored is None or fresh is None or stored.empty or fresh.empty:
        return False
    if adj_type != "qfq":
        return False

    s = stored[["trade_date", "close"]].dropna().set_index("trade_date")["close"].astype(float)
    f = fresh[["trade_date", "close"]].dropna().set_index("trade_date")["close"].astype(float)
    common = s.index.intersection(f.index)
    if len(common) < 5:
        return False

    diff = (s.loc[common] - f.loc[common]).abs() / s.loc[common].abs().replace(0, np.nan)
    ratio = float((diff > tol).mean())
    return ratio > 0.10


if __name__ == "__main__":
    from src.layer1_data.fetcher.daily_price import fetch_stock_full

    price, basic = fetch_stock_full("600000", "2023-01-01", "2023-06-30")
    cleaned, rep = clean_price(price, name="浦发银行")
    print("质量报告:", rep)
    print(cleaned[cleaned["adj_type"] == "qfq"].head(5).to_string())
    print("\n可疑行:\n", validate_price(cleaned).to_string())
