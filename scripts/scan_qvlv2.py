# -*- coding: utf-8 -*-
"""
优秀因子挖掘 第2轮: 在12成分池上再挖2个多头夏普>=1.2低回撤的新配方。
与第1轮已交付(QV-Mom40=momqv-t40-m6 / MD-Mom50=momdv-t50 / DV-LV50=dvdef-t50)差异化:
  - 质量动量家族 (gm/roe/ep 主导)
  - 红利质量家族 (dy+lvol+gm)
  - 反转+低波家族 (rev20+lvol+illiq)
  - momqv2 邻域 (第1轮已知 1.358, 候补)
用法: python scripts/scan_qvlv2.py [--start .. --end ..]
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
    PortfolioBacktester, monthly_rebalance_dates,
)


def variants():
    """(名称, 权重dict, topn, weighting, max_weight)"""
    out = []
    # ---- 质量动量家族 (毛利率/ROE 主导 + 动量, 无低波对照) ----
    out.append(("qm-t50", {"gm": 0.18, "roe": 0.20, "ep": 0.15, "mom": 0.20,
                           "bm": 0.12, "rev20": 0.15}, 50, "float_mv", 0.05))
    out.append(("qmlv-t50", {"roe": 0.15, "gm": 0.12, "ep": 0.10, "bm": 0.10,
                             "mom": 0.15, "lvol": 0.25, "tur": 0.08, "dy": 0.05}, 50, "float_mv", 0.05))
    out.append(("qmlv-t40-m6", {"roe": 0.15, "gm": 0.12, "ep": 0.10, "bm": 0.10,
                                "mom": 0.15, "lvol": 0.25, "tur": 0.08, "dy": 0.05}, 40, "float_mv", 0.06))
    out.append(("gmlv-t50", {"gm": 0.15, "roe": 0.12, "ep": 0.10, "lvol": 0.28,
                             "dy": 0.15, "bm": 0.10, "tur": 0.10}, 50, "float_mv", 0.05))
    # ---- 红利质量家族 ----
    out.append(("dvq-t50", {"dy": 0.22, "bm": 0.15, "ep": 0.10, "lvol": 0.28,
                            "gm": 0.10, "size": 0.08, "tur": 0.07}, 50, "float_mv", 0.05))
    out.append(("momdy2-t50", {"mom": 0.15, "dy": 0.22, "bm": 0.15, "ep": 0.10,
                               "lvol": 0.23, "roe": 0.10, "tur": 0.05}, 50, "float_mv", 0.05))
    # ---- 反转+低波家族 ----
    out.append(("revlv-t50", {"lvol": 0.30, "rev20": 0.20, "tur": 0.15,
                              "illiq": 0.15, "dy": 0.20}, 50, "float_mv", 0.05))
    out.append(("revlv2-t50", {"lvol": 0.25, "rev20": 0.25, "tur": 0.10,
                               "illiq": 0.15, "dy": 0.15, "size": 0.10}, 50, "float_mv", 0.05))
    # ---- momqv2 邻域 (第1轮已知 t50=1.358) ----
    momqv2 = {"mom": 0.20, "roe": 0.15, "ep": 0.10, "bm": 0.12,
              "lvol": 0.23, "tur": 0.10, "illiq": 0.10}
    out.append(("momqv2-t40-m6", dict(momqv2), 40, "float_mv", 0.06))
    out.append(("momqv2-t60-m4", dict(momqv2), 60, "float_mv", 0.04))
    # ---- momqv 换血: illiq -> dy ----
    out.append(("momqdy-t50", {"mom": 0.15, "roe": 0.18, "ep": 0.12, "bm": 0.12,
                               "lvol": 0.20, "dy": 0.13, "tur": 0.10}, 50, "float_mv", 0.05))
    # ---- dvdef2 t60 (第1轮 t50=1.165 差一口气) ----
    out.append(("dvdef2-t60-m4", {"dy": 0.25, "bm": 0.15, "ep": 0.12, "lvol": 0.28,
                                  "tur": 0.10, "size": 0.10}, 60, "float_mv", 0.04))
    return out


COMP_NAMES = ["illiq", "tur", "size", "bm", "ep", "dy", "roe", "gm",
              "lev", "lvol", "rev20", "mom"]


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
    ap.add_argument("--out", default="outputs/qvlv_scan")
    args = ap.parse_args()

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    span = f"{args.start[:4]}-{args.end[:4]}"
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}

    ps = (pd.Timestamp(args.start) - pd.Timedelta(days=460)).strftime("%Y-%m-%d")
    t0 = time.time()
    logger.info(f"构建面板 {ps} ~ {args.end} ...")
    panel = build_factor_panel(ps, args.end, with_financial=True,
                               with_industry=False, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    panel = attach_raw_close(panel)
    dv = read_sql(
        """SELECT trade_date, ts_code, dv_ttm FROM daily_basic
           WHERE trade_date BETWEEN :s AND :e""",
        {"s": ps, "e": args.end})
    dv["trade_date"] = pd.to_datetime(dv["trade_date"]).dt.date
    dv["dv_ttm"] = pd.to_numeric(dv["dv_ttm"], errors="coerce").astype("float32")
    panel = panel.merge(dv, on=["trade_date", "ts_code"], how="left")
    logger.info(f"面板 {len(panel):,} 行 {panel['ts_code'].nunique()} 只, 耗时 {time.time()-t0:.0f}s")

    # ---- 12 成分 (截面 RANK 0~1, 越大越优) ----
    p = panel.copy()
    eps_ = np.finfo(np.float32).eps
    raw_close = pd.to_numeric(p.get("raw_close", p["close"]), errors="coerce").replace(0, np.nan)
    p["_logamt"] = np.log(np.maximum(p["amount"], eps_))
    p["_logmv"] = np.log(np.maximum(p["float_mv"], eps_))
    p["_bm"] = pd.to_numeric(p["bps"], errors="coerce") / raw_close
    p["_ep"] = pd.to_numeric(p["eps_ttm"], errors="coerce") / raw_close
    p["_dy"] = pd.to_numeric(p["dv_ttm"], errors="coerce")
    p["_roe"] = pd.to_numeric(p["roe_ttm"], errors="coerce")
    p["_gm"] = pd.to_numeric(p["gross_margin"], errors="coerce")
    p["_lev"] = -pd.to_numeric(p["debt_ratio"], errors="coerce")

    g = p.groupby("ts_code", sort=False)
    ret = p["close"] / g["close"].shift(1) - 1.0
    p["_ret"] = ret
    p["_vol20"] = ret.groupby(p["ts_code"], sort=False).transform(
        lambda x: x.rolling(20, min_periods=10).std())
    p["_ret20"] = p["close"] / g["close"].shift(20) - 1.0
    p["_ret250"] = p["close"] / g["close"].shift(250) - 1.0
    p["_mom"] = p["_ret250"] - p["_ret20"]

    r = p.groupby("trade_date")
    comp_cols = {
        "illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm",
        "ep": "_r_ep", "dy": "_r_dy", "roe": "_r_roe", "gm": "_r_gm",
        "lev": "_r_lev", "lvol": "_r_lvol", "rev20": "_r_rev20", "mom": "_r_mom",
    }
    p["_r_illiq"] = 1.0 - r["_logamt"].rank(pct=True)
    p["_r_tur"] = 1.0 - r["turnover_rate"].rank(pct=True)
    p["_r_size"] = 1.0 - r["_logmv"].rank(pct=True)
    p["_r_bm"] = r["_bm"].rank(pct=True)
    p["_r_ep"] = r["_ep"].rank(pct=True)
    p["_r_dy"] = r["_dy"].rank(pct=True)
    p["_r_roe"] = r["_roe"].rank(pct=True)
    p["_r_gm"] = r["_gm"].rank(pct=True)
    p["_r_lev"] = r["_lev"].rank(pct=True)
    p["_r_lvol"] = 1.0 - r["_vol20"].rank(pct=True)
    p["_r_rev20"] = 1.0 - r["_ret20"].rank(pct=True)
    p["_r_mom"] = r["_mom"].rank(pct=True)
    logger.info(f"12成分 RANK 完成, 耗时 {time.time()-t0:.0f}s")

    p = p[p["trade_date"] >= pd.Timestamp(args.start).date()].copy()
    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=args.start, end=args.end)
    bench = load_benchmark(args.start, args.end)
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    vlist = variants()
    logger.info(f"变体总数: {len(vlist)}")

    rows = []
    for vi, (name, w, topn, wtype, mw) in enumerate(vlist, 1):
        tot = sum(w.values())
        wnorm = {k: v / tot for k, v in w.items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in comp_cols.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val
        t1 = time.time()
        weights = bt.build_weights(
            p, rb, score_col="_score", top_n=topn, weighting=wtype,
            max_weight=mw, min_weight=0.001)
        if weights.empty:
            logger.warning(f"[{vi}/{len(vlist)}] {name}: 权重空")
            continue
        res = bt.run(p, weights, start_date=args.start, benchmark=bench)
        m = res["metrics"]
        rows.append({
            "变体": name, "TopN": topn, "加权": wtype, "权重": json_w(wnorm),
            "累计收益": m.get("总收益率"), "年化收益": m.get("年化收益率"),
            "年化波动": m.get("年化波动率"), "夏普": m.get("夏普比率"),
            "索提诺": m.get("索提诺比率"), "最大回撤": m.get("最大回撤"),
            "基准年化": m.get("基准年化收益率"), "Alpha": m.get("年化Alpha"),
            "超额年化": m.get("超额年化收益率"), "IR": m.get("信息比率"),
            "换手": m.get("年化换手率"), "持仓数": m.get("平均持仓数"),
        })
        logger.info(f"[{vi}/{len(vlist)}] {name:16s} Top{topn} {wtype:8s} "
                    f"夏普={m.get('夏普比率'):.3f} 回撤={m.get('最大回撤')*100:.1f}% "
                    f"波动={m.get('年化波动率')*100:.1f}% 年化={m.get('年化收益率')*100:.1f}% "
                    f"({time.time()-t1:.0f}s)")

    res_df = pd.DataFrame(rows)
    res_df.to_csv(out_dir / f"qvlv2_{span}_{tag}.csv", index=False, encoding="utf-8-sig")
    print("\n" + "=" * 130)
    print(f"  优秀因子挖掘扫描 第2轮  {args.start}~{args.end}  目标: 夏普>=1.2 低回撤")
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
    print(f"已保存: {out_dir / f'qvlv2_{span}_{tag}.csv'}")


def json_w(w: dict) -> str:
    return " ".join(f"{k}{v:.0%}" for k, v in sorted(w.items()))


if __name__ == "__main__":
    main()
