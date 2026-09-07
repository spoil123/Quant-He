# -*- coding: utf-8 -*-
"""
复现 PandaAI 平台 Top5 因子到本地系统，做本地评估（IC/ICIR/分层/多空）。

对比口径说明：
- 平台因子: 各成分先做截面 RANK(百分位) 再按权重线性合成; 无行业/市值中性化。
- 本脚本在本地数据(MySQL)上重建完全相同结构:
    illiq  = RANK(-log1p(amount))   流动性(低成交额=高 illiq)  [平台 -LOGABS(AMOUNT)]
    tur    = RANK(-turnover_rate)   低换手
    size   = RANK(-log(float_mv))   小市值(流通)
    bm     = RANK(bps/close)        账面市值比(本地 bps 经 ann_date PIT 对齐)
  再按 5 因子的权重线性合成, 周期不同仅影响调仓, IC 评估用日频横截面。
- 与平台可比: 评估窗口同为 2023-01-01 ~ 2025-12-31, 剔除 ST。
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

from src.common.logger import logger          # noqa: E402
from src.layer3_strategy.panel import (       # noqa: E402
    add_forward_returns, load_financial_pit, load_price_panel,
)
from src.layer3_strategy.factors.evaluation import calc_ic, ic_summary, quantile_returns  # noqa: E402

# ---------------- 平台 Top5 因子定义 ----------------
# name: (周期, 各成分权重 dict{illiq,tur,size,bm})
FACTOR_DEFS = {
    "b6-mix4":        (10, {"illiq": 0.30, "tur": 0.30, "size": 0.20, "bm": 0.20}),
    "b7-f_mix4_v3":   (10, {"illiq": 0.25, "tur": 0.25, "size": 0.20, "bm": 0.30}),
    "b13-f_c3_5th_a": (3,  {"illiq": 0.20, "tur": 0.20, "size": 0.25, "bm": 0.35}),
    "lowdd_r1k_c4":   (4,  {"illiq": 0.2125, "tur": 0.2125, "size": 0.125, "bm": 0.45}),
    "hp1-c1":         (1,  {"illiq": 0.30, "tur": 0.25, "size": 0.20, "bm": 0.25}),
}


def cross_rank(s: pd.Series) -> pd.Series:
    """截面百分位排名 0~1（RANK 算子等价，单调正比）。"""
    return s.rank(pct=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="PandaAI Top5 因子本地复现评估")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--out", default="outputs/factors")
    args = ap.parse_args()

    # lookback 400 天（因子窗口 + 动量留白充足）
    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    logger.info(f"构建面板 {ps} ~ {args.end} ...")
    panel = load_price_panel(ps, args.end, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    trade_dates = sorted(panel["trade_date"].unique())
    all_codes = sorted(panel["ts_code"].unique())
    fin_p = load_financial_pit(trade_dates, all_codes)
    if not fin_p.empty:
        panel = panel.merge(fin_p, on=["trade_date", "ts_code"], how="left")
    logger.info(f"面板原始 {len(panel):,} 行")

    # 剔除 ST / 停牌
    if "is_st" in panel.columns:
        n0 = len(panel)
        panel = panel[panel["is_st"].fillna(0).astype(int) == 0].copy()
        logger.info(f"剔除 ST: {n0 - len(panel):,} 行")
    if "is_suspended" in panel.columns:
        panel = panel[panel["is_suspended"].fillna(0).astype(int) == 0].copy()

    # 有效样本：需 amount/turnover_rate/float_mv/bps/close 齐全
    need = ["amount", "turnover_rate", "float_mv", "bps", "close"]
    for c in need:
        if c not in panel.columns:
            logger.error(f"缺少字段 {c}")
            sys.exit(1)

    # ---------------- 构造成分 ----------------
    p = panel.copy()
    eps = np.finfo(np.float32).eps
    p["_logamt"] = np.log(np.maximum(p["amount"], eps))
    p["_logmv"] = np.log(np.maximum(p["float_mv"], eps))
    p["_bm"] = p["bps"] / p["close"].replace(0, np.nan)

    # 截面 RANK（负号代表"越小越好"类成分, 统一转正方向: 值越大越优）
    p["_r_illiq"] = p.groupby("trade_date")["_logamt"].rank(pct=True) * -1.0
    p["_r_tur"] = p.groupby("trade_date")["turnover_rate"].rank(pct=True) * -1.0
    p["_r_size"] = p.groupby("trade_date")["_logmv"].rank(pct=True) * -1.0
    p["_r_bm"] = p.groupby("trade_date")["_bm"].rank(pct=True)

    # 负方向成分需要转 0~1 正比分: RANK(x)*w 中 RANK(-x) 与 -RANK(x) 单调等价,
    # 为可读性, 用 (1 - rank) 代替 -rank 保持 0~1 区间
    p["_r_illiq"] = 1.0 - p.groupby("trade_date")["_logamt"].rank(pct=True)
    p["_r_tur"] = 1.0 - p.groupby("trade_date")["turnover_rate"].rank(pct=True)
    p["_r_size"] = 1.0 - p.groupby("trade_date")["_logmv"].rank(pct=True)

    comps = {"illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm"}

    # 5 个合成因子列
    for fname, (cycle, w) in FACTOR_DEFS.items():
        val = pd.Series(0.0, index=p.index)
        for k, col in comps.items():
            val += w[k] * p[col]
        p[f"f_{fname}"] = val

    logger.info(f"评估窗口 {args.start} ~ {args.end}")
    ev_panel = p[p["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    ev_panel = add_forward_returns(ev_panel, periods=(1, 5, 20))
    score_cols = [f"f_{n}" for n in FACTOR_DEFS]

    # 多周期 IC: 1/5/20 日
    lines = []
    out_rows = []
    for n in (5, 20):
        fwd = f"fwd_ret_{n}d"
        for fname in FACTOR_DEFS:
            col = f"f_{fname}"
            ic = calc_ic(ev_panel, col, fwd)
            s = ic_summary(ic)
            qr = quantile_returns(ev_panel, col, fwd, 5)
            mono = np.nan
            if not qr.empty and len(qr) == 5:
                mono = float(pd.Series(qr["mean_return"].values).corr(
                    pd.Series(range(5)), method="spearman"))
            q1 = qr.iloc[0]["mean_return"] if len(qr) >= 1 else np.nan
            q5 = qr.iloc[-1]["mean_return"] if len(qr) >= 5 else np.nan
            ls = q5 - q1
            ann = qr.iloc[-1]["annualized"] if len(qr) >= 5 else np.nan
            out_rows.append({
                "因子": fname, "周期": FACTOR_DEFS[fname][0], "预测期(日)": n,
                "IC": round(s["ic_mean"], 4), "ICIR": round(s["ir"], 4),
                "IC>0占比": round(s["ic_positive_ratio"], 3), "t值": round(s["t_stat"], 2),
                "单调性": round(mono, 3) if mono == mono else None,
                "Q5月均收益%": round(q5 * 100, 3) if q5 == q5 else None,
                "Q1月均收益%": round(q1 * 100, 3) if q1 == q1 else None,
                "多空月均%": round(ls * 100, 3) if ls == ls else None,
                "Q5年化%": round(ann * 100, 1) if ann == ann else None,
            })

    res = pd.DataFrame(out_rows)
    # 展示: 按 20 日 IC 排序
    show = res[res["预测期(日)"] == 20].copy()
    show = show.sort_values("IC", ascending=False)
    print("\n" + "=" * 100)
    print(f"  PandaAI Top5 因子 · 本地复现评估（{args.start} ~ {args.end}, 剔ST）")
    print("=" * 100)
    print(show.to_string(index=False))

    # 逐周期详细
    for n in (5, 20):
        sub = res[res["预测期(日)"] == n].sort_values("IC", ascending=False)
        print(f"\n--- 预测期 {n} 日 ---")
        print(sub.to_string(index=False))

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    span = f"{args.start[:4]}-{args.end[:4]}"
    res.to_csv(out_dir / f"pandaai_top5_local_{span}_{tag}.csv",
               index=False, encoding="utf-8-sig")
    print(f"\n已保存: {out_dir / f'pandaai_top5_local_{span}_{tag}.csv'}")


if __name__ == "__main__":
    main()
