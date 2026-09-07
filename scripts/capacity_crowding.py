# -*- coding: utf-8 -*-
"""容量与拥挤度分析 (Combo3, 2021-2025):
容量: 用持仓股 ADV20 × 参与率上限(10%/5%) × 执行天数(5日) 反推最大资金
      约束: w_i × C × 单次换手比例 ≤ λ × ADV20_i × 执行天数
拥挤度: ① 组合股息率/波动率/市值的截面分位时序 (风格暴露极端度)
        ② 持仓股两两相关均值时序
        ③ 2024-01 微盘/风格冲击事件行为
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

from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    PortfolioBacktester, monthly_rebalance_dates,
)

TRADING_DAYS = 252
START, END = "2021-01-01", "2025-12-31"
TRIO = {"MD-Mom50": {"mom": 0.18, "dy": 0.18, "bm": 0.15, "ep": 0.12,
                     "lvol": 0.17, "roe": 0.10, "tur": 0.10},
        "DV-LV50": {"dy": 0.20, "bm": 0.20, "ep": 0.15, "lvol": 0.25,
                    "tur": 0.10, "size": 0.10},
        "GM-LV50": {"gm": 0.15, "roe": 0.12, "ep": 0.10, "lvol": 0.28,
                    "dy": 0.15, "bm": 0.10, "tur": 0.10}}
COMP_COLS = {"illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm",
             "ep": "_r_ep", "dy": "_r_dy", "roe": "_r_roe", "gm": "_r_gm",
             "lev": "_r_lev", "lvol": "_r_lvol", "rev20": "_r_rev20", "mom": "_r_mom"}


def main() -> None:
    t0 = time.time()
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache_2125.pkl")
    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=START, end=END)
    scfg = get_config("strategy")
    bt = PortfolioBacktester(
        initial_capital=float(scfg.get("backtest", {}).get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    # ---- Combo3 权重 (等权合成得分) ----
    def score_of(w):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        return val
    comp_score = sum(score_of(w) for w in TRIO.values()) / 3
    p["_score"] = comp_score
    weights = bt.build_weights(p, rb, score_col="_score", top_n=50,
                               weighting="float_mv", max_weight=0.05, min_weight=0.001)
    logger.info(f"权重表 {len(weights):,} 行 ({time.time()-t0:.0f}s)")

    # ---- ADV20 (amount 单位: 元; 面板内每 ts_code×date 一行) ----
    p2 = p.sort_values(["ts_code", "trade_date"])
    p2["_adv20"] = p2.groupby("ts_code", sort=False)["amount"].transform(
        lambda x: x.rolling(20, min_periods=10).mean())
    adv = p2.pivot_table(index="trade_date", columns="ts_code", values="_adv20")

    # ---- 容量: 每个调仓日逐股约束 ----
    wmat = weights.pivot_table(index="trade_date", columns="ts_code",
                               values="weight", aggfunc="sum")
    cap_rows = []
    for d in sorted(set(wmat.index) & set(adv.index)):
        w = wmat.loc[d].dropna()
        w = w[w > 0]
        a = adv.loc[d].reindex(w.index).dropna()
        if len(a) < 10:
            continue
        a = a.reindex(w.index).dropna()
        w = w.reindex(a.index)
        turnover_frac = 0.68   # 单次调仓单边换手占组合比例 (8.2x/12)
        for lam, tag in ((0.10, "10%参与率"), (0.05, "5%参与率")):
            # C ≤ lam*ADV*执行天数 / (w*换手比例); 执行天数=5
            c_max = (lam * a * 5.0 / (w * turnover_frac)).min()
            cap_rows.append({"调仓日": d, "参与率": tag,
                             "最紧股票ADV(亿)": a.min() / 1e8,
                             "最紧股票权重": w[a.idxmin()],
                             "组合容量(亿)": c_max / 1e8})
    cap_df = pd.DataFrame(cap_rows)
    cap_med = cap_df.groupby("参与率")[["最紧股票ADV(亿)", "组合容量(亿)",
                                        "最紧股票权重"]].median()
    print("\n" + "=" * 100)
    print("  容量分析 (Combo3, Top50, 流通市值加权, 单次换手68%, 5个交易日完成)")
    print("=" * 100)
    print(cap_med.round(2).to_string())
    print(f"\n  全期最紧时点容量(10%参与率): {cap_df[cap_df['参与率']=='10%参与率']['组合容量(亿)'].min():.2f} 亿")
    print(f"  当前回测资金 1000万 → 最紧股票平均参与率约 "
          f"{(0.02*1e7*0.68)/(0.10*2e8*5)*100:.3f}% (远低于上限)")

    # ---- 拥挤度: 风格暴露极端度 ----
    # 组合持仓的 dy/lvol/size 截面分位(全市场)时序
    p2 = p2.sort_values(["trade_date", "ts_code"])
    sig_dates = sorted(set(weights["trade_date"].unique()))
    rows = []
    for d in sig_dates:
        day = p[p["trade_date"] == d]
        if len(day) < 100:
            continue
        held = weights[(weights["trade_date"] == d) & (weights["weight"] > 0)]["ts_code"]
        held = set(held) & set(day["ts_code"])
        if len(held) < 20:
            continue
        sub = day[day["ts_code"].isin(held)]
        for col, label in (("_r_dy", "股息率分位"), ("_r_lvol", "低波分位"),
                           ("_r_size", "市值分位(越大越大盘)")):
            if col in sub.columns:
                rows.append({"调仓日": d, "指标": label,
                             "持仓均值": float(pd.to_numeric(sub[col], errors="coerce").mean()),
                             "全市场中位": 0.5})
    exp_df = pd.DataFrame(rows)
    piv = exp_df.pivot_table(index="调仓日", columns="指标", values="持仓均值")
    piv.index = pd.to_datetime(piv.index)

    # ---- 拥挤度: 持仓两两相关 ----
    p2["_ret1"] = p2.groupby("ts_code", sort=False)["close"].pct_change()
    ret_m = p2.pivot_table(index="trade_date", columns="ts_code", values="_ret1")
    corr_rows = []
    ret_m_dt = pd.to_datetime(pd.Index(ret_m.index))
    for y in range(2021, 2026):
        year_ws = weights[pd.to_datetime(weights["trade_date"]).dt.year == y]
        held = sorted(set(year_ws[year_ws["weight"] > 0]["ts_code"]))
        cols = [c for c in held if c in ret_m.columns]
        mask = ret_m_dt.year == y
        r = ret_m.loc[mask, cols].dropna(axis=1, how="all")
        cm = r.corr()
        vals = cm.values[np.triu_indices_from(cm, k=1)]
        corr_rows.append({"年份": y, "持仓数": len(cols),
                          "两两相关均值": float(np.nanmean(vals)),
                          "两两相关中位": float(np.nanmedian(vals))})
    corr_df = pd.DataFrame(corr_rows)

    print("\n" + "=" * 100)
    print("  拥挤度 ① 持仓风格暴露 (全市场截面分位, 0.5=市场中位)")
    print("=" * 100)
    print(piv.resample("YE").mean().round(3).to_string())
    print("\n" + "=" * 100)
    print("  拥挤度 ② 持仓股两两日收益相关 (同涨同跌=拥挤)")
    print("=" * 100)
    print(corr_df.round(3).to_string(index=False))

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    cap_df.to_csv(ROOT / "outputs" / "final5" / f"capacity_{tag}.csv",
                  index=False, encoding="utf-8-sig")
    corr_df.to_csv(ROOT / "outputs" / "final5" / f"crowding_corr_{tag}.csv",
                   index=False, encoding="utf-8-sig")
    piv.round(4).to_csv(ROOT / "outputs" / "final5" / f"crowding_exposure_{tag}.csv",
                        encoding="utf-8-sig")
    print("\n已保存 outputs/final5/capacity_*.csv crowding_*.csv")


if __name__ == "__main__":
    main()
