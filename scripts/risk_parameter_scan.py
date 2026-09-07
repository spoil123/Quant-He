# -*- coding: utf-8 -*-
"""
风控参数网格优化。

复用同一次面板+权重，对多组风控参数跑 run_risk_controls → 回测，
按「风险调整后收益」选最优组，写回 risk.yaml。

用法：
    python scripts/risk_parameter_scan.py [--start 2019-01-01 --end 2023-12-31]
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

# 参数网格：止损 / 移动止损 / 熔断回撤（降仓固定 0.5，冷却 20 天）
# 2026-08-31：新增「机制拆分」组——个股止损与组合熔断本质不同：
#   止损=震荡洗盘割肉（Top50 单票仅 5%、A 股个股年波动 40%+，容易被洗出）
#   熔断=组合级回撤控制（理论上降回撤且代价小）
# 必须分开评估，不能只做"全开/全关"的粗粒度对比。
GRID = [
    {"name": "A_当前(-25/-30/-12)", "sl": -0.25, "trail": -0.30, "cb": -0.12,
     "sl_on": True, "cb_on": True},
    {"name": "B_放宽(-30/-40/-15)", "sl": -0.30, "trail": -0.40, "cb": -0.15,
     "sl_on": True, "cb_on": True},
    {"name": "C_更宽(-35/-45/-20)", "sl": -0.35, "trail": -0.45, "cb": -0.20,
     "sl_on": True, "cb_on": True},
    {"name": "D_熔断宽(-25/-30/-20)", "sl": -0.25, "trail": -0.30, "cb": -0.20,
     "sl_on": True, "cb_on": True},
    {"name": "E_止损宽(-30/-40/-12)", "sl": -0.30, "trail": -0.40, "cb": -0.12,
     "sl_on": True, "cb_on": True},
    {"name": "F_仅熔断(关止损)", "sl": -0.30, "trail": -0.40, "cb": -0.12,
     "sl_on": False, "cb_on": True},
    {"name": "G_仅熔断严(-15)", "sl": -0.30, "trail": -0.40, "cb": -0.15,
     "sl_on": False, "cb_on": True},
    {"name": "H_仅止损(关熔断)", "sl": -0.30, "trail": -0.40, "cb": -0.12,
     "sl_on": True, "cb_on": False},
    {"name": "I_全关(裸奔对照)", "sl": -0.30, "trail": -0.40, "cb": -0.12,
     "sl_on": False, "cb_on": False},
]


def main() -> None:
    ap = argparse.ArgumentParser(description="风控参数网格优化")
    # 2026-08-31 方法论修正：调参必须在【样本内】2015-2018 做，
    # 样本外 2019-2023 只用于最终验证一次。此前默认在样本外调参，
    # 等于拿验证集当训练集，调出的参数和样本外收益全部失去可信度。
    ap.add_argument("--start", default="2015-01-01",
                    help="调参区间（默认样本内 2015-2018，勿改成样本外）")
    ap.add_argument("--end", default="2018-12-31")
    ap.add_argument("--apply", action="store_true", help="把最优组写回 risk.yaml")
    args = ap.parse_args()

    scfg = get_config("strategy")
    sel = scfg["selection"]

    # ---------------- 面板 + 权重（复用一次） ----------------
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    panel = build_factor_panel(ps, args.end, with_financial=True,
                               with_industry=True, adj_type="qfq")
    panel, names = compute_all_factors(panel)
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    panel, score_cols = preprocess_all_factors(panel, names)
    panel["composite_score"] = composite_score(panel, score_cols)

    rb = monthly_rebalance_dates(sorted(panel["trade_date"].unique()),
                                 start=args.start, end=args.end)
    bt = PortfolioBacktester(initial_capital=float(scfg["backtest"]["initial_cash"]))
    weights = bt.build_weights(
        panel, rb, top_n=int(sel["top_n"]), weighting=sel["weighting"],
        max_weight=float(sel["max_weight"]), min_weight=float(sel["min_weight"]),
    )
    logger.info(f"面板/权重就绪：{len(weights)} 条，调仓日 {weights['trade_date'].nunique()} 个")

    # ---------------- 无风控基准（一次） ----------------
    res0 = bt.run(panel, weights, start_date=args.start)
    m0 = res0["metrics"]
    print(f"\n无风控基准: 总收益 {m0['总收益率']:.4f}  夏普 {m0['夏普比率']:.4f}  "
          f"回撤 {m0['最大回撤']:.4f}")

    base_cfg = get_config("risk")

    # ---------------- 网格扫描 ----------------
    rows = []
    for g in GRID:
        cfg = deepcopy(base_cfg)
        cfg["stop_loss"]["single_stock_drawdown"] = g["sl"]
        cfg["stop_loss"]["trailing_stop"] = g["trail"]
        cfg["circuit_breaker"]["drawdown_trigger"] = g["cb"]
        # 机制拆分：独立开关止损/熔断
        cfg["stop_loss"]["enabled"] = g.get("sl_on", True)
        cfg["circuit_breaker"]["enabled"] = g.get("cb_on", True)

        rm = RiskManager(cfg)
        w_risk, events = rm.run_risk_controls(weights, panel,
                                              base_equity=res0["equity_curve"])
        res1 = bt.run(panel, w_risk, start_date=args.start)
        m1 = res1["metrics"]
        calmar = m1.get("年化收益率", 0) / m1.get("最大回撤", 1) if m1.get("最大回撤") else 0
        rows.append({
            "组": g["name"], "止损": g["sl"], "移动": g["trail"], "熔断": g["cb"],
            "事件数": len(events),
            "总收益": round(m1["总收益率"], 4), "年化": round(m1["年化收益率"], 4),
            "夏普": round(m1["夏普比率"], 4), "回撤": round(m1["最大回撤"], 4),
            "卡玛": round(calmar, 4),
        })
        print(f"  {g['name']}: 收益 {m1['总收益率']:.4f}  夏普 {m1['夏普比率']:.4f}  "
              f"回撤 {m1['最大回撤']:.4f}  卡玛 {calmar:.4f}  事件 {len(events)}")

    df = pd.DataFrame(rows)
    print("\n" + "=" * 80)
    print(df.to_string(index=False))
    print("=" * 80)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out = ROOT / "outputs" / "risk"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f"param_scan_{tag}.csv", index=False, encoding="utf-8-sig")
    print(f"扫描结果: {out}/param_scan_{tag}.csv")

    # 选最优：卡玛比率最高（风险调整后收益）
    best = df.loc[df["卡玛"].idxmax()]
    print(f"\n最优组: {best['组']}  （卡玛 {best['卡玛']:.4f}）")

    if args.apply:
        import yaml
        path = ROOT / "config" / "risk.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw["stop_loss"]["single_stock_drawdown"] = float(best["止损"])
        raw["stop_loss"]["trailing_stop"] = float(best["移动"])
        raw["circuit_breaker"]["drawdown_trigger"] = float(best["熔断"])
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        print(f"已写回 {path}")


if __name__ == "__main__":
    main()
