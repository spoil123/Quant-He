# -*- coding: utf-8 -*-
"""低相关挖掘 第2轮: 双周调仓救反转系, 小市值+质量/红利/低波混合救夏普,
   从"现有5+新候选"全池贪心选两两相关<0.5的最终组合(最多5个)。"""
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
from scan_decor import add_derived_components, monthly_from_daily   # noqa: E402
from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import PortfolioBacktester   # noqa: E402

TRADING_DAYS = 252
CORR_MAX = 0.5

# (名称, 权重, topn, 加权, 上限, 频率, 备注)  频率: M=月度 BW=双周(每10交易日)
VARIANTS = [
    # ---- 双周反转系 ----
    ("Rev-BW",      {"rev20": 0.50, "illiq": 0.25, "tur": 0.25}, 50, "float_mv", 0.05, "BW", "纯反转双周"),
    ("Rev-BW-100",  {"rev20": 0.50, "illiq": 0.25, "tur": 0.25}, 100, "float_mv", 0.04, "BW", "纯反转双周Top100"),
    ("RevSCap-BW",  {"rev20": 0.40, "tur": 0.25, "illiq": 0.20, "size": 0.15}, 50, "float_mv", 0.05, "BW", "反转+小市值双周"),
    ("SCap-BW",     {"size": 0.45, "illiq": 0.30, "tur": 0.25}, 50, "float_mv", 0.05, "BW", "小市值双周"),
    # ---- 小市值混合(月度) ----
    ("SCap-100",    {"size": 0.45, "illiq": 0.30, "tur": 0.25}, 100, "float_mv", 0.04, "M", "小市值Top100"),
    ("SCapQ-50",    {"size": 0.40, "illiq": 0.25, "ep": 0.20, "roe": 0.15}, 50, "float_mv", 0.05, "M", "小市值+盈利过滤"),
    ("SCapQ-30",    {"size": 0.40, "illiq": 0.25, "ep": 0.20, "roe": 0.15}, 30, "float_mv", 0.06, "M", "小市值+盈利集中"),
    ("SCapQ-100",   {"size": 0.40, "illiq": 0.25, "ep": 0.20, "roe": 0.15}, 100, "float_mv", 0.04, "M", "小市值+盈利Top100"),
    ("SCapM-50",    {"size": 0.35, "mom60": 0.30, "illiq": 0.20, "rev20": 0.15}, 50, "float_mv", 0.05, "M", "小市值+3月动量"),
    ("SCapDV-50",   {"size": 0.35, "dy": 0.25, "illiq": 0.20, "ep": 0.20}, 50, "float_mv", 0.05, "M", "小市值+红利"),
    ("SCapLV-50",   {"size": 0.35, "lvol": 0.30, "illiq": 0.20, "ep": 0.15}, 50, "float_mv", 0.05, "M", "小市值+低波"),
    # ---- 反转月度对照 ----
    ("Rev-BW-50Q",  {"rev20": 0.40, "illiq": 0.20, "tur": 0.20, "roe": 0.20}, 50, "float_mv", 0.05, "BW", "反转+质量双周"),
]


def biweekly_rebalance_dates(trade_dates, start, end, step=10):
    s = [d for d in sorted(trade_dates)
         if d >= pd.Timestamp(start).date() and d <= pd.Timestamp(end).date()]
    return [d for i, d in enumerate(s) if i % step == 0]


