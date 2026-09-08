# -*- coding: utf-8 -*-
"""
组合回测的风控前后对比（桌面端「风控与监控」页数据源）。

与 example_risk.py 的区别：不重建因子和权重，直接读 run_combo_strategy
的产物（combo_weights_{tag}.csv + combo_equity_{tag}.csv）——
「无风控」一列 = 该 tag 的组合回测原结果，两页数字严格一致；
「有风控」一列 = 对同一权重表套 RiskManager 闸门后重跑撮合。

闸门参数由命令行显式给定（桌面端调参框），给定即启用：
    --stop -0.25      个股成本回撤止损
    --trailing -0.30  移动止损
    --fuse -0.12      组合回撤熔断（降仓至 risk.yaml reduce_to）
不给则沿用 config/risk.yaml（当前默认全关 → 有风控≈仓位约束修边）。

用法：
    python scripts/run_combo_risk.py [--tag 20260909_0028]
        [--stop -0.25 --trailing -0.30 --fuse -0.12]
        [--final5 其他final5目录]
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config
from src.common.logger import logger
from src.common.paths import get_final5_dir, get_outputs_root
from src.layer3_strategy.portfolio_backtest import PortfolioBacktester
from src.layer4_risk.risk_manager import RiskManager
from src.layer4_risk.risk_metrics import RiskMetrics

METRIC_KEYS = ["总收益率", "年化收益率", "年化波动率", "夏普比率", "最大回撤",
               "卡玛比率", "var", "cvar"]


def _latest_tag(final5: Path) -> str:
    files = sorted(final5.glob("combo_metrics_*.csv"), reverse=True)
    if not files:
        sys.exit(f"{final5} 下没有 combo_metrics_* 产物，请先跑一次组合回测")
    return files[0].stem[len("combo_metrics_"):]


def main() -> None:
    ap = argparse.ArgumentParser(description="组合回测风控前后对比")
    ap.add_argument("--tag", default="", help="组合回测 tag（默认取最新）")
    ap.add_argument("--stop", type=float, default=None, help="个股止损阈值（给定即启用）")
    ap.add_argument("--trailing", type=float, default=None, help="移动止损阈值（给定即启用）")
    ap.add_argument("--fuse", type=float, default=None, help="组合熔断回撤阈值（给定即启用）")
    ap.add_argument("--final5", default="", help="override final5 目录（默认 paths 解析）")
    args = ap.parse_args()

    final5 = Path(args.final5) if args.final5 else get_final5_dir()
    tag = args.tag or _latest_tag(final5)
    w_path = final5 / f"combo_weights_{tag}.csv"
    e_path = final5 / f"combo_equity_{tag}.csv"
    if not w_path.exists() or not e_path.exists():
        sys.exit(f"tag {tag} 缺少权重/净值产物: {w_path} / {e_path}")

    start = None
    cfg_json = final5 / f"combo_config_{tag}.json"
    if cfg_json.exists():
        import json
        start = (json.loads(cfg_json.read_text(encoding="utf-8"))
                 .get("period", {}).get("start"))

    logger.info(f"风控对比: tag={tag}  final5={final5}")
    weights = pd.read_csv(w_path, dtype={"ts_code": str})
    weights["trade_date"] = pd.to_datetime(weights["trade_date"]).dt.date
    if "is_signal" in weights.columns:
        weights["is_signal"] = weights["is_signal"].astype(str) == "True"
    eq = pd.read_csv(e_path)
    eq_plain = pd.Series(eq["equity"].values,
                         index=pd.to_datetime(eq["trade_date"]).dt.date,
                         name="equity")

    # ---------------- 面板（缓存离线） ----------------
    cache = final5 / "panel_cache_2125.pkl"
    if not cache.exists():
        sys.exit(f"面板缓存不存在: {cache}")
    panel = pd.read_pickle(cache)

    # ---------------- 风控闸门 ----------------
    cfg = copy.deepcopy(get_config("risk"))
    gates = []
    if args.stop is not None or args.trailing is not None:
        sl = cfg.setdefault("stop_loss", {})
        sl["enabled"] = True
        if args.stop is not None:
            sl["single_stock_drawdown"] = -abs(args.stop)
            gates.append(f"止损{sl['single_stock_drawdown']:.0%}")
        if args.trailing is not None:
            sl["trailing_stop"] = -abs(args.trailing)
            gates.append(f"移动{sl['trailing_stop']:.0%}")
    if args.fuse is not None:
        cb = cfg.setdefault("circuit_breaker", {})
        cb["enabled"] = True
        cb["drawdown_trigger"] = -abs(args.fuse)
        gates.append(f"熔断{cb['drawdown_trigger']:.0%}")
    logger.info("生效闸门: " + ("、".join(gates) if gates
                             else "仅仓位约束（止损/熔断按 risk.yaml 默认关闭）"))

    rm = RiskManager(cfg)
    w_risk, events = rm.run_risk_controls(weights, panel, base_equity=eq_plain)

    # ---------------- 风控后重跑撮合 ----------------
    init_cash = float(get_config("strategy")["backtest"]["initial_cash"])
    bt = PortfolioBacktester(initial_capital=init_cash)
    res = bt.run(panel, w_risk, start_date=start)
    eq_risk = res["equity_curve"]

    # ---------------- 报告 ----------------
    m_p, m_r = RiskMetrics(eq_plain), RiskMetrics(eq_risk)
    rows = []
    for k in METRIC_KEYS:
        a = m_p.summary().get(k)
        b = m_r.summary().get(k)
        rows.append({"指标": k, "无风控": round(a, 4) if a is not None else None,
                     "有风控": round(b, 4) if b is not None else None})
    df = pd.DataFrame(rows)
    print("\n" + "=" * 64)
    print(f"  风控前后对比（组合 tag {tag}）" + ("  闸门: " + "、".join(gates) if gates else ""))
    print("=" * 64)
    print(df.to_string(index=False))

    out_dir = get_outputs_root() / "risk"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / f"risk_report_{tag}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(events).to_csv(out_dir / f"risk_events_{tag}.csv",
                                index=False, encoding="utf-8-sig")
    eq_plain.rename("equity").to_frame().to_csv(
        out_dir / f"equity_plain_{tag}.csv", encoding="utf-8-sig")
    eq_risk.rename("equity").to_frame().to_csv(
        out_dir / f"equity_risk_{tag}.csv", encoding="utf-8-sig")
    logger.info(f"风控产物已写入 {out_dir}（tag {tag}，事件 {len(events)} 条）")


if __name__ == "__main__":
    main()
