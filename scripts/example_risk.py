# -*- coding: utf-8 -*-
"""
第 4 层演示：风控闸门前后对比。

流程：
    因子面板 → 五因子合成 → 月末 Top50 → 流通市值加权
    → 无风控回测（拿基准净值）
    → RiskManager 闸门（仓位约束 + 个股止损 + 组合熔断）
    → 风控后回测
    → 对比报告：收益/回撤/VaR/CVaR/压力测试 + 回撤止损模拟曲线

用法：
    python scripts/example_risk.py [--start 2023-01-01 --end 2024-12-31]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config
from src.common.logger import logger
from src.layer3_strategy.panel import build_factor_panel
from src.layer3_strategy.portfolio_backtest import (
    PortfolioBacktester,
    monthly_rebalance_dates,
)
from src.layer4_risk.risk_manager import RiskManager
from src.layer4_risk.risk_metrics import RiskMetrics

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


def main() -> None:
    ap = argparse.ArgumentParser(description="第 4 层风控演示")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2023-12-31")
    ap.add_argument("--codes", default="", help="逗号分隔代码子集（留空=全库）")
    ap.add_argument("--out", default="outputs/risk")
    args = ap.parse_args()

    scfg = get_config("strategy")
    sel = scfg["selection"]
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")

    # ---------------- 面板 + 因子 ----------------
    LOOKBACK = 400
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=LOOKBACK)).strftime("%Y-%m-%d")
    logger.info(f"构建面板 {ps} ~ {args.end}（lookback，回测从 {args.start} 起）")
    # with_financial=True：与 run_top50_strategy 一致（五因子完整版）。
    # 修复：此前为 False，导致风控报告的"无风控基准"是三因子结果（46.96%），
    # 与主策略五因子（37.68%）不一致，风控对比失真。
    panel = build_factor_panel(ps, args.end, codes=codes, with_financial=True,
                               with_industry=True, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    # 三步拆分（compute → 裁掉 lookback → preprocess），控制内存峰值
    from src.layer3_strategy.factors.composite import (
        composite_score,
        compute_all_factors,
        preprocess_all_factors,
    )

    panel, names = compute_all_factors(panel)
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    panel, score_cols = preprocess_all_factors(panel, names)
    panel["composite_score"] = composite_score(panel, score_cols)

    # ---------------- 权重 ----------------
    trade_dates = sorted(panel["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=args.start, end=args.end)
    bt = PortfolioBacktester(initial_capital=float(scfg["backtest"]["initial_cash"]))
    weights = bt.build_weights(
        panel, rb, top_n=int(sel["top_n"]), weighting=sel["weighting"],
        max_weight=float(sel["max_weight"]), min_weight=float(sel["min_weight"]),
    )
    logger.info(f"权重: {len(weights)} 条，调仓日 {weights['trade_date'].nunique()} 个")

    # ---------------- 无风控回测 ----------------
    res_plain = bt.run(panel, weights, start_date=args.start)
    eq_plain = res_plain["equity_curve"]

    # ---------------- 风控闸门 ----------------
    rm = RiskManager()
    w_risk, events = rm.run_risk_controls(weights, panel, base_equity=eq_plain)

    # ---------------- 风控后回测 ----------------
    res_risk = bt.run(panel, w_risk, start_date=args.start)
    eq_risk = res_risk["equity_curve"]

    # ---------------- 风险度量 + 报告 ----------------
    m_plain = RiskMetrics(eq_plain)
    m_risk = RiskMetrics(eq_risk)

    print("\n" + "=" * 72)
    print(f"  风控前后对比    {args.start} ~ {args.end}")
    print("=" * 72)
    keys = ["总收益率", "年化收益率", "年化波动率", "夏普比率", "最大回撤", "卡玛比率",
            "var", "cvar"]
    rows = []
    for k in keys:
        a = m_plain.summary().get(k)
        b = m_risk.summary().get(k)
        rows.append({"指标": k, "无风控": round(a, 4) if a is not None else None,
                     "有风控": round(b, 4) if b is not None else None})
    df_cmp = pd.DataFrame(rows)
    print(df_cmp.to_string(index=False))

    if events:
        print(f"\n风控事件 {len(events)} 条（前 10 条）:")
        for e in events[:10]:
            print(f"  {e.get('date','')} {e.get('type','')} {e.get('ts_code','')} "
                  f"{e.get('reason','')} dd={e.get('dd','')}")

    # 压力测试（风控后）
    print("\n风控后压力测试:")
    stress = m_risk.stress_test()
    for k, v in stress.items():
        print(f"  {k}: {v}")

    # ---------------- 图：三线对比 ----------------
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=130)
    ax.plot(eq_plain.index, eq_plain.values, label="无风控", linewidth=1.4, color="#c0392b")
    ax.plot(eq_risk.index, eq_risk.values, label="有风控（约束+止损+熔断）", linewidth=1.4, color="#2980b9")
    stop_curve = m_plain.drawdown_stop_curve()
    ax.plot(stop_curve.index, stop_curve.values, label="仅回撤熔断模拟", linewidth=1.1,
            color="#7f8c8d", linestyle="--", alpha=0.9)
    ax.set_title(f"风控前后净值对比  {args.start}~{args.end}", fontsize=13, pad=12)
    ax.set_ylabel("净值")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")
    fig.tight_layout()
    chart = out_dir / f"risk_compare_{tag}.png"
    fig.savefig(chart)
    plt.close(fig)

    df_cmp.to_csv(out_dir / f"risk_report_{tag}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(events).to_csv(out_dir / f"risk_events_{tag}.csv", index=False, encoding="utf-8-sig")
    # 前端数据：两条净值曲线（无风控 / 有风控）
    eq_plain.rename("equity").to_frame().to_csv(
        out_dir / f"equity_plain_{tag}.csv", encoding="utf-8-sig")
    eq_risk.rename("equity").to_frame().to_csv(
        out_dir / f"equity_risk_{tag}.csv", encoding="utf-8-sig")
    print(f"\n产物: {out_dir}/risk_compare_{tag}.png / risk_report_{tag}.csv / "
          f"risk_events_{tag}.csv / equity_plain_{tag}.csv / equity_risk_{tag}.csv")


if __name__ == "__main__":
    main()
