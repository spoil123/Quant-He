# -*- coding: utf-8 -*-
"""低相关因子挖掘: 引入新收益源(小市值流动性/反转/盈利改善/低价格/低杠杆),
   约束: 与现有5因子及彼此的月收益相关性 < 0.5, 同时尽量保持夏普>=1.2。
   贪心选择: 按夏普降序, 逐个入选(与已选全部相关<0.5), 选满5个。
   最后给出入选5因子组合(等权日收益均值)的分散化效果。
用法: python scripts/scan_decor.py
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
from src.common.db import read_sql                         # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    PortfolioBacktester, monthly_rebalance_dates,
)

TRADING_DAYS = 252
CORR_MAX = 0.5

# 新收益源候选: (名称, 权重, topn, 加权, 上限, 备注)
NEW_VARIANTS = [
    # ---- 小市值+流动性 (与低波/红利大中盘暴露天然对冲) ----
    ("SCap-50",   {"size": 0.45, "illiq": 0.30, "tur": 0.25}, 50, "float_mv", 0.05, "小市值流动性"),
    ("SCap-30",   {"size": 0.45, "illiq": 0.30, "tur": 0.25}, 30, "float_mv", 0.06, "小市值集中"),
    ("SCap-Rev",  {"size": 0.35, "illiq": 0.25, "tur": 0.20, "rev20": 0.20}, 50, "float_mv", 0.05, "小市值+反转"),
    # ---- 短期反转 (与动量12-1负相关的收益源) ----
    ("Rev-50",    {"rev20": 0.50, "illiq": 0.25, "tur": 0.25}, 50, "float_mv", 0.05, "纯反转"),
    ("Rev-SCap",  {"rev20": 0.40, "tur": 0.30, "illiq": 0.15, "size": 0.15}, 50, "float_mv", 0.05, "反转+小市值"),
    # ---- 盈利改善 (ROE季度变化, 与水平值因子不同的信息) ----
    ("RoeChg-50", {"roechg": 0.40, "gm": 0.20, "ep": 0.20, "mom": 0.20}, 50, "float_mv", 0.05, "盈利改善+质量"),
    ("RoeChg-LV", {"roechg": 0.35, "roe": 0.20, "gm": 0.15, "lvol": 0.15, "mom": 0.15}, 50, "float_mv", 0.05, "盈利改善+低波"),
    # ---- 3个月动量 (与12-1动量不同持有期) ----
    ("Mom60-50",  {"mom60": 0.40, "lvol": 0.25, "roe": 0.20, "bm": 0.15}, 50, "float_mv", 0.05, "3月动量+低波质量"),
    # ---- 低价格异象 ----
    ("LP-50",     {"lp": 0.35, "illiq": 0.25, "tur": 0.20, "rev20": 0.20}, 50, "float_mv", 0.05, "低价+流动性"),
    # ---- 低杠杆质量 ----
    ("LevQ-50",   {"lev": 0.30, "roe": 0.25, "gm": 0.25, "lvol": 0.20}, 50, "float_mv", 0.05, "低杠杆质量"),
    # ---- 深度价值 (ep/bm/lev, 无低波无红利) ----
    ("DeepV-50",  {"ep": 0.40, "bm": 0.30, "lev": 0.30}, 50, "float_mv", 0.05, "深度价值"),
]


def add_derived_components(p: pd.DataFrame) -> pd.DataFrame:
    """在缓存面板上追加3个新成分: roechg / mom60 / lp"""
    eps_ = np.finfo(np.float32).eps
    raw_close = pd.to_numeric(p.get("raw_close", p["close"]), errors="coerce").replace(0, np.nan)
    # 盈利改善: roe_ttm 相对一季度前(~63交易日)的变化
    p = p.sort_values(["ts_code", "trade_date"])
    p["_roe_chg"] = p.groupby("ts_code", sort=False)["roe_ttm"].transform(
        lambda x: pd.to_numeric(x, errors="coerce") - pd.to_numeric(x, errors="coerce").shift(63))
    # 3个月动量
    p["_mom60"] = p["close"] / p.groupby("ts_code", sort=False)["close"].shift(60) - 1.0
    # 低价格: -log(价格), 越低分越高
    p["_lp"] = -np.log(np.maximum(raw_close, eps_))

    r = p.groupby("trade_date")
    p["_r_roechg"] = r["_roe_chg"].rank(pct=True)
    p["_r_mom60"] = r["_mom60"].rank(pct=True)
    p["_r_lp"] = r["_lp"].rank(pct=True)
    p["_lp_src"] = raw_close  # 留档
    return p


def monthly_from_daily(dr: pd.Series) -> pd.Series:
    dr = dr.dropna()
    dr.index = pd.to_datetime(dr.index)
    return (1 + dr).resample("ME").prod() - 1


def yearly_sharpe(daily: pd.Series) -> dict:
    eq = (1 + daily.fillna(0)).cumprod()
    out = {}
    for y in sorted({d.year for d in daily.index}):
        r = daily[daily.index.year == y].dropna()
        if len(r) < 10:
            continue
        e = eq[eq.index.year == y]
        e = e / e.iloc[0]
        out[y] = (r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan,
                  -(e / e.cummax() - 1).min())
    return out


def main() -> None:
    start, end = "2023-01-01", "2025-12-31"
    t0 = time.time()
    cache = ROOT / "outputs" / "final5" / "panel_cache.pkl"
    if cache.exists():
        logger.info("读取面板缓存...")
        p = pd.read_pickle(cache)
    else:
        logger.info("构建面板...")
        p = build_panel(start, end)
        cache.parent.mkdir(parents=True, exist_ok=True)
        p.to_pickle(cache)
    p = add_derived_components(p)
    logger.info(f"面板 {len(p):,} 行 (含新成分), 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=start, end=end)
    bench = load_benchmark(start, end)
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    all_comp = dict(COMP_COLS)
    all_comp.update({"roechg": "_r_roechg", "mom60": "_r_mom60", "lp": "_r_lp"})

    def run_factor(name, w, topn, wtype, mw):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in all_comp.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val
        weights = bt.build_weights(p, rb, score_col="_score", top_n=topn,
                                   weighting=wtype, max_weight=mw, min_weight=0.001)
        if weights.empty:
            return None
        res = bt.run(p, weights, start_date=start, benchmark=bench)
        m = res["metrics"]
        dr = res["daily_returns"].copy()
        dr.index = pd.to_datetime(dr.index)
        return {"name": name, "metrics": m, "daily": dr,
                "w": " ".join(f"{k}{v:.0%}" for k, v in sorted(wnorm.items(), key=lambda x: -x[1])),
                "struct": f"Top{topn}/{wtype}/上限{mw:.0%}"}

    # ---- 现有5因子 (对照) ----
    logger.info("回测现有5因子(对照)...")
    existing = []
    for f in FACTORS:
        r = run_factor(f["name"], f["w"], f["topn"], f["wtype"], f["mw"])
        existing.append(r)
        logger.info(f"  {f['name']}: 夏普={r['metrics']['夏普比率']:.3f}")
    ex_monthly = pd.DataFrame({r["name"]: monthly_from_daily(r["daily"]) for r in existing})

    # ---- 新收益源候选 ----
    logger.info(f"回测新收益源候选 {len(NEW_VARIANTS)} 个...")
    candidates = []
    for name, w, topn, wtype, mw, note in NEW_VARIANTS:
        r = run_factor(name, w, topn, wtype, mw)
        if r is None:
            logger.warning(f"  {name}: 权重空, 跳过")
            continue
        r["note"] = note
        candidates.append(r)
        m = r["metrics"]
        corrs = ex_monthly.apply(lambda col: col.corr(monthly_from_daily(r["daily"])))
        r["corr_max"] = float(corrs.max())
        r["corr_max_vs"] = str(corrs.idxmax())
        logger.info(f"  {name:10s} 夏普={m['夏普比率']:.3f} 回撤={m['最大回撤']*100:.1f}% "
                    f"年化={m['年化收益率']*100:.1f}% | 与现有最大相关 {r['corr_max']:.3f}({r['corr_max_vs']}) "
                    f"({time.time()-t0:.0f}s)")

    # ---- 汇总 ----
    rows = []
    for r in existing + candidates:
        m = r["metrics"]
        rows.append({
            "因子": r["name"], "类型": "现有" if r in existing else "新收益源",
            "说明": r.get("note", r.get("desc", "")), "结构": r["struct"], "权重": r["w"],
            "年化收益": m.get("年化收益率"), "夏普": m.get("夏普比率"),
            "最大回撤": m.get("最大回撤"), "年化波动": m.get("年化波动率"),
            "与现有最大相关": r.get("corr_max", np.nan),
        })
    df = pd.DataFrame(rows)

    # ---- 贪心选择: 相关<0.5, 选满5个 ----
    monthly_all = {}
    for r in existing + candidates:
        monthly_all[r["name"]] = monthly_from_daily(r["daily"])
    mon_df = pd.DataFrame(monthly_all)

    pool = [r for r in candidates if r.get("corr_max", 1) is not None]
    pool.sort(key=lambda r: -(r["metrics"]["夏普比率"] if r["metrics"]["夏普比率"] else -9))
    selected = []
    for r in pool:
        ok = True
        for s in selected:
            c = mon_df[r["name"]].corr(mon_df[s["name"]])
            if c >= CORR_MAX:
                ok = False
                break
        # 与保留的现有因子也要低相关
        for keep in ("QV-Mom40", "DV-LV50"):
            if mon_df[r["name"]].corr(mon_df[keep]) >= CORR_MAX:
                ok = False
                break
        if ok:
            selected.append(r)
        if len(selected) >= 3:
            break

    final_names = ["QV-Mom40", "DV-LV50"] + [r["name"] for r in selected]
    logger.info(f"最终低相关组合: {final_names}")

    # 相关性矩阵(最终组合)
    corr_final = mon_df[final_names].corr().round(3)

    # ---- 组合分散化效果: 最终5因子等权日均 ----
    def combine(names):
        d = pd.concat([monthly_all and None], axis=0) if False else None
        daily_df = pd.DataFrame({r["name"]: r["daily"] for r in existing + candidates})
        comb = daily_df[names].mean(axis=1).dropna()
        eq = (1 + comb).cumprod()
        years = len(comb) / TRADING_DAYS
        ann = eq.iloc[-1] ** (1 / years) - 1
        shp = comb.mean() / comb.std() * np.sqrt(TRADING_DAYS)
        mdd = -(eq / eq.cummax() - 1).min()
        vol = comb.std() * np.sqrt(TRADING_DAYS)
        b_ann = bench.iloc[-1] ** (252 / len(bench)) - 1
        return {"年化": ann, "夏普": shp, "回撤": mdd, "波动": vol, "基准年化": b_ann}

    comb_final = combine(final_names)
    comb_old = combine([f["name"] for f in FACTORS])

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "final5"
    df.to_csv(out / f"decor_scan_{tag}.csv", index=False, encoding="utf-8-sig")
    corr_final.to_csv(out / f"decor_corr_{tag}.csv", encoding="utf-8-sig")

    print("\n" + "=" * 120)
    print(f"  低相关因子挖掘  {start}~{end}  约束: 两两月收益相关 < {CORR_MAX}")
    print("=" * 120)
    show = df.copy()
    for c in ("年化收益", "最大回撤", "年化波动"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    print(show.drop(columns=["权重"]).round(3).to_string(index=False))

    print(f"\n最终低相关组合: {final_names}")
    print("\n最终组合月收益相关性:")
    print(corr_final.to_string())

    print("\n分散化效果对比:")
    for label, c in (("原5因子(高相关)", comb_old), ("新5因子(低相关)", comb_final)):
        print(f"  {label:14s} 年化={c['年化']*100:6.2f}%  夏普={c['夏普']:.3f}  "
              f"回撤={c['回撤']*100:6.2f}%  波动={c['波动']*100:6.2f}%")
    print("=" * 120)
    print(f"已保存: {out}\\decor_*_{tag}.csv")


if __name__ == "__main__":
    main()
