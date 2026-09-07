# -*- coding: utf-8 -*-
"""低相关挖掘 第3轮(收尾): 面板内剩余未开发信号源
   波动率收敛 / 低振幅 / 动量加速度 / 换手降温, 全部纯信号无借腿。
   与 QV2-Mom60(1.439) 和 SCap-100(0.812) 比相关性, 能 <0.5 且夏普高则入选。"""
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

from backtest_final5 import COMP_COLS, FACTORS, load_benchmark            # noqa: E402
from scan_decor import monthly_from_daily                                  # noqa: E402
from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import PortfolioBacktester   # noqa: E402

TRADING_DAYS = 252
CORR_MAX = 0.5

VARIANTS = [
    ("VolChg-50", {"volchg": 0.60, "tur": 0.20, "illiq": 0.20}, 50, "float_mv", 0.05, "波动率收敛"),
    ("Amp-50",    {"amp": 0.50, "tur": 0.25, "illiq": 0.25}, 50, "float_mv", 0.05, "低振幅"),
    ("MomAcc-50", {"momacc": 0.50, "roe": 0.25, "ep": 0.25}, 50, "float_mv", 0.05, "动量加速度+质量"),
    ("TurnCool-50", {"turncool": 0.50, "illiq": 0.25, "rev20": 0.25}, 50, "float_mv", 0.05, "换手降温+反转"),
]


