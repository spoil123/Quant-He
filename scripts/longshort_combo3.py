# -*- coding: utf-8 -*-
"""Combo3 (MD-Mom50+DV-LV50+GM-LV50 等权合成) 的多空口径夏普: 2021-2025.
月度调仓, 截面分5/10组, 多头=最高分组, 空头=最低分组, 等权, 成本前。
同时给出多头组(等权G5)对照。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from backtest_final5 import COMP_COLS, FACTORS  # noqa: E402
from src.common.logger import logger            # noqa: E402
from src.layer3_strategy.portfolio_backtest import monthly_rebalance_dates  # noqa: E402

START, END = "2021-01-01", "2025-12-31"
TRADING_DAYS = 252
NAMES3 = ["MD-Mom50", "DV-LV50", "GM-LV50"]


def sharpe_maxdd(daily: pd.Series) -> tuple:
    daily = daily.dropna()
    eq = (1 + daily).cumprod()
    years = len(daily) / TRADING_DAYS
    ann = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    shp = daily.mean() / daily.std() * np.sqrt(TRADING_DAYS) if daily.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return ann, shp, -dd


def group_matrix(p: pd.DataFrame, score: pd.Series, rb_set: set,
                 ret_m: pd.DataFrame, n_g: int) -> pd.DataFrame:
    sig = p[p["trade_date"].isin(rb_set)][["trade_date", "ts_code"]].copy()
    sig["_s"] = score[sig.index]

    def _gn(x: pd.Series) -> pd.Series:
        m = x.notna()
        out = pd.Series(np.nan, index=x.index)
        pct = x[m].rank(method="first", pct=True)
        out[m] = np.minimum(n_g, np.ceil(pct * n_g))
        return out

    sig["group"] = sig.groupby("trade_date")["_s"].transform(_gn)
    gm0 = sig.rename(columns={"trade_date": "sig_date"}).pivot_table(
        index="sig_date", columns="ts_code", values="group")
    return gm0.reindex(ret_m.index).ffill().reindex(columns=ret_m.columns)


def main() -> None:
    cache = ROOT / "outputs" / "final5" / "panel_cache_2125.pkl"
    p = pd.read_pickle(cache)
    logger.info(f"面板 {len(p):,} 行")

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=START, end=END)
    rb_set = set(rb)

    p2 = p.sort_values(["ts_code", "trade_date"])
    p2["_ret1"] = p2.groupby("ts_code", sort=False)["close"].pct_change()
    ret_m = p2.pivot_table(index="trade_date", columns="ts_code", values="_ret1")

    # Combo3 复合得分: 三因子截面得分等权平均
    combo_score = pd.Series(0.0, index=p.index)
    for name in NAMES3:
        f = next(x for x in FACTORS if x["name"] == name)
        wnorm = {k: v / sum(f["w"].values()) for k, v in f["w"].items()}
        for k, col in COMP_COLS.items():
            if k in wnorm:
                combo_score += wnorm[k] * p[col].fillna(0.5) / len(NAMES3)

    out = {}
    for n_g in (5, 10):
        gm = group_matrix(p, combo_score, rb_set, ret_m, n_g)
        ret_long = ret_m.where(gm == n_g).mean(axis=1)
        ret_short = ret_m.where(gm == 1).mean(axis=1)
        ls = (ret_long - ret_short).dropna()
        out[f"{n_g}组多空"] = sharpe_maxdd(ls)
        if n_g == 5:
            out["多头组(等权G5)"] = sharpe_maxdd(ret_long.dropna())
            # 分组诊断
            for g in range(1, 6):
                gr = ret_m.where(gm == g).mean(axis=1).dropna()
                eqg = (1 + gr).cumprod()
                out[f"G{g}"] = (eqg.iloc[-1] ** (252 / len(gr)) - 1, np.nan, np.nan)

    print("\n" + "=" * 88)
    print(f"  Combo3 多空口径 (2021-01 ~ 2025-12, 月度调仓, 等权分组, 成本前)")
    print("=" * 88)
    for k, (ann, shp, mdd) in out.items():
        if k.startswith("G"):
            print(f"  {k:<14} 年化 {ann*100:7.2f}%")
        else:
            print(f"  {k:<14} 年化 {ann*100:7.2f}%  夏普 {shp:6.3f}  回撤 {mdd*100:6.2f}%")
    print("=" * 88)

    df = pd.DataFrame([{"口径": k, "年化": a, "夏普": s, "回撤": m}
                       for k, (a, s, m) in out.items()])
    df.to_csv(ROOT / "outputs" / "final5" / "combo3_longshort.csv",
              index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
