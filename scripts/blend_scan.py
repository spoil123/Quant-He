# -*- coding: utf-8 -*-
"""QV2-Mom60 x SCap-100 配比曲线: 找分散化后的最优风险收益点"""
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

from backtest_final5 import FACTORS, load_benchmark                        # noqa: E402
from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    PortfolioBacktester, monthly_rebalance_dates,
)

TRADING_DAYS = 252
SCAP_W = {"size": 0.45, "illiq": 0.30, "tur": 0.25}


def main() -> None:
    start, end = "2023-01-01", "2025-12-31"
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache.pkl")
    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=start, end=end)
    bench = load_benchmark(start, end)
    scfg = get_config("strategy")
    bt = PortfolioBacktester(
        initial_capital=float(scfg.get("backtest", {}).get("initial_cash", 10_000_000)),
        cost_tier="conservative")
    all_comp = {"illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm",
                "ep": "_r_ep", "dy": "_r_dy", "roe": "_r_roe", "gm": "_r_gm",
                "lev": "_r_lev", "lvol": "_r_lvol", "rev20": "_r_rev20", "mom": "_r_mom"}

    def run(w, topn, wtype, mw):
        wnorm = {k: v / sum(w.values()) for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in all_comp.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val
        weights = bt.build_weights(p, rb, score_col="_score", top_n=topn,
                                   weighting=wtype, max_weight=mw, min_weight=0.001)
        res = bt.run(p, weights, start_date=start, benchmark=bench)
        dr = res["daily_returns"].copy()
        dr.index = pd.to_datetime(dr.index)
        return dr

    logger.info("回测 QV2-Mom60 ...")
    dr_qv = run(FACTORS[3]["w"], 60, "float_mv", 0.04)
    logger.info("回测 SCap-100 ...")
    dr_sc = run(SCAP_W, 100, "float_mv", 0.04)

    rows = []
    for wq in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        comb = (wq * dr_qv + (1 - wq) * dr_sc).dropna()
        eq = (1 + comb).cumprod()
        years = len(comb) / TRADING_DAYS
        rows.append({
            "QV2占比": f"{wq:.0%}", "SCap占比": f"{1-wq:.0%}",
            "年化": eq.iloc[-1] ** (1 / years) - 1,
            "夏普": comb.mean() / comb.std() * np.sqrt(TRADING_DAYS),
            "回撤": -(eq / eq.cummax() - 1).min(),
            "波动": comb.std() * np.sqrt(TRADING_DAYS),
        })
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "outputs" / "final5" / "blend_curve.csv", index=False, encoding="utf-8-sig")
    print("\nQV2-Mom60 × SCap-100 配比曲线 (2023-2025, 成本后):")
    show = df.copy()
    for c in ("年化", "回撤", "波动"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    print(show.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
