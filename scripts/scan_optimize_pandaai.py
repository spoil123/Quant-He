# -*- coding: utf-8 -*-
"""
5 因子迭代优化扫描: 目标纯多头夏普≈1.2 + 降波动/回撤（可让年化）。

一次构建面板与成分, 批量跑多组变体回测。变体 = 结构配方 × TopN × 加权。
三组杠杆:
  A. 成分: 原四成分(illiq/tur/size/bm) 逐步加低波成分 vol(0.05~0.20)、提 BM
  B. 持仓: TopN 50/100/150, 单票上限收紧 5%->3%
  C. 加权: float_mv / sqrt_mv / equal
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config                  # noqa: E402
from src.common.db import read_sql                         # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.panel import attach_raw_close, build_factor_panel   # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    PortfolioBacktester, cost_tier_config, monthly_rebalance_dates,
)

# 基线因子（平台五因子权重）
BASE = {
    "b6":  {"illiq": 0.30, "tur": 0.30, "size": 0.20, "bm": 0.20},
    "b7":  {"illiq": 0.25, "tur": 0.25, "size": 0.20, "bm": 0.30},
    "b13": {"illiq": 0.20, "tur": 0.20, "size": 0.25, "bm": 0.35},
    "r1k": {"illiq": 0.2125, "tur": 0.2125, "size": 0.125, "bm": 0.45},
    "hp1": {"illiq": 0.30, "tur": 0.25, "size": 0.20, "bm": 0.25},
}

# 变体生成器: (名称, 权重dict含可选vol, topn, weighting, max_weight)
def variants():
    out = []
    R = BASE['r1k']
    # 合体: 5因子等权合成 (低相关多因子提夏普)
    out.append(('five-eq-t50', dict(BASE['b6'], _all=True), 50, 'float_mv', 0.05))
    out.append(('five-eq-t60', dict(BASE['b6'], _all=True), 60, 'float_mv', 0.04))
    out.append(('five-v15-t60', dict(BASE['b6'], _all=True, vol=0.15), 60, 'float_mv', 0.04))
    # 双周调仓(用 _biweek 标记, 在 main 内处理)
    out.append(('r1k-t60-biweek', dict(R), 60, 'float_mv', 0.04))
    out.append(('five-t60-biweek', dict(BASE['b6'], _all=True), 60, 'float_mv', 0.04))
    return out


def load_benchmark(start: str, end: str) -> pd.Series:
    df = read_sql(
        """SELECT trade_date, close FROM index_daily
           WHERE index_code='000300' AND trade_date BETWEEN :s AND :e
           ORDER BY trade_date""", {"s": start, "e": end})
    if df.empty:
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df.set_index("trade_date")["close"].astype(float)
    return s / s.iloc[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--codes", default="")
    ap.add_argument("--out", default="outputs/top5_opt")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    span = f"{args.start[:4]}-{args.end[:4]}"
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}

    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    t0 = time.time()
    logger.info(f"构建面板 {ps} ~ {args.end} ...")
    panel = build_factor_panel(ps, args.end, codes=codes,
                               with_financial=True, with_industry=True, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    # 估值因子要用不复权价配 BPS（同口径），见 attach_raw_close 文档
    panel = attach_raw_close(panel)
    logger.info(f"面板 {len(panel):,} 行 {panel['ts_code'].nunique()} 只, 耗时 {time.time()-t0:.0f}s")

    # ---- 成分 (截面 RANK 0~1 越大越优) ----
    p = panel.copy()
    eps = np.finfo(np.float32).eps
    p["_logamt"] = np.log(np.maximum(p["amount"], eps))
    p["_logmv"] = np.log(np.maximum(p["float_mv"], eps))
    # BM 必须用不复权价：BPS 是披露当期原始值，未做复权，
    # 除以 qfq 价会让送转股的历史 BM 系统性放大（M4 修复）
    p["_bm"] = pd.to_numeric(p["bps"], errors="coerce") / pd.to_numeric(
        p.get("raw_close", p["close"]), errors="coerce").replace(0, np.nan)
    # 波动率: 20日收益标准差（需要逐股滚动，用 close）
    g_close = p.groupby("ts_code", sort=False)["close"]
    ret = p["close"] / g_close.shift(1) - 1.0
    p["_vol20"] = ret.groupby(p["ts_code"], sort=False).transform(
        lambda x: x.rolling(20, min_periods=10).std())

    p["_r_illiq"] = 1.0 - p.groupby("trade_date")["_logamt"].rank(pct=True)
    p["_r_tur"] = 1.0 - p.groupby("trade_date")["turnover_rate"].rank(pct=True)
    p["_r_size"] = 1.0 - p.groupby("trade_date")["_logmv"].rank(pct=True)
    p["_r_bm"] = p.groupby("trade_date")["_bm"].rank(pct=True)
    p["_r_vol"] = 1.0 - p.groupby("trade_date")["_vol20"].rank(pct=True)
    comp_cols = {"illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size",
                 "bm": "_r_bm", "vol": "_r_vol"}

    # 只保留回测区间
    p = p[p["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    trade_dates = sorted(p["trade_date"].unique())
    rb_month = monthly_rebalance_dates(trade_dates, start=args.start, end=args.end)
    # 双周调仓: 每月1/15号附近（用 10 个交易日间隔近似）
    s = pd.Series(pd.to_datetime(trade_dates)).sort_values()
    rb_biweek = []
    last = None
    for d in s:
        if last is None or (d - last).days >= 10:
            rb_biweek.append(d.date()); last = d
    bench = load_benchmark(args.start, args.end)
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    # ---- 批量变体 ----
    vlist = variants()
    # 去掉重复（b13-v10-eq 会覆盖同名）: 用集合保证唯一
    seen, uniq = set(), []
    for v in vlist:
        if v[0] not in seen:
            seen.add(v[0]); uniq.append(v)
    vlist = uniq
    logger.info(f"变体总数: {len(vlist)} | 月调仓 {len(rb_month)} 双周 {len(rb_biweek)}")

    rows = []
    for vi, (name, w, topn, wtype, mw) in enumerate(vlist, 1):
        rb = rb_biweek if "biweek" in name else rb_month
        if w.get("_all"):
            # 5因子等权合成: 先各自归一成分分, 再平均
            tot = sum({k: v for k, v in w.items() if k != "_all" and k != "vol"}.values())
            wnorm = {k: v / tot for k, v in w.items() if k != "_all"}
            val = pd.Series(0.0, index=p.index)
            per = {}
            for kk in BASE:
                ww = {k: (v if k != "vol" else w.get("vol", 0.0)) for k, v in wnorm.items()}
                tot2 = sum(ww.values())
                ww2 = {k: v / tot2 for k, v in ww.items()}
                vv = pd.Series(0.0, index=p.index)
                for k, col in comp_cols.items():
                    if k in ww2:
                        vv += ww2[k] * p[col]
                per[kk] = vv
            val = sum(per.values()) / len(per)
        else:
            tot = sum(w.values())
            wnorm = {k: v / tot for k, v in w.items()}
            val = pd.Series(0.0, index=p.index)
            for k, col in comp_cols.items():
                if k in wnorm:
                    val += wnorm[k] * p[col]
        p[f"_score"] = val
        t1 = time.time()
        # 微盘过滤: 变体名含 amt30m/amt100m 时提高日均成交额门槛
        min_amt = None
        if "amt30m" in name:
            min_amt = 30_000_000
        elif "amt100m" in name:
            min_amt = 100_000_000
        weights = bt.build_weights(
            p, rb, score_col="_score", top_n=topn, weighting=wtype,
            max_weight=mw, min_weight=0.001, min_avg_amount=min_amt)
        if weights.empty:
            logger.warning(f"[{vi}/{len(vlist)}] {name}: 权重空")
            continue
        res = bt.run(p, weights, start_date=args.start, benchmark=bench)
        m = res["metrics"]
        row = {
            "变体": name, "TopN": topn, "加权": wtype,
            "权重": json_w(wnorm),
            "累计收益": m.get("总收益率"), "年化收益": m.get("年化收益率"),
            "年化波动": m.get("年化波动率"), "夏普": m.get("夏普比率"),
            "索提诺": m.get("索提诺比率"), "最大回撤": m.get("最大回撤"),
            "基准年化": m.get("基准年化收益率"), "Alpha": m.get("年化Alpha"),
            "超额年化": m.get("超额年化收益率"), "IR": m.get("信息比率"),
            "换手": m.get("年化换手率"), "持仓数": m.get("平均持仓数"),
        }
        rows.append(row)
        logger.info(f"[{vi}/{len(vlist)}] {name:22s} Top{topn} {wtype:8s} "
                    f"夏普={m.get('夏普比率'):.3f} 回撤={m.get('最大回撤')*100:.1f}% "
                    f"波动={m.get('年化波动率')*100:.1f}% 年化={m.get('年化收益率')*100:.1f}% "
                    f"({time.time()-t1:.0f}s)")

    res_df = pd.DataFrame(rows)
    res_df.to_csv(out_dir / f"top5_opt_{span}_{tag}.csv", index=False, encoding="utf-8-sig")
    print("\n" + "=" * 130)
    print(f"  5因子迭代优化扫描  {args.start}~{args.end}  目标: 夏普≥1.2 且低回撤")
    print("=" * 130)
    show = res_df.copy()
    for c in ("累计收益", "年化收益", "年化波动", "最大回撤", "基准年化", "超额年化"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    show = show.sort_values("夏普", ascending=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    cols = ["变体", "TopN", "加权", "累计收益", "年化收益", "年化波动", "夏普",
            "索提诺", "最大回撤", "超额年化", "IR", "换手", "持仓数"]
    print(show[cols].to_string(index=False))
    print("=" * 130)
    print(f"已保存: {out_dir / f'top5_opt_{span}_{tag}.csv'}")


def json_w(w: dict) -> str:
    return " ".join(f"{k}{v:.0%}" for k, v in sorted(w.items()))


if __name__ == "__main__":
    main()
