# -*- coding: utf-8 -*-
"""
PandaAI Top5 因子 → 本地完整组合回测（净值/年化/夏普/回撤/超额/卡玛）。

流程（复用本地系统 PortfolioBacktester，与 run_top50_strategy 同链路）：
    面板(PIT对齐) -> 因子RANK合成分 -> 每月末选 Top50 -> 流通市值加权
    -> 次日开盘成交(全A股成本:佣金/过户/印花税/动态滑点,保守档)
    -> 向量化回测 -> 沪深300基准对照(alpha/beta/IR/超额/跟踪误差)

窗口默认与平台一致 2023-01-01 ~ 2025-12-31。
用法: python scripts/backtest_pandaai_top5.py [--start .. --end ..] [--topn 50]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config                 # noqa: E402
from src.common.db import read_sql                        # noqa: E402
from src.common.logger import logger                      # noqa: E402
from src.layer3_strategy.panel import attach_raw_close, build_factor_panel  # noqa: E402
from src.layer3_strategy.portfolio_backtest import (      # noqa: E402
    PortfolioBacktester, cost_tier_config, monthly_rebalance_dates,
)

FACTOR_DEFS = {
    "b6-mix4":        (10, {"illiq": 0.30, "tur": 0.30, "size": 0.20, "bm": 0.20}),
    "hp1-c1":         (1,  {"illiq": 0.30, "tur": 0.25, "size": 0.20, "bm": 0.25}),
    "mix4f-c3":       (3,  {"illiq": 0.25, "tur": 0.25, "size": 0.20, "bm": 0.30}),
    "hp17-c2":        (2,  {"illiq": 0.30, "tur": 0.25, "size": 0.20, "bm": 0.25}),
    "radarA-c3":      (3,  {"illiq": 0.25, "tur": 0.20, "size": 0.15, "bm": 0.40}),
}


def load_benchmark(start: str, end: str) -> pd.Series:
    df = read_sql(
        """SELECT trade_date, close FROM index_daily
           WHERE index_code = '000300' AND trade_date BETWEEN :s AND :e
           ORDER BY trade_date""",
        {"s": start, "e": end},
    )
    if df.empty:
        logger.warning("基准数据为空")
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df.set_index("trade_date")["close"].astype(float)
    return s / s.iloc[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="PandaAI Top5 单因子组合回测")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--topn", type=int, default=50)
    ap.add_argument("--codes", default="")
    ap.add_argument("--out", default="outputs/top5_bt")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    span = f"{args.start[:4]}-{args.end[:4]}"
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}
    sel_cfg = scfg.get("selection", {}) or {}

    # 面板：lookback 420 天（因子窗口无时序算子，仅给财务对齐余量，保守取 420）
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    logger.info(f"构建面板 {ps} ~ {args.end} ...")
    panel = build_factor_panel(ps, args.end, codes=codes,
                               with_financial=True, with_industry=True,
                               adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    # 估值因子要用不复权价配 BPS（同口径），见 attach_raw_close 文档
    panel = attach_raw_close(panel)
    logger.info(f"面板: {len(panel):,} 行, {panel['ts_code'].nunique()} 只")

    # ---- 构造成分（截面 RANK，0~1 正比，越大越优）----
    p = panel.copy()
    eps = np.finfo(np.float32).eps
    p["_logamt"] = np.log(np.maximum(p["amount"], eps))
    p["_logmv"] = np.log(np.maximum(p["float_mv"], eps))
    # BM 必须用不复权价：BPS 是披露当期原始值，未做复权，
    # 除以 qfq 价会让送转股的历史 BM 系统性放大（M4 修复）
    p["_bm"] = pd.to_numeric(p["bps"], errors="coerce") / pd.to_numeric(
        p.get("raw_close", p["close"]), errors="coerce").replace(0, np.nan)
    p["_r_illiq"] = 1.0 - p.groupby("trade_date")["_logamt"].rank(pct=True)
    p["_r_tur"] = 1.0 - p.groupby("trade_date")["turnover_rate"].rank(pct=True)
    p["_r_size"] = 1.0 - p.groupby("trade_date")["_logmv"].rank(pct=True)
    p["_r_bm"] = p.groupby("trade_date")["_bm"].rank(pct=True)
    comps = {"illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm"}

    # 只保留回测区间（lookback 段裁掉，选股与回测不需要）
    p = p[p["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    for fname, (cycle, w) in FACTOR_DEFS.items():
        val = pd.Series(0.0, index=p.index)
        for k, col in comps.items():
            val += w[k] * p[col]
        p[f"score_{fname}"] = val

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=args.start, end=args.end)
    bench = load_benchmark(args.start, args.end)
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative",
    )
    tier_info = cost_tier_config("conservative")
    logger.info(f"调仓日 {len(rb)} 个 | 成本档 {tier_info['label']} | Top{args.topn}")

    all_rows = []
    equities = {}
    for fname, (cycle, w) in FACTOR_DEFS.items():
        logger.info(f"===== 回测 {fname} (周期{cycle}日) =====")
        weights = bt.build_weights(
            p, rb,
            score_col=f"score_{fname}",
            top_n=args.topn,
            weighting=str(sel_cfg.get("weighting", "float_mv")),
            max_weight=float(sel_cfg.get("max_weight", 0.05)),
            min_weight=float(sel_cfg.get("min_weight", 0.002)),
        )
        if weights.empty:
            logger.warning(f"{fname}: 权重为空, 跳过")
            continue
        res = bt.run(p, weights, start_date=args.start, benchmark=bench)
        m = dict(res["metrics"])
        m["因子"] = fname
        m["调仓周期(日)"] = cycle
        all_rows.append(m)
        equities[fname] = res["equity_curve"]
        logger.info(f"  完成: 年化={m.get('年化收益率')} 夏普={m.get('夏普比率')} 回撤={m.get('最大回撤')}")

    # ---- 汇总 ----
    cols = ["因子", "调仓周期(日)", "总收益率", "年化收益率", "年化波动率",
            "夏普比率", "索提诺比率", "最大回撤", "卡玛比率",
            "基准年化收益率", "年化Alpha", "Beta", "超额年化收益率", "信息比率",
            "跟踪误差", "年化换手率", "平均持仓数", "成本档位"]
    cols = [c for c in cols if c in all_rows[0]] if all_rows else cols
    res_df = pd.DataFrame(all_rows)[cols]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print("\n" + "=" * 130)
    print(f"  PandaAI Top5 因子 · 本地完整组合回测  {args.start} ~ {args.end}  "
          f"Top{args.topn} 月度调仓 全成本(保守档)")
    print("=" * 130)
    print(res_df.to_string(index=False))
    print("=" * 130)

    res_df.to_csv(out_dir / f"pandaai_top5_bt_{span}_{tag}.csv",
                  index=False, encoding="utf-8-sig")
    print(f"已保存: {out_dir / f'pandaai_top5_bt_{span}_{tag}.csv'}")
    # 净值曲线 JSON（供报告前端）
    nav = {}
    for fname, eq in equities.items():
        nav[fname] = {"date": [d.strftime("%Y-%m-%d") for d in eq.index],
                      "nav": [float(v) for v in eq.values]}
    if bench is not None and len(bench):
        nav["沪深300"] = {"date": [d.strftime("%Y-%m-%d") for d in bench.index],
                          "nav": [float(v) for v in bench.values]}
    import json
    (out_dir / f"pandaai_top5_nav_{span}_{tag}.json").write_text(
        json.dumps(nav, ensure_ascii=False), encoding="utf-8")
    print(f"净值已保存: {out_dir / f'pandaai_top5_nav_{span}_{tag}.json'}")


if __name__ == "__main__":
    main()
