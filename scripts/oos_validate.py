# -*- coding: utf-8 -*-
"""样本外验证 (walk-forward + 参数稳健性):
A. Walk-forward 动态选择: 每年用过去1年各因子夏普挑Top3等权, 检验下一年 —— 对比固定Combo3
B. 参数邻域稳健性: Combo3 在 TopN×上限 网格上的夏普分布 (是否刀锋参数)
C. 保存2021-2025各因子日收益 → outputs/final5/daily_returns_2125.csv 供归因复用
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

from backtest_final5 import COMP_COLS, FACTORS, build_panel, load_benchmark   # noqa: E402
from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import PortfolioBacktester   # noqa: E402

TRADING_DAYS = 252
TRIO = ["MD-Mom50", "DV-LV50", "GM-LV50"]


def main() -> None:
    start, end = "2021-01-01", "2025-12-31"
    t0 = time.time()
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache_2125.pkl")
    trade_dates = sorted(p["trade_date"].unique())
    s = pd.Series(pd.to_datetime(trade_dates))
    ym = pd.DataFrame({"d": s})
    ym["ym"] = ym["d"].dt.strftime("%Y-%m")
    rb = [d.date() for d in ym.groupby("ym")["d"].max()]
    bench = load_benchmark(start, end)
    scfg = get_config("strategy")
    bt = PortfolioBacktester(
        initial_capital=float(scfg.get("backtest", {}).get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    def score_of(w):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        return val

    def backtest(val, topn=50, wtype="float_mv", mw=0.05):
        p["_score"] = val
        weights = bt.build_weights(p, rb, score_col="_score", top_n=topn,
                                   weighting=wtype, max_weight=mw, min_weight=0.001)
        res = bt.run(p, weights, start_date=start, benchmark=bench)
        dr = res["daily_returns"].copy()
        dr.index = pd.to_datetime(dr.index)
        return dr, res["metrics"]

    fmap = {f["name"]: f for f in FACTORS}

    # ---- C: 5因子 + Combo3 日收益 (2021-2025) ----
    logger.info("回测5单因子+Combo3...")
    daily = {}
    for n in TRIO + ["QV-Mom40", "QV2-Mom60"]:
        f = fmap[n]
        dr, m = backtest(score_of(f["w"]), f["topn"], f["wtype"], f["mw"])
        daily[n] = dr
        logger.info(f"  {n}: 夏普={m['夏普比率']:.3f}")
    comp_score = sum(score_of(fmap[n]["w"]) for n in TRIO) / 3
    dr_c3, m_c3 = backtest(comp_score, 50, "float_mv", 0.05)
    daily["Combo3"] = dr_c3
    logger.info(f"  Combo3: 夏普={m_c3['夏普比率']:.3f} ({time.time()-t0:.0f}s)")

    daily_df = pd.DataFrame(daily)
    daily_df.to_csv(ROOT / "outputs" / "final5" / "daily_returns_2125.csv",
                    encoding="utf-8-sig")

    # ---- A: Walk-forward 动态选择 (训练2021 → 测试2022, 依此滚动) ----
    logger.info("Walk-forward 动态选择...")
    years = [2022, 2023, 2024, 2025]
    wf_rows = []
    wf_daily = []
    pool = TRIO + ["QV-Mom40", "QV2-Mom60"]
    for y in years:
        train = daily_df[daily_df.index.year == y - 1]
        shps = {n: train[n].mean() / train[n].std() * np.sqrt(TRADING_DAYS)
                if train[n].std() > 0 else np.nan for n in pool}
        top3 = sorted(pool, key=lambda n: -(shps[n] or -9))[:3]
        test = daily_df[daily_df.index.year == y]
        blend = test[top3].mean(axis=1)
        eq = (1 + blend).cumprod()
        shp = blend.mean() / blend.std() * np.sqrt(TRADING_DAYS)
        mdd = -(eq / eq.cummax() - 1).min()
        ann = eq.iloc[-1] ** (TRADING_DAYS / len(blend)) - 1
        c3 = test["Combo3"]
        eq3 = (1 + c3).cumprod()
        wf_rows.append({"测试年": y,
                        "选中Top3(按上年夏普)": ",".join(top3),
                        "WF年化": ann, "WF夏普": shp, "WF回撤": mdd,
                        "固定Combo3夏普": c3.mean() / c3.std() * np.sqrt(TRADING_DAYS),
                        "固定Combo3回撤": -(eq3 / eq3.cummax() - 1).min()})
        wf_daily.append(blend.rename(y))
    wf_df = pd.DataFrame(wf_rows)
    wf_all = pd.concat(wf_daily)
    eq_all = (1 + wf_all).cumprod()
    years_total = len(wf_all) / TRADING_DAYS
    wf_summary = {"WF(2022-2025)年化": eq_all.iloc[-1] ** (1 / years_total) - 1,
                  "WF夏普": wf_all.mean() / wf_all.std() * np.sqrt(TRADING_DAYS),
                  "WF回撤": -(eq_all / eq_all.cummax() - 1).min()}

    # ---- B: 参数邻域稳健性 ----
    logger.info("参数网格稳健性...")
    grid = []
    for topn in (30, 40, 50, 60, 70):
        for mw in (0.04, 0.05, 0.06):
            dr, m = backtest(comp_score, topn, "float_mv", mw)
            grid.append({"TopN": topn, "上限": f"{mw:.0%}",
                         "年化": m["年化收益率"], "夏普": m["夏普比率"],
                         "回撤": m["最大回撤"]})
    grid_df = pd.DataFrame(grid)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    wf_df.to_csv(ROOT / "outputs" / "final5" / f"oos_walkforward_{tag}.csv",
                 index=False, encoding="utf-8-sig")
    grid_df.to_csv(ROOT / "outputs" / "final5" / f"oos_grid_{tag}.csv",
                   index=False, encoding="utf-8-sig")

    print("\n" + "=" * 110)
    print("  A. Walk-forward 动态选择 (每年按上年夏普挑Top3等权) vs 固定Combo3")
    print("=" * 110)
    pd.set_option("display.width", 200)
    print(wf_df.round(3).to_string(index=False))
    print(f"\n  WF汇总(2022-2025): 年化={wf_summary['WF(2022-2025)年化']*100:.2f}%  "
          f"夏普={wf_summary['WF夏普']:.3f}  回撤={wf_summary['WF回撤']*100:.2f}%")
    print("\n" + "=" * 110)
    print("  B. Combo3 参数邻域 (TopN × 单票上限, 15组)")
    print("=" * 110)
    show = grid_df.copy()
    for c in ("年化", "回撤"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    print(show.round(3).to_string(index=False))
    print(f"\n  夏普: mean={grid_df['夏普'].mean():.3f}  min={grid_df['夏普'].min():.3f}  "
          f"max={grid_df['夏普'].max():.3f}  std={grid_df['夏普'].std():.3f}")
    print(f"  回撤: max={grid_df['回撤'].max()*100:.2f}%")
    print("=" * 110)


if __name__ == "__main__":
    main()