def main() -> None:
    start, end = "2023-01-01", "2025-12-31"
    t0 = time.time()
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache.pkl")

    # ---- 新信号 ----
    eps_ = np.finfo(np.float32).eps
    p = p.sort_values(["ts_code", "trade_date"])
    g = p.groupby("ts_code", sort=False)
    ret = p["close"] / g["close"].shift(1) - 1.0
    p["_v20"] = ret.groupby(p["ts_code"], sort=False).transform(
        lambda x: x.rolling(20, min_periods=10).std())
    p["_v60"] = ret.groupby(p["ts_code"], sort=False).transform(
        lambda x: x.rolling(60, min_periods=30).std())
    p["_volchg"] = p["_v20"] - p["_v60"]          # <0 = 波动收敛
    if "high" in p.columns and "low" in p.columns:
        h = pd.to_numeric(p["high"], errors="coerce")
        l = pd.to_numeric(p["low"], errors="coerce")
        c = pd.to_numeric(p["close"], errors="coerce")
        p["_amp_raw"] = ((h - l) / c.replace(0, np.nan)).groupby(
            p["ts_code"], sort=False).transform(lambda x: x.rolling(20, min_periods=10).mean())
    else:
        p["_amp_raw"] = np.nan
    p["_momacc"] = (p["close"] / g["close"].shift(60) - 1.0) - \
                   (p["close"] / g["close"].shift(20) - 1.0)   # 60日动量-20日动量
    tur = pd.to_numeric(p["turnover_rate"], errors="coerce")
    tur20 = tur.groupby(p["ts_code"], sort=False).transform(
        lambda x: x.rolling(20, min_periods=10).mean())
    tur20_prev = tur20.groupby(p["ts_code"], sort=False).shift(20)
    p["_turncool"] = -(tur20 / tur20_prev.replace(0, np.nan))    # 换手降温, 越小(负得少? ) 越好→取负后rank

    r = p.groupby("trade_date")
    p["_r_volchg"] = 1.0 - r["_volchg"].rank(pct=True)   # 收敛(更小)分高
    p["_r_amp"] = 1.0 - r["_amp_raw"].rank(pct=True)     # 振幅低分高
    p["_r_momacc"] = r["_momacc"].rank(pct=True)
    p["_r_turncool"] = r["_turncool"].rank(pct=True)     # -tur20/tur20_prev 越大=降温越多
    logger.info(f"新信号完成, 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    s = pd.Series(pd.to_datetime(trade_dates))
    df_ym = pd.DataFrame({"d": s})
    df_ym["ym"] = df_ym["d"].dt.strftime("%Y-%m")
    rb = [d.date() for d in df_ym.groupby("ym")["d"].max()
          if d.date() >= pd.Timestamp(start).date() and d.date() <= pd.Timestamp(end).date()]
    bench = load_benchmark(start, end)
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    all_comp = dict(COMP_COLS)
    all_comp.update({"volchg": "_r_volchg", "amp": "_r_amp",
                     "momacc": "_r_momacc", "turncool": "_r_turncool"})

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
        return {"name": name, "metrics": m, "daily": dr}

    # 对照: 现有5 + 第2轮两个低相关赢家
    pool = []
    for f in FACTORS:
        r_ = run_factor(f["name"], f["w"], f["topn"], f["wtype"], f["mw"])
        r_["note"] = "现有"
        pool.append(r_)
    scap_w = {"size": 0.45, "illiq": 0.30, "tur": 0.25}
    r_ = run_factor("SCap-100", scap_w, 100, "float_mv", 0.04)
    r_["note"] = "小市值Top100"
    pool.append(r_)

    for name, w, topn, wtype, mw, note in VARIANTS:
        r_ = run_factor(name, w, topn, wtype, mw)
        if r_ is None:
            continue
        r_["note"] = note
        pool.append(r_)
        m = r_["metrics"]
        logger.info(f"{name:12s} 夏普={m['夏普比率']:6.3f} 回撤={m['最大回撤']*100:5.1f}% "
                    f"年化={m['年化收益率']*100:5.1f}% ({time.time()-t0:.0f}s)")

    mon = pd.DataFrame({r_["name"]: monthly_from_daily(r_["daily"]) for r_ in pool})
    corr = mon.corr()

    print("\n" + "=" * 110)
    print(f"  第3轮新信号源  {start}~{end}  (对照现有5因子 + SCap-100)")
    print("=" * 110)
    rows = []
    for r_ in pool:
        m = r_["metrics"]
        cmax_vs_anchor = max(abs(corr.loc[r_["name"], "QV2-Mom60"]),
                             abs(corr.loc[r_["name"], "SCap-100"])) \
            if r_["name"] not in ("QV2-Mom60", "SCap-100") else np.nan
        rows.append({"因子": r_["name"], "说明": r_["note"],
                     "年化": f"{m['年化收益率']*100:.2f}%", "夏普": round(m["夏普比率"], 3),
                     "回撤": f"{m['最大回撤']*100:.2f}%", "波动": f"{m['年化波动率']*100:.2f}%",
                     "|corr|vs(QV2,SCap)": round(cmax_vs_anchor, 3) if np.isfinite(cmax_vs_anchor) else "-"})
    pd.set_option("display.width", 200)
    print(pd.DataFrame(rows).sort_values("夏普", ascending=False).to_string(index=False))

    # 新信号与QV2/SCap的两两相关明细
    new_names = [v[0] for v in VARIANTS]
    print("\n新信号 vs 锚因子相关性:")
    print(corr.loc[new_names, ["QV2-Mom60", "SCap-100"]].round(3).to_string())

    # 若有新信号达标(夏普>=1.0 且 与QV2、SCap-100相关<0.5), 尝试扩充组合
    qualified = [n for n in new_names
                 if pool[[r_["name"] for r_ in pool].index(n)]["metrics"]["夏普比率"] >= 1.0
                 and corr.loc[n, "QV2-Mom60"] < CORR_MAX
                 and corr.loc[n, "SCap-100"] < CORR_MAX]
    sel = ["QV2-Mom60", "SCap-100"] + qualified
    print(f"\n达标扩充: {qualified or '无 → 面板内低相关+高夏普组合止步于 QV2-Mom60 + SCap-100'}")
    if len(sel) > 2:
        daily_df = pd.DataFrame({r_["name"]: r_["daily"] for r_ in pool})
        comb = daily_df[sel].mean(axis=1).dropna()
        eq = (1 + comb).cumprod()
        years = len(comb) / TRADING_DAYS
        print(f"  组合{sel}: 年化={((eq.iloc[-1] ** (1/years) - 1))*100:.2f}% "
              f"夏普={comb.mean()/comb.std()*np.sqrt(252):.3f} "
              f"回撤={-(eq/eq.cummax()-1).min()*100:.2f}%")
    print("=" * 110)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    corr.round(3).to_csv(ROOT / "outputs" / "final5" / f"decor3_corr_{tag}.csv", encoding="utf-8-sig")


if __name__ == "__main__":
    main()
