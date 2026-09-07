# -*- coding: utf-8 -*-
"""
第 3 层验收：因子有效性检验（IC / 分层收益）。

对五因子做标准化体检：
    IC           因子值与未来 20 日收益的秩相关（越大越有效）
    ICIR         IC 均值 / IC 标准差（考虑稳定性）
    ​分层收益      按因子分 5 组，看高分组是否跑赢低分组（单调性）

用法：
    python scripts/factor_evaluation.py [--start 2019-01-01 --end 2023-12-31]
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

from src.common.logger import logger
from src.layer3_strategy.factors.composite import (
    composite_score,
    compute_all_factors,
    preprocess_all_factors,
)
from src.layer3_strategy.factors.evaluation import evaluate_factors, quantile_returns
from src.layer3_strategy.panel import add_forward_returns, build_factor_panel

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


def main() -> None:
    ap = argparse.ArgumentParser(description="因子 IC / 分层检验")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2023-12-31")
    ap.add_argument("--codes", default="")
    ap.add_argument("--out", default="outputs/factors")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    # 区间标注写进产物名与内容（此前无区间列，三个月后分不清跑的哪个区间）
    span = f"{args.start[:4]}-{args.end[:4]}"

    # 面板（带 lookback，因子窗口有历史可用）
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    panel = build_factor_panel(ps, args.end, codes=codes, with_financial=True,
                               with_industry=True, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")

    # 因子流水线（三步拆分：compute → 裁掉 lookback → preprocess/合成）
    panel, names = compute_all_factors(panel)
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    panel, score_cols = preprocess_all_factors(panel, names)
    panel["composite_score"] = composite_score(panel, score_cols)
    # 删除预处理中间列（f_*_raw / *_wins / *_z / *_neut 共 20 列），
    # 只留 score 列 —— IC 计算阶段内存减 60%
    drop = [c for c in panel.columns
            if c.endswith(("_raw", "_wins", "_z", "_neut")) and c != "composite_score"]
    panel = panel.drop(columns=drop)
    logger.info(f"参与检验的因子: {score_cols}")

    # 未来收益（仅评估用）
    panel = add_forward_returns(panel, periods=(1, 5, 20))
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start).date()]

    # 完整评估（20 日 IC）
    ev = evaluate_factors(panel, score_cols, fwd_col="fwd_ret_20d", n_groups=5)
    print("\n" + "=" * 78)
    print(f"  因子有效性检验    {args.start} ~ {args.end}")
    print("=" * 78)
    print(ev.to_string(index=False))
    print("=" * 78)

    ev = ev.copy()
    ev.insert(0, "区间", span)   # 元数据：区分样本内/样本外/滚动段
    ev.to_csv(out_dir / f"factor_ic_{span}_{tag}.csv", index=False, encoding="utf-8-sig")

    # IC 时间序列（前端折线图数据）：每月度 IC × 因子
    from src.layer3_strategy.factors.evaluation import calc_ic
    ic_series = {}
    for col in score_cols:
        if col not in panel.columns:
            continue
        s = calc_ic(panel, col, "fwd_ret_20d")
        ic_series[col.replace("f_", "").replace("_score", "")] = s
    if ic_series:
        ic_df = pd.DataFrame(ic_series).sort_index()
        ic_df.insert(0, "区间", span)
        ic_df.to_csv(out_dir / f"factor_ic_series_{span}_{tag}.csv", encoding="utf-8-sig")

    # 分层收益数据（前端柱状图）
    qr_all = []
    for col in score_cols:
        qr = quantile_returns(panel, col, "fwd_ret_20d", 5)
        if qr.empty:
            continue
        q = qr.copy()
        q["factor"] = col.replace("f_", "").replace("_score", "")
        qr_all.append(q)
    if qr_all:
        pd.concat(qr_all, ignore_index=True).to_csv(
            out_dir / f"factor_quantile_{tag}.csv", index=False, encoding="utf-8-sig")

    # 分层收益图（每因子一组小图）
    n = len(score_cols)
    fig, axes = plt.subplots(1, max(n, 1), figsize=(4.2 * max(n, 1), 3.6), dpi=120)
    axes = [axes] if n == 1 else axes
    for ax, col in zip(axes, score_cols):
        qr = quantile_returns(panel, col, "fwd_ret_20d", 5)
        if qr.empty:
            ax.set_title(f"{col}: 无数据")
            continue
        ax.bar(range(1, len(qr) + 1), qr["mean_return"] * 100, color="#5b8ff9")
        ax.set_title(f"{col}\n20日分层收益(%)", fontsize=10)
        ax.set_xlabel("组（1=最低因子分, 5=最高）")
        ax.axhline(0, color="gray", linewidth=0.8)
        ax.grid(True, alpha=0.3)
    fig.suptitle(f"因子分层收益  {args.start}~{args.end}", fontsize=12)
    fig.tight_layout()
    chart = out_dir / f"factor_quantile_{tag}.png"
    fig.savefig(chart)
    plt.close(fig)
    logger.info(f"分层图已保存: {chart}")


if __name__ == "__main__":
    main()
