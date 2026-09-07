# -*- coding: utf-8 -*-
"""
第 3 层主入口：五因子 Top50 月度调仓策略（完整流水线）。

流程：
    库内行情/市值/财务/行业 -> 因子面板(PIT对齐) -> 因子计算
    -> MAD去极值/zscore标准化/行业市值中性化 -> 等权合成综合分
    -> 月末信号日选 Top50 -> 流通市值加权(上下限) -> 次日开盘成交
    -> 向量化回测(完整A股成本+停牌涨停约束) -> 指标+净值曲线

用法：
    # 正式跑（样本外 2019~2023）
    python scripts/run_top50_strategy.py

    # 指定区间 / 限样本（冒烟测试）
    python scripts/run_top50_strategy.py --start 2023-01-01 --end 2024-12-31 --codes 000001,600000,000002
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config
from src.common.db import read_sql
from src.common.logger import logger
from src.layer3_strategy.factors.composite import run_factor_pipeline
from src.layer3_strategy.panel import build_factor_panel, coverage_report
from src.layer3_strategy.portfolio_backtest import (
    PortfolioBacktester,
    cost_tier_config,
    monthly_rebalance_dates,
)

# 中文绘图
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------- 实验追踪（P1-8）

def _code_hash(roots: list) -> str:
    """对关键代码目录做整体 hash，标识「这份结果由哪一版代码产生」。"""
    h = hashlib.sha256()
    for root in roots:
        for p in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            try:
                h.update(p.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:16]


def write_run_info(out_dir: Path, args, start: str, end: str,
                   metrics: dict, extra: dict | None = None) -> Path:
    """每次运行落盘 run_info.json：时间戳 + config 全文 + 代码 hash。

    P1-8：任何一次绩效数字必须能追溯到「当时的配置 + 当时的代码」，
    否则改过参数后旧结果就说不清是哪个版本跑出来的了。
    """
    info: dict = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "args": vars(args),
        "period": {"start": start, "end": end},
        "code_hash": _code_hash([ROOT / "src", ROOT / "scripts"]),
        "configs": {},
        "metrics": {k: (float(v) if isinstance(v, (int, float, np.floating)) else str(v))
                    for k, v in metrics.items()},
    }
    if extra:
        info["extra"] = extra
    for cfg_name in ("strategy", "factors", "costs", "risk", "universe"):
        cfg_file = ROOT / "config" / f"{cfg_name}.yaml"
        try:
            info["configs"][cfg_name] = cfg_file.read_text(encoding="utf-8")
        except OSError:
            info["configs"][cfg_name] = "<读取失败>"
    path = out_dir / "run_info.json"
    path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"实验追踪已写入 {path}（code_hash={info['code_hash']}）")
    return path


def load_benchmark(start: str, end: str) -> pd.Series:
    """沪深300 收盘价序列（用于基准净值）。"""
    df = read_sql(
        """SELECT trade_date, close FROM index_daily
           WHERE index_code = '000300' AND trade_date BETWEEN :s AND :e
           ORDER BY trade_date""",
        {"s": start, "e": end},
    )
    if df.empty:
        logger.warning("基准数据为空（index_daily 缺 000300）")
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df.set_index("trade_date")["close"].astype(float)
    return s / s.iloc[0]


def save_equity_chart(
    equity: pd.Series,
    bench: pd.Series,
    out_png: Path,
    title: str,
) -> None:
    """策略净值 vs 基准净值。"""
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=130)
    ax.plot(equity.index, equity.values, label="策略", linewidth=1.6, color="#c0392b")
    if len(bench) > 0:
        ax.plot(bench.index, bench.values, label="沪深300", linewidth=1.2,
                color="#7f8c8d", alpha=0.85)
    ax.set_title(title, fontsize=13, pad=12)
    ax.set_ylabel("净值")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    logger.info(f"净值图已保存: {out_png}")


def main() -> None:
    ap = argparse.ArgumentParser(description="五因子 Top50 月度调仓策略")
    ap.add_argument("--start", default=None, help="回测起始（默认取 strategy.yaml）")
    ap.add_argument("--end", default=None, help="回测结束（默认取 strategy.yaml）")
    ap.add_argument("--codes", default="", help="逗号分隔的代码子集（冒烟测试用）")
    ap.add_argument("--out", default="outputs/top50", help="输出目录")
    ap.add_argument("--skip-financial", action="store_true", help="跳过财务因子（财务表未入库时用）")
    ap.add_argument("--risk", action="store_true",
                    help="接入组合熔断（默认关闭）。2026-08-31 熔断 T+1 去前视后"
                         "样本外(2019-23)夏普 0.245→0.194 为负贡献，故默认关闭；"
                         "样本内(2015-18)为正贡献（长熊保险费），熊市担忧者可开")
    ap.add_argument("--cost-tier", default=None,
                    help="成本档位：conservative(默认,滑点×1.5) | optimistic(滑点×1.0)。"
                         "impact_coef 未实证，正式绩效一律用保守档")
    args = ap.parse_args()

    scfg = get_config("strategy")
    bt_cfg = scfg["backtest"]
    sel = scfg["selection"]
    start = args.start or bt_cfg["start"]
    end = args.end or bt_cfg["end"]
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")

    logger.info(f"===== Top50 多因子策略 =====  区间 {start} ~ {end}"
                f"  股票数 {len(codes) if codes else '全量'}")
    fcfg = get_config("factors")
    comp_method = fcfg.get("composite", {}).get("method", "?")
    weights_desc = fcfg.get("composite", {}).get("weights") or {}
    logger.info(f"策略: 五因子合成({comp_method}, 权重{weights_desc}) / "
                f"月末信号 / 次月首日开盘 / {sel['top_n']} 只 / {sel['weighting']} 加权")

    # ---------------- 1. 因子面板（PIT 对齐） ----------------
    # lookback 回填：动量(250+21)/波动(60) 等因子需要回测起点之前的历史。
    # 面板从 start 前 LOOKBACK 天开始查，回测净值仍从 start 起算，
    # 否则回测前 ~12 个月因子全是 NaN，选股退化、组合长期空仓。
    LOOKBACK_DAYS = 400   # 覆盖最大因子窗口(250+21=271) + 余量；太大拖慢面板查询
    panel_start = (pd.Timestamp(start) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    panel = build_factor_panel(
        panel_start, end, codes=codes,
        with_financial=not args.skip_financial,
        with_industry=True,
        adj_type="qfq",
    )
    if panel.empty:
        logger.error("面板为空，终止")
        sys.exit(1)
    n_stock = panel["ts_code"].nunique()
    n_dates = panel["trade_date"].nunique()
    logger.info(f"面板就绪: {n_stock} 只 × {n_dates} 个交易日"
                f"（lookback {panel_start} 起，回测 {start} 起）")

    # ---- 幸存者偏差对账（P0-1）：面板 vs 当年全市场上市家数 ----
    coverage_report(panel, start=start, end=end)

    # ---------------- 2. 因子流水线（三步拆分，内存优化） ----------------
    # lookback 段只用于因子计算（shift 需要历史），预处理/选股/回测
    # 只需要回测区间 —— 在预处理前裁掉 lookback，610 万行面板省 35% 内存，
    # 否则预处理阶段（每因子 4 列副本 × 5 因子）峰值内存会撑爆。
    from src.layer3_strategy.factors.composite import (
        composite_score,
        compute_all_factors,
        preprocess_all_factors,
    )

    panel, names = compute_all_factors(panel)
    panel = panel[panel["trade_date"] >= pd.Timestamp(start).date()].copy()
    panel, score_cols = preprocess_all_factors(panel, names)
    panel["composite_score"] = composite_score(panel, score_cols)

    # ---------------- 3. 调仓日 + 权重 ----------------
    trade_dates = sorted(panel["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=start, end=end)
    logger.info(f"调仓日: {len(rb)} 个（每月末最后交易日）")

    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg["initial_cash"]),
        cost_tier=args.cost_tier,
    )
    tier_info = cost_tier_config(args.cost_tier)
    logger.info(f"成本档位: {tier_info['label']}（impact_coef 未实证，滑点×{tier_info['slip_multiplier']}）")
    weights = bt.build_weights(
        panel, rb,
        top_n=int(sel["top_n"]),
        weighting=sel["weighting"],
        max_weight=float(sel["max_weight"]),
        min_weight=float(sel["min_weight"]),
    )
    if weights.empty:
        logger.error("权重为空（可能调仓日无候选），终止")
        sys.exit(1)
    avg_hold = weights.groupby("trade_date").size().mean()
    logger.info(f"权重构建完成: {len(weights)} 条记录，平均持仓 {avg_hold:.1f} 只")

    # ---------------- 4. 回测（可选接风控） ----------------
    # P1-9 基准：沪深300 归一化净值，传入回测器用于 alpha/beta/IR 计算
    bench = load_benchmark(start, end)
    res = bt.run(panel, weights, start_date=start, benchmark=bench)
    equity = res["equity_curve"]

    if args.risk:
        # 可选接风控（组合熔断）。2026-08-31 熔断 T+1 去前视后样本外为负贡献
        # （夏普 0.245→0.194），故默认关闭；样本内 2015-18 正贡献（长熊保险）。
        from src.layer4_risk.risk_manager import RiskManager
        rm = RiskManager()
        logger.info("风控闸门介入主链路（仓位约束 + 个股止损 + 组合熔断）")
        # P1-6：打印实际生效项清单 —— 明确告诉你风控拦了什么，
        # 避免「以为开了止损其实没开」的死配置陷阱
        for line in rm.effective_controls():
            logger.info(f"  [风控生效项] {line}")
        w_risk, events = rm.run_risk_controls(weights, panel,
                                              base_equity=equity)
        res_risk = bt.run(panel, w_risk, start_date=start, benchmark=bench)
        # 风控前后对比
        m0, m1 = res["metrics"], res_risk["metrics"]
        print("\n" + "-" * 60)
        print(f"  风控对比（{len(events)} 条事件）")
        print("-" * 60)
        for k in ("总收益率", "年化收益率", "夏普比率", "最大回撤", "卡玛比率"):
            v0, v1 = m0.get(k), m1.get(k)
            if v0 is not None and v1 is not None:
                print(f"  {k:<10s} 无风控 {v0:>10.4f}  有风控 {v1:>10.4f}")
        print("-" * 60)
        # 保存双线净值（与 example_risk 产物格式一致，前端共用）
        equity.rename("equity").to_frame().to_csv(
            out_dir / f"equity_plain_{tag}.csv", encoding="utf-8-sig")
        res_risk["equity_curve"].rename("equity").to_frame().to_csv(
            out_dir / f"equity_risk_{tag}.csv", encoding="utf-8-sig")
        # 风控后作为主结果输出
        res = res_risk
        equity = res_risk["equity_curve"]

    metrics = res["metrics"]

    # ---------------- 5. 输出 ----------------
    print("\n" + "=" * 60)
    print(f"  Top50 多因子策略绩效    {start} ~ {end}   (n={n_stock}只)")
    print("=" * 60)
    for k, v in metrics.items():
        if isinstance(v, (int, float, np.floating)):
            print(f"  {k:<12s} {v:>14.4f}")
        else:
            print(f"  {k:<12s} {str(v):>14s}")
    print("=" * 60)

    # 保存产物
    eq_csv = out_dir / f"equity_{tag}.csv"
    w_csv = out_dir / f"weights_{tag}.csv"
    m_csv = out_dir / f"metrics_{tag}.csv"
    equity.rename("equity").to_frame().to_csv(eq_csv, encoding="utf-8-sig")
    weights.to_csv(w_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame([metrics]).T.to_csv(m_csv, encoding="utf-8-sig")

    chart = out_dir / f"equity_{tag}.png"
    save_equity_chart(equity, bench, chart,
                      f"五因子 Top50 月度调仓  {start}~{end}")

    # P1-8 实验追踪：每次运行落盘 run_info.json（时间戳/config全文/代码hash）
    write_run_info(out_dir, args, start, end, metrics,
                   extra={"n_stocks": int(n_stock),
                          "cost_tier": cost_tier_config(args.cost_tier)["label"]})

    print(f"\n产物已保存到 {out_dir}/")
    print(f"  净值曲线: equity_{tag}.png")
    print(f"  净值序列: equity_{tag}.csv")
    print(f"  权重明细: weights_{tag}.csv")
    print(f"  绩效指标: metrics_{tag}.csv")
    print(f"  实验追踪: run_info.json")


if __name__ == "__main__":
    main()
