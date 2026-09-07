# -*- coding: utf-8 -*-
"""5 因子的多空组合(分组多空)夏普: 月度调仓, 截面分5组,
   多头=得分最高组(G5), 空头=最低组(G1), 等权, 组内日收益均值。
   多空日收益 = G5 - G1 (成本前, 因子层面口径)。
输出: outputs/final5/final5_longshort_*.csv
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from backtest_final5 import COMP_COLS, FACTORS, build_panel   # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    monthly_rebalance_dates,
)

TRADING_DAYS = 252


def sharpe_maxdd(daily: pd.Series) -> tuple:
    daily = daily.dropna()
    eq = (1 + daily).cumprod()
    years = len(daily) / TRADING_DAYS
    ann = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    shp = daily.mean() / daily.std() * np.sqrt(TRADING_DAYS) if daily.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return ann, shp, -dd


def main() -> None:
    start, end = "2023-01-01", "2025-12-31"
    t0 = time.time()
    cache = ROOT / "outputs" / "final5" / "panel_cache.pkl"
    if cache.exists():
        logger.info("读取面板缓存...")
        p = pd.read_pickle(cache)
    else:
        logger.info("构建面板(一次, 之后缓存到 outputs/final5/panel_cache.pkl)...")
        p = build_panel(start, end)
        cache.parent.mkdir(parents=True, exist_ok=True)
        p.to_pickle(cache)
    logger.info(f"面板 {len(p):,} 行, 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=start, end=end)
    rb_set = set(rb)

    # 日收益矩阵 (日期 × 股票)
    p2 = p.sort_values(["ts_code", "trade_date"])
    p2["_ret1"] = p2.groupby("ts_code", sort=False)["close"].pct_change()
    ret_m = p2.pivot_table(index="trade_date", columns="ts_code", values="_ret1")

    rows = []
    monthly_ls = {}
    diag = {}
    for f in FACTORS:
        wnorm = {k: v / sum(f["w"].values()) for k, v in f["w"].items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val

        # 每个调仓日: 截面分5组 (1=最低分, 5=最高分)
        # 用 rank 分位手工分组, 避免 qcut 在小截面(节假日前后)上重复分箱报错
        sig = p[p["trade_date"].isin(rb_set)][["trade_date", "ts_code", "_score"]].copy()
        def _g5(x: pd.Series) -> pd.Series:
            pct = x.rank(method="first", pct=True)
            return np.minimum(5, np.ceil(pct * 5)).astype(int)
        sig["group"] = sig.groupby("trade_date")["_score"].transform(_g5)
        # 分组信号: 宽表(调仓日 × 股票) → 重索引到全部交易日并前向填充
        sig = sig.rename(columns={"trade_date": "sig_date"})
        gm0 = sig.pivot_table(index="sig_date", columns="ts_code", values="group")
        gm = gm0.reindex(ret_m.index).ffill()
        gm = gm.reindex(columns=ret_m.columns)

        ret_long = ret_m.where(gm == 5).mean(axis=1)
        ret_short = ret_m.where(gm == 1).mean(axis=1)
        ret_top20 = ret_long
        ls = (ret_long - ret_short).dropna()
        ls.index = pd.to_datetime(ls.index)
        monthly_ls[f["name"]] = (1 + ls).resample("ME").prod() - 1

        # 诊断: 5分组各组年化收益 (验证两端表现, 排查分组口径问题)
        gann = {}
        for g in range(1, 6):
            gr = ret_m.where(gm == g).mean(axis=1).dropna()
            eqg = (1 + gr).cumprod()
            gann[g] = eqg.iloc[-1] ** (252 / len(gr)) - 1 if len(gr) > 20 else np.nan
        diag[f["name"]] = gann

        # 10分组口径 (G10-G1): 头部更集中, 选股强度更接近 TopN 组合
        sig10 = p[p["trade_date"].isin(rb_set)][["trade_date", "ts_code", "_score"]].copy()
        def _g10(x: pd.Series) -> pd.Series:
            pct = x.rank(method="first", pct=True)
            return np.minimum(10, np.ceil(pct * 10)).astype(int)
        sig10["group"] = sig10.groupby("trade_date")["_score"].transform(_g10)
        sig10 = sig10.rename(columns={"trade_date": "sig_date"})
        gm0_10 = sig10.pivot_table(index="sig_date", columns="ts_code", values="group")
        gm10 = gm0_10.reindex(ret_m.index).ffill().reindex(columns=ret_m.columns)
        ls10 = (ret_m.where(gm10 == 10).mean(axis=1)
                - ret_m.where(gm10 == 1).mean(axis=1)).dropna()

        ann, shp, mdd = sharpe_maxdd(ls)
        ann10, shp10, mdd10 = sharpe_maxdd(ls10)
        # 多头组单独口径 (对照)
        ann_l, shp_l, mdd_l = sharpe_maxdd(ret_top20.dropna())
        rows.append({
            "因子": f["name"],
            "多空年化": ann, "多空夏普": shp, "多空回撤": mdd,
            "10组多空年化": ann10, "10组多空夏普": shp10, "10组多空回撤": mdd10,
            "多头组夏普": shp_l, "多头组年化": ann_l, "多头组回撤": mdd_l,
        })
        logger.info(f"{f['name']}: 5组多空夏普={shp:.3f} 10组多空夏普={shp10:.3f} | "
                    f"多头组夏普={shp_l:.3f} | 各组年化="
                    + " ".join(f"G{g}={v*100:.1f}%" for g, v in gann.items()))

    df = pd.DataFrame(rows)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "final5"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f"final5_longshort_{tag}.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print(f"  5因子多空组合(成本前, 月度调仓)  {start}~{end}")
    print("=" * 100)
    show = df.copy()
    for c in ("多空年化", "多空回撤", "10组多空年化", "10组多空回撤", "多头组年化", "多头组回撤"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 240)
    print(show.round(3).to_string(index=False))
    print("\n5分组各组年化收益(诊断):")
    for name, gann in diag.items():
        print(f"  {name:10s} " + " ".join(f"G{g}={v*100:6.1f}%" for g, v in gann.items()))
    print("=" * 100)
    print(f"已保存: {out / f'final5_longshort_{tag}.csv'}")


if __name__ == "__main__":
    main()
