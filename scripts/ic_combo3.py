# -*- coding: utf-8 -*-
"""新挖因子 + Combo3 的月度 Rank IC / ICIR 评估 (2021-2025, 本地面板).
方法: 每月末调仓日取因子得分, 与未来21个交易日前瞻收益做Spearman秩相关 -> 月度IC序列.
指标: IC均值 / IC标准差 / ICIR / t统计量 / IC>0胜率 / 分年IC.
输出: outputs/final5/combo3_ic.csv + 控制台汇总。
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

START, END = "2021-01-01", "2025-12-31"
FWD_DAYS = 21
NAMES3 = ["MD-Mom50", "DV-LV50", "GM-LV50"]


def main() -> None:
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache_2125.pkl")
    logger.info(f"面板 {len(p):,} 行")

    # 前瞻收益: 每个交易日t, 未来FWD_DAYS日累计收益 (按ts_code分组shift)
    p2 = p.sort_values(["ts_code", "trade_date"]).copy()
    p2["_fwd"] = p2.groupby("ts_code", sort=False)["close"].pct_change(FWD_DAYS).shift(-FWD_DAYS)
    fwd = p2.set_index(["trade_date", "ts_code"])["_fwd"]

    # 月末调仓日 (每月最后一个交易日)
    td = pd.Series(pd.to_datetime(p["trade_date"].unique())).sort_values()
    month_ends = td.groupby(td.dt.to_period("M")).max()
    me_set = set(month_ends.dt.date)  # 面板trade_date是datetime.date对象

    def score_of(name: str) -> pd.Series:
        if name == "Combo3":
            comps, wtot = {}, 0.0
            for nm in NAMES3:
                f = next(x for x in FACTORS if x["name"] == nm)
                w = {k: v / sum(f["w"].values()) for k, v in f["w"].items()}
                comps[nm] = w
                wtot += 1.0
            val = pd.Series(0.0, index=p.index)
            for nm, w in comps.items():
                for k, col in COMP_COLS.items():
                    if k in w:
                        val += w[k] * p[col].fillna(0.5) / wtot
            return val
        f = next(x for x in FACTORS if x["name"] == name)
        wnorm = {k: v / sum(f["w"].values()) for k, v in f["w"].items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        return val

    results, ics_all = {}, {}
    for name in NAMES3 + ["Combo3"]:
        sc = score_of(name)
        sig = p[p["trade_date"].isin(me_set)][["trade_date", "ts_code"]].copy()
        sig["_s"] = sc[sig.index]
        sig["_fwd"] = list(fwd.loc[list(zip(sig["trade_date"], sig["ts_code"]))])
        sub = sig.dropna(subset=["_s", "_fwd"]).copy()
        # 组内秩 -> 组内Pearson(秩) = Spearman, 向量化实现
        gpre = sub.groupby("trade_date")
        sub["_rs"] = gpre["_s"].rank(method="average")
        sub["_rf"] = gpre["_fwd"].rank(method="average")
        sub["_rp"] = sub["_rs"] * sub["_rf"]
        g = sub.groupby("trade_date")
        m = g[["_rs", "_rf"]].mean()
        ics = (g["_rp"].mean() - m["_rs"] * m["_rf"]) / (g["_rs"].std() * g["_rf"].std())
        ics = ics.dropna()
        ics.index = pd.to_datetime(ics.index)
        n = len(ics)
        mean, sd = ics.mean(), ics.std()
        icir = mean / sd if sd > 0 else np.nan
        tstat = icir * np.sqrt(n)
        results[name] = {
            "IC均值": mean, "IC标准差": sd, "ICIR": icir,
            "t统计量": tstat, "IC>0占比": (ics > 0).mean(), "样本月数": n,
        }
        results[name]["分年IC"] = ics.groupby(ics.index.year).mean().round(4).to_dict()
        ics_all[name] = ics
        logger.info(f"{name}: IC={mean:.4f} ICIR={icir:.3f} t={tstat:.2f} 胜率={(ics>0).mean()*100:.0f}%")

    print("\n" + "=" * 92)
    print(f"  月度Rank IC 评估 (信号日=月末, 前瞻{FWD_DAYS}日收益, 2021-01 ~ 2025-12)")
    print("=" * 92)
    hdr = f"{'因子':<10} {'IC均值':>8} {'IC标准差':>9} {'ICIR':>7} {'t值':>6} {'IC>0占比':>9} {'月数':>5}"
    print(hdr)
    for name, r in results.items():
        print(f"{name:<10} {r['IC均值']:>8.4f} {r['IC标准差']:>9.4f} {r['ICIR']:>7.3f} "
              f"{r['t统计量']:>6.2f} {r['IC>0占比']*100:>8.0f}% {r['样本月数']:>5}")
    print("-" * 92)
    print("分年IC均值:")
    years = sorted({y for r in results.values() for y in r["分年IC"]})
    print(f"{'因子':<10}" + "".join(f"{y:>9}" for y in years))
    for name, r in results.items():
        print(f"{name:<10}" + "".join(f"{r['分年IC'].get(y, float('nan')):>9.4f}" for y in years))
    print("=" * 92)

    df = pd.DataFrame(results).T.drop(columns=["分年IC"])
    df.to_csv(ROOT / "outputs" / "final5" / "combo3_ic.csv", encoding="utf-8-sig")
    pd.DataFrame(ics_all).to_csv(ROOT / "outputs" / "final5" / "combo3_ic_series.csv",
                                 encoding="utf-8-sig")
    logger.info("已保存 outputs/final5/combo3_ic*.csv")


if __name__ == "__main__":
    main()
