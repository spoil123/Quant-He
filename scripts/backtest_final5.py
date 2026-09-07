# -*- coding: utf-8 -*-
"""最终交付: 5 个达标因子(多头夏普>=1.2低回撤)统一本地回测。
   面板只构建一次, 5 因子逐个回测, 输出:
   - 每因子完整绩效(年化/夏普/索提诺/波动/回撤/超额/IR/换手)
   - 分年指标(2023/2024/2025 收益/夏普/回撤)
   - 5 因子月收益相关性矩阵
   - CSV + HTML 报告
用法: python scripts/backtest_final5.py [--start .. --end ..]
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

# ---- 5 个达标因子 (名称, 权重, topn, 加权, 单票上限) ----
FACTORS = [
    {"name": "QV-Mom40", "w": {"mom": 0.15, "roe": 0.18, "ep": 0.12, "bm": 0.15,
                               "lvol": 0.20, "tur": 0.10, "illiq": 0.10},
     "topn": 40, "wtype": "float_mv", "mw": 0.06,
     "desc": "动量+质量+价值 (Qlib Alpha158 / QMJ)"},
    {"name": "MD-Mom50", "w": {"mom": 0.18, "dy": 0.18, "bm": 0.15, "ep": 0.12,
                               "lvol": 0.17, "roe": 0.10, "tur": 0.10},
     "topn": 50, "wtype": "float_mv", "mw": 0.05,
     "desc": "动量+红利 (全场换手最低)"},
    {"name": "DV-LV50", "w": {"dy": 0.20, "bm": 0.20, "ep": 0.15, "lvol": 0.25,
                              "tur": 0.10, "size": 0.10},
     "topn": 50, "wtype": "float_mv", "mw": 0.05,
     "desc": "红利低波 (全场回撤最低)"},
    # NEW1 / NEW2 由 scan_qvlv2 扫描结果填入
    {"name": "QV2-Mom60", "w": {"mom": 0.20, "roe": 0.15, "ep": 0.10, "bm": 0.12,
                                "lvol": 0.23, "tur": 0.10, "illiq": 0.10},
     "topn": 60, "wtype": "float_mv", "mw": 0.04,
     "desc": "动量质量2号 (动量权重加大, Top60摊薄个股风险)"},
    {"name": "GM-LV50", "w": {"gm": 0.15, "roe": 0.12, "ep": 0.10, "lvol": 0.28,
                              "dy": 0.15, "bm": 0.10, "tur": 0.10},
     "topn": 50, "wtype": "float_mv", "mw": 0.05,
     "desc": "质量低波红利 (毛利率主导, 无动量, 与动量系互补)"},
]

COMP_COLS = {
    "illiq": "_r_illiq", "tur": "_r_tur", "size": "_r_size", "bm": "_r_bm",
    "ep": "_r_ep", "dy": "_r_dy", "roe": "_r_roe", "gm": "_r_gm",
    "lev": "_r_lev", "lvol": "_r_lvol", "rev20": "_r_rev20", "mom": "_r_mom",
}


def build_panel(start: str, end: str) -> pd.DataFrame:
    ps = (pd.Timestamp(start) - pd.Timedelta(days=460)).strftime("%Y-%m-%d")
    panel = build_factor_panel(ps, end, with_financial=True,
                               with_industry=False, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    panel = attach_raw_close(panel)
    dv = read_sql(
        """SELECT trade_date, ts_code, dv_ttm FROM daily_basic
           WHERE trade_date BETWEEN :s AND :e""", {"s": ps, "e": end})
    dv["trade_date"] = pd.to_datetime(dv["trade_date"]).dt.date
    dv["dv_ttm"] = pd.to_numeric(dv["dv_ttm"], errors="coerce").astype("float32")
    panel = panel.merge(dv, on=["trade_date", "ts_code"], how="left")

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
    return p[p["trade_date"] >= pd.Timestamp(start).date()].copy()


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


def yearly_stats(daily_ret: pd.Series) -> dict:
    """分年: 收益/夏普/回撤"""
    eq = (1 + daily_ret.fillna(0)).cumprod()
    out = {}
    for y in sorted({d.year for d in daily_ret.index}):
        r = daily_ret[daily_ret.index.year == y].dropna()
        if len(r) < 10:
            continue
        e = eq[eq.index.year == y]
        e = e / e.iloc[0]
        dd = (e / e.cummax() - 1).min()
        total = e.iloc[-1] - 1.0
        sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
        out[y] = (total, sharpe, -dd)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2025-12-31")
    args = ap.parse_args()

    out_dir = ROOT / "outputs" / "final5"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    scfg = get_config("strategy")
    bt_cfg = scfg.get("backtest", {}) or {}

    t0 = time.time()
    logger.info("构建面板(一次)...")
    p = build_panel(args.start, args.end)
    logger.info(f"面板 {len(p):,} 行, 耗时 {time.time()-t0:.0f}s")

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=args.start, end=args.end)
    bench = load_benchmark(args.start, args.end)
    bt = PortfolioBacktester(
        initial_capital=float(bt_cfg.get("initial_cash", 10_000_000)),
        cost_tier="conservative")

    factors = [f for f in FACTORS if f["w"]]
    monthly = {}
    rows = []
    year_rows = []
    for i, f in enumerate(factors, 1):
        wnorm = {k: v / sum(f["w"].values()) for k, v in f["w"].items()}
        val = pd.Series(0.0, index=p.index)
        for k, col in COMP_COLS.items():
            if k in wnorm:
                val += wnorm[k] * p[col].fillna(0.5)
        p["_score"] = val
        weights = bt.build_weights(p, rb, score_col="_score", top_n=f["topn"],
                                   weighting=f["wtype"], max_weight=f["mw"],
                                   min_weight=0.001)
        if weights.empty:
            logger.warning(f"{f['name']}: 权重空, 跳过")
            continue
        res = bt.run(p, weights, start_date=args.start, benchmark=bench)
        m = res["metrics"]
        dr = res["daily_returns"]
        dr.index = pd.to_datetime(dr.index)
        monthly[f["name"]] = (1 + dr.fillna(0)).resample("ME").prod() - 1
        ys = yearly_stats(dr)
        rows.append({
            "因子": f["name"], "结构": f"Top{f['topn']}/{f['wtype']}/上限{f['mw']:.0%}",
            "权重": " ".join(f"{k}{v:.0%}" for k, v in sorted(wnorm.items(), key=lambda x: -x[1])),
            "累计收益": m.get("总收益率"), "年化收益": m.get("年化收益率"),
            "夏普": m.get("夏普比率"), "索提诺": m.get("索提诺比率"),
            "年化波动": m.get("年化波动率"), "最大回撤": m.get("最大回撤"),
            "超额年化": m.get("超额年化收益率"), "信息比率": m.get("信息比率"),
            "换手率": m.get("年化换手率"), "平均持仓": m.get("平均持仓数"),
        })
        for y, (tot, sharpe, mdd) in ys.items():
            year_rows.append({"因子": f["name"], "年份": y,
                              "收益": tot, "夏普": sharpe, "回撤": mdd})
        logger.info(f"[{i}/{len(factors)}] {f['name']}: 夏普={m.get('夏普比率'):.3f} "
                    f"回撤={m.get('最大回撤')*100:.2f}% 年化={m.get('年化收益率')*100:.2f}%")

    res_df = pd.DataFrame(rows)
    yr_df = pd.DataFrame(year_rows)
    res_df.to_csv(out_dir / f"final5_metrics_{tag}.csv", index=False, encoding="utf-8-sig")
    yr_df.to_csv(out_dir / f"final5_yearly_{tag}.csv", index=False, encoding="utf-8-sig")

    corr = pd.DataFrame(monthly).corr().round(3)
    corr.to_csv(out_dir / f"final5_corr_{tag}.csv", encoding="utf-8-sig")

    print("\n" + "=" * 120)
    print(f"  最终5因子统一回测  {args.start}~{args.end}  (全A剔ST·月度调仓·真实成本·基准沪深300)")
    print("=" * 120)
    show = res_df.copy()
    for c in ("累计收益", "年化收益", "年化波动", "最大回撤", "超额年化", "换手率"):
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 40)
    print(show.drop(columns=["权重"]).to_string(index=False))
    print("\n分年:")
    print(yr_df.pivot_table(index="因子", columns="年份",
                            values=["收益", "夏普", "回撤"]).round(3).to_string())
    print("\n月收益相关性:")
    print(corr.to_string())
    print("=" * 120)
    print(f"已保存: {out_dir}\\final5_*_{tag}.csv")


if __name__ == "__main__":
    main()
