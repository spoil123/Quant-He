# -*- coding: utf-8 -*-
"""三因子合成组合回测 2021-2025: MD-Mom50 + DV-LV50 + GM-LV50
加权方法: 机构最常用的 等权合成 (三因子截面RANK得分等权相加成复合因子, Top50/流通市值加权)
对照: 组合层面等权(三策略日收益均值) + 三单因子
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
START, END = "2021-01-01", "2025-12-31"
TRIO = ["MD-Mom50", "DV-LV50", "GM-LV50"]


def yearly_stats(daily: pd.Series) -> dict:
    eq = (1 + daily.fillna(0)).cumprod()
    out = {}
    for y in sorted({d.year for d in daily.index}):
        r = daily[daily.index.year == y].dropna()
        if len(r) < 10:
            continue
        e = eq[eq.index.year == y]
        e = e / e.iloc[0]
        out[y] = (r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan,
                  -(e / e.cummax() - 1).min(), e.iloc[-1] - 1)
    return out


def main() -> None:
    t0 = time.time()
    cache = ROOT / "outputs" / "final5" / "panel_cache_2125.pkl"
    if cache.exists():
        logger.info("读取2021-2025面板缓存...")
        p = pd.read_pickle(cache)
    else:
        logger.info("构建2021-2025面板(约4-6分钟)...")
        p = build_panel(START, END)
        cache.parent.mkdir(parents=True, exist_ok=True)
        p.to_pickle(cache)
    logger.info(f"面板 {len(p):,} 行, 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    rb = [d for d in trade_dates
          if d >= pd.Timestamp(START).date() and d <= pd.Timestamp(END).date()]
    s = pd.Series(pd.to_datetime(rb))
    ym = pd.DataFrame({"d": s})
    ym["ym"] = ym["d"].dt.strftime("%Y-%m")
    rb = [d.date() for d in ym.groupby("ym")["d"].max()]
    bench = load_benchmark(START, END)
    scfg = get_config("strategy")
    bt = PortfolioBacktester(
        initial_capital=float(scfg.get("backtest", {}).get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    fmap = {f["name"]: f for f in FACTORS}

    def score_of(w):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        return val

    def backtest(val, topn=50, wtype="float_mv", mw=0.05, label=""):
        p["_score"] = val
        weights = bt.build_weights(p, rb, score_col="_score", top_n=topn,
                                   weighting=wtype, max_weight=mw, min_weight=0.001)
        res = bt.run(p, weights, start_date=START, benchmark=bench)
        dr = res["daily_returns"].copy()
        dr.index = pd.to_datetime(dr.index)
        logger.info(f"{label}: 夏普={res['metrics']['夏普比率']:.3f} "
                    f"回撤={res['metrics']['最大回撤']*100:.2f}% ({time.time()-t0:.0f}s)")
        return {"metrics": res["metrics"], "daily": dr}

    # 三单因子 (对照)
    singles = {}
    for name in TRIO:
        f = fmap[name]
        singles[name] = backtest(score_of(f["w"]), f["topn"], f["wtype"], f["mw"], name)

    # 主方案: 等权合成复合因子 (三因子得分等权平均, Top50/流通市值/上限5%)
    comp_score = sum(score_of(fmap[n]["w"]) for n in TRIO) / len(TRIO)
    combo = backtest(comp_score, 50, "float_mv", 0.05, "等权合成Combo3")

    # 对照: 组合层面等权 (三策略日收益均值, 各自结构不变)
    daily_df = pd.DataFrame({n: singles[n]["daily"] for n in TRIO})
    blend = daily_df.mean(axis=1).dropna()

    # 汇总输出
    def row_of(m):
        return {"年化": m["年化收益率"], "夏普": m["夏普比率"], "索提诺": m["索提诺比率"],
                "波动": m["年化波动率"], "回撤": m["最大回撤"],
                "超额年化": m.get("超额年化收益率"), "IR": m.get("信息比率"),
                "换手": m["年化换手率"]}
    rows = []
    for n in TRIO:
        rows.append({"方案": n, **row_of(singles[n]["metrics"])})
    rows.append({"方案": "等权合成Combo3(主)", **row_of(combo["metrics"])})
    eq = (1 + blend).cumprod()
    years = len(blend) / TRADING_DAYS
    b_ann = bench.iloc[-1] ** (252 / len(bench)) - 1
    rows.append({"方案": "组合层面等权(对照)", "年化": eq.iloc[-1] ** (1 / years) - 1,
                 "夏普": blend.mean() / blend.std() * np.sqrt(TRADING_DAYS),
                 "索提诺": np.nan, "波动": blend.std() * np.sqrt(TRADING_DAYS),
                 "回撤": -(eq / eq.cummax() - 1).min(),
                 "超额年化": eq.iloc[-1] ** (1 / years) - 1 - b_ann, "IR": np.nan,
                 "换手": np.nan})
    df = pd.DataFrame(rows)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "final5"
    df.to_csv(out / f"combo3_{tag}.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 120)
    print(f"  三因子合成组合回测  {START} ~ {END}  (全A剔ST·月度调仓·真实成本·基准沪深300 年化{b_ann*100:.2f}%)")
    print("=" * 120)
    show = df.copy()
    for c in ("年化", "波动", "回撤", "超额年化", "换手"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 240)
    print(show.round(3).to_string(index=False))

    print("\n等权合成Combo3 分年 (夏普/回撤/收益):")
    for y, (shp, mdd, tot) in yearly_stats(combo["daily"]).items():
        print(f"  {y}: 夏普={shp:6.3f}  回撤={mdd*100:6.2f}%  收益={tot*100:7.2f}%")
    print("\n组合层面等权 分年:")
    for y, (shp, mdd, tot) in yearly_stats(blend).items():
        print(f"  {y}: 夏普={shp:6.3f}  回撤={mdd*100:6.2f}%  收益={tot*100:7.2f}%")
    print("\n三因子月收益相关性:")
    mon = pd.DataFrame({n: (1 + singles[n]["daily"].dropna()).resample("ME").prod() - 1
                        for n in TRIO})
    print(mon.corr().round(3).to_string())
    print("=" * 120)
    print(f"已保存: {out}\\combo3_{tag}.csv")


if __name__ == "__main__":
    main()
