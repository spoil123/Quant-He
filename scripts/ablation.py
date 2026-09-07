# -*- coding: utf-8 -*-
"""
归因/消融实验 v2：以【当前生产配置】为基线，逐项关闭测贡献。

2026-08-31 v1 的教训：基线用的是旧配置（等权/动量+1/固定2bp），
对照组和目标组不是同一个东西，测出的"+28pp 归因于权重"没有意义。
v2 反过来：基线 = 生产配置（custom 权重 + 动态滑点 + 组合熔断），
每组关闭一项，看指标怎么退化——退化多少就是该项的真实贡献。

用法：
    python scripts/ablation.py [--start 2019-01-01 --end 2023-12-31]
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.common.config import get_config
from src.common.logger import logger
from src.layer3_strategy.factors.composite import (
    composite_score,
    compute_all_factors,
    preprocess_all_factors,
)
from src.layer3_strategy.panel import build_factor_panel
from src.layer3_strategy.portfolio_backtest import (
    PortfolioBacktester,
    monthly_rebalance_dates,
)
from src.layer4_risk.risk_manager import RiskManager

BASE_FACTORS = get_config("factors")
BASE_COSTS = get_config("costs")


def _dyn_kwargs() -> dict:
    sl = BASE_COSTS["slippage"]
    return {"slip_model": "liquidity",
            "impact_coef": float(sl.get("impact_coef", 1.0)),
            "slip_bps": float(sl.get("base_bps", 2.0)),
            "participation_cap": float(sl.get("participation_cap", 0.10)),
            "slip_max_bps": float(sl.get("max_bps", 60.0)),
            "slip_min_bps": float(sl.get("min_bps", 1.0))}


def main() -> None:
    ap = argparse.ArgumentParser(description="策略改动归因实验（生产基线版）")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2023-12-31")
    args = ap.parse_args()

    scfg = get_config("strategy")
    sel = scfg["selection"]

    # ---------------- 面板（一次） ----------------
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    panel = build_factor_panel(ps, args.end, with_financial=True,
                               with_industry=True, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    logger.info(f"面板就绪: {len(panel):,} 行")

    rb = monthly_rebalance_dates(sorted(panel["trade_date"].unique()),
                                 start=args.start, end=args.end)

    # 因子计算一次（生产配置），变体只动合成/成本/风控
    p, names = compute_all_factors(panel)
    p = p[p["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    p, score_cols = preprocess_all_factors(p, names)
    p["composite_score"] = composite_score(p, score_cols)

    cap = float(scfg["backtest"]["initial_cash"])
    dyn = _dyn_kwargs()

    def build_and_run(bt_kwargs: dict):
        bt = PortfolioBacktester(initial_capital=cap, **bt_kwargs)
        w = bt.build_weights(p, rb, top_n=int(sel["top_n"]),
                             weighting=sel["weighting"],
                             max_weight=float(sel["max_weight"]),
                             min_weight=float(sel["min_weight"]))
        return bt, bt.run(p, w, start_date=args.start), w

    # ---------------- S0 生产基线（= run_top50 默认：无风控） ----------------
    logger.info("S0 生产基线（custom + 动态滑点，无风控，与 run_top50 默认一致）")
    bt1, res1, w1 = build_and_run(dict(dyn))
    eq1 = res1["equity_curve"]

    # ---------------- S1 加熔断（--risk 行为） ----------------
    logger.info("S1 加熔断（--risk 行为）")
    rm = RiskManager()
    w0, ev0 = rm.apply_circuit_breaker(w1, p, base_equity=eq1)
    bt0 = PortfolioBacktester(initial_capital=cap, **dyn)
    res0 = bt0.run(p, w0, start_date=args.start)

    # ---------------- S2 等权替换 custom（其余同生产） ----------------
    fc_eq = deepcopy(BASE_FACTORS)
    fc_eq["composite"]["method"] = "equal_weight"
    p_eq, names_eq = compute_all_factors(panel)
    p_eq = p_eq[p_eq["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    p_eq, sc_eq = preprocess_all_factors(p_eq, names_eq, factor_cfg=fc_eq)
    p_eq["composite_score"] = composite_score(p_eq, sc_eq, factor_cfg=fc_eq)
    bt2 = PortfolioBacktester(initial_capital=cap, **dyn)
    w2 = bt2.build_weights(p_eq, rb, top_n=int(sel["top_n"]),
                           weighting=sel["weighting"],
                           max_weight=float(sel["max_weight"]),
                           min_weight=float(sel["min_weight"]))
    res2 = bt2.run(p_eq, w2, start_date=args.start)

    # ---------------- S3 固定 2bp 滑点（其余同生产，无风控） ----------------
    bt3, res3, w3 = build_and_run({"slip_model": "fixed", "slip_bps": 2.0})

    # ---------------- S4/S5 滑点敏感性 ±（无风控） ----------------
    kw_hi = dict(dyn); kw_hi["impact_coef"] = dyn["impact_coef"] * 2
    bt4 = PortfolioBacktester(initial_capital=cap, **kw_hi)
    res4 = bt4.run(p, w1, start_date=args.start)
    kw_lo = dict(dyn); kw_lo["impact_coef"] = dyn["impact_coef"] * 0.5
    bt5 = PortfolioBacktester(initial_capital=cap, **kw_lo)
    res5 = bt5.run(p, w1, start_date=args.start)

    rows = []
    def _add(name, res):
        m = res["metrics"]
        calmar = (m["年化收益率"] / m["最大回撤"]) if m.get("最大回撤") else 0
        rows.append({"组": name, "总收益": round(m["总收益率"], 4),
                     "年化": round(m["年化收益率"], 4), "夏普": round(m["夏普比率"], 4),
                     "回撤": round(m["最大回撤"], 4), "卡玛": round(calmar, 4)})
        print(f"  {name}: 收益 {m['总收益率']:.2%}  夏普 {m['夏普比率']:.3f}  "
              f"回撤 {m['最大回撤']:.2%}")

    print("\n---- 归因（基线=生产配置[无风控]，逐项变体） ----")
    _add("S0 生产基线(custom+动态滑点,无风控)", res1)
    _add("S1 加熔断(--risk行为)", res0)
    _add("S2 等权替换custom(无风控)", res2)
    _add("S3 固定2bp滑点(无风控)", res3)
    _add("S4 滑点×2(敏感性)", res4)
    _add("S5 滑点×0.5(敏感性)", res5)

    df = pd.DataFrame(rows)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "top50"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f"ablation_{tag}.csv", index=False, encoding="utf-8-sig")
    print(f"\n归因结果: {out}/ablation_{tag}.csv")


if __name__ == "__main__":
    main()