def main() -> None:
    start, end = "2023-01-01", "2025-12-31"
    t0 = time.time()
    cache = ROOT / "outputs" / "final5" / "panel_cache.pkl"
    p = pd.read_pickle(cache)
    p = add_derived_components(p)
    logger.info(f"面板 {len(p):,} 行, 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    rb_m = [d for d in trade_dates
            if d >= pd.Timestamp(start).date() and d <= pd.Timestamp(end).date()]
    rb_monthly = _monthly(rb_m, start, end)
    rb_biweekly = biweekly_rebalance_dates(trade_dates, start, end, step=10)
    bench = load_benchmark(start, end)
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    all_comp = dict(COMP_COLS)
    all_comp.update({"roechg": "_r_roechg", "mom60": "_r_mom60", "lp": "_r_lp"})

    def run_factor(name, w, topn, wtype, mw, rb_dates):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in all_comp.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val
        weights = bt.build_weights(p, rb_dates, score_col="_score", top_n=topn,
                                   weighting=wtype, max_weight=mw, min_weight=0.001)
        if weights.empty:
            return None
        res = bt.run(p, weights, start_date=start, benchmark=bench)
        m = res["metrics"]
        dr = res["daily_returns"].copy()
        dr.index = pd.to_datetime(dr.index)
        return {"name": name, "metrics": m, "daily": dr,
                "struct": f"Top{topn}/{wtype}/上限{mw:.0%}"}

    # 现有5因子(月度)
    pool = []
    for f in FACTORS:
        r = run_factor(f["name"], f["w"], f["topn"], f["wtype"], f["mw"], rb_monthly)
        r["note"] = "现有"
        pool.append(r)

    # 新候选
    for name, w, topn, wtype, mw, freq, note in VARIANTS:
        rb = rb_biweekly if freq == "BW" else rb_monthly
        r = run_factor(name, w, topn, wtype, mw, rb)
        if r is None:
            logger.warning(f"{name}: 权重空")
            continue
        r["note"] = f"{note}({freq})"
        pool.append(r)
        m = r["metrics"]
        logger.info(f"{name:12s} 夏普={m['夏普比率']:6.3f} 回撤={m['最大回撤']*100:5.1f}% "
                    f"年化={m['年化收益率']*100:5.1f}% 波动={m['年化波动率']*100:5.1f}% "
                    f"({time.time()-t0:.0f}s)")

    # 月收益相关矩阵
    mon = pd.DataFrame({r["name"]: monthly_from_daily(r["daily"]) for r in pool})
    names = [r["name"] for r in pool]
    corr = mon.corr()

    # 贪心: 夏普降序, 两两相关<0.5, 最多5个
    order = sorted(pool, key=lambda r: -(r["metrics"]["夏普比率"] or -9))
    selected = []
    for r in order:
        if all(corr.loc[r["name"], s["name"]] < CORR_MAX for s in selected):
            selected.append(r)
        if len(selected) >= 5:
            break
    sel_names = [r["name"] for r in selected]
    logger.info(f"贪心选中: {sel_names}")

    # 组合效果
    daily_df = pd.DataFrame({r["name"]: r["daily"] for r in pool})
    def combine(nms):
        comb = daily_df[nms].mean(axis=1).dropna()
        eq = (1 + comb).cumprod()
        years = len(comb) / TRADING_DAYS
        return {"年化": eq.iloc[-1] ** (1 / years) - 1,
                "夏普": comb.mean() / comb.std() * np.sqrt(TRADING_DAYS),
                "回撤": -(eq / eq.cummax() - 1).min(),
                "波动": comb.std() * np.sqrt(TRADING_DAYS)}
    comb_sel = combine(sel_names)
    comb_old = combine([f["name"] for f in FACTORS])

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "final5"
    rows = [{"因子": r["name"], "说明": r["note"], "结构": r["struct"],
             "年化收益": r["metrics"]["年化收益率"], "夏普": r["metrics"]["夏普比率"],
             "最大回撤": r["metrics"]["最大回撤"], "年化波动": r["metrics"]["年化波动率"]}
            for r in pool]
    pd.DataFrame(rows).to_csv(out / f"decor2_scan_{tag}.csv", index=False, encoding="utf-8-sig")
    corr.round(3).to_csv(out / f"decor2_corr_{tag}.csv", encoding="utf-8-sig")

    print("\n" + "=" * 120)
    print(f"  低相关挖掘第2轮(双周反转+小市值混合)  {start}~{end}  约束: 两两月收益相关<{CORR_MAX}")
    print("=" * 120)
    show = pd.DataFrame(rows).copy()
    for c in ("年化收益", "最大回撤", "年化波动"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 240)
    print(show.sort_values("夏普", ascending=False).round(3).to_string(index=False))
    print(f"\n贪心选中(两两相关<{CORR_MAX}): {sel_names}")
    print("\n选中组合相关性:")
    print(corr.loc[sel_names, sel_names].round(3).to_string())
    print("\n组合分散化效果:")
    for label, c in (("原5因子(高相关)", comb_old), ("低相关组合", comb_sel)):
        print(f"  {label:14s} 年化={c['年化']*100:6.2f}%  夏普={c['夏普']:.3f}  "
              f"回撤={c['回撤']*100:6.2f}%  波动={c['波动']*100:6.2f}%")
    print("=" * 120)


def _monthly(dates, start, end):
    s = pd.Series(pd.to_datetime(sorted(dates)))
    df = pd.DataFrame({"d": s})
    df["ym"] = df["d"].dt.strftime("%Y-%m")
    out = [d.date() for d in df.groupby("ym")["d"].max()]
    return [d for d in out
            if d >= pd.Timestamp(start).date() and d <= pd.Timestamp(end).date()]


if __name__ == "__main__":
    main()
