# -*- coding: utf-8 -*-
"""风格归因: α 与风格 β 拆开。
1. 从面板自建风格多空组合(月度调仓5分组等权): MKT/SMB/VMG(红利)/LVOL(低波)/MOM
2. Combo3 周收益对5风格回归 → alpha(年化)/t值/β/R²
3. 对自建"红利低波风格基准"(dy+lvol 头部20% 流通市值加权)对比 + 回归
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from src.common.config import get_config                  # noqa: E402
from src.common.logger import logger                       # noqa: E402
from src.layer3_strategy.portfolio_backtest import (       # noqa: E402
    PortfolioBacktester, monthly_rebalance_dates,
)

TRADING_DAYS = 252
START, END = "2021-01-01", "2025-12-31"


def main() -> None:
    t0 = time.time()
    p = pd.read_pickle(ROOT / "outputs" / "final5" / "panel_cache_2125.pkl")
    dr = pd.read_csv(ROOT / "outputs" / "final5" / "daily_returns_2125.csv",
                     index_col=0, parse_dates=True)["Combo3"]

    trade_dates = sorted(p["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=START, end=END)
    rb_set = set(rb)

    # 日收益矩阵
    p2 = p.sort_values(["ts_code", "trade_date"])
    p2["_ret1"] = p2.groupby("ts_code", sort=False)["close"].pct_change()
    ret_m = p2.pivot_table(index="trade_date", columns="ts_code", values="_ret1")

    comp_map = {"SMB": ("_r_size", True),    # 小市值做多
                "LVOL": ("_r_lvol", True),    # 低波动做多
                "MOM": ("_r_mom", True)}      # 高动量做多

    style_ls = {}
    for name, (col, asc) in comp_map.items():
        sig = p[p["trade_date"].isin(rb_set)][["trade_date", "ts_code", col]].copy()
        def _g5(x: pd.Series) -> pd.Series:
            out = pd.Series(np.nan, index=x.index)
            m = x.notna()
            pct = x[m].rank(method="first", pct=True)
            out[m] = np.minimum(5, np.ceil(pct * 5))
            return out
        sig["group"] = sig.groupby("trade_date")[col].transform(_g5)
        sig = sig.rename(columns={"trade_date": "sig_date"})
        gm0 = sig.pivot_table(index="sig_date", columns="ts_code", values="group")
        gm = gm0.reindex(ret_m.index).ffill().reindex(columns=ret_m.columns)
        ls = (ret_m.where(gm == 5).mean(axis=1)
              - ret_m.where(gm == 1).mean(axis=1)).dropna()
        ls.index = pd.to_datetime(ls.index)
        style_ls[name] = ls
        logger.info(f"风格组合 {name} 完成 ({time.time()-t0:.0f}s)")

    mkt = ret_m.mean(axis=1).dropna()
    mkt.index = pd.to_datetime(mkt.index)
    style_ls["MKT"] = mkt

    # ---- 自建"红利低波风格基准": 0.5*dy + 0.5*lvol, 头部20% 流通市值加权 ----
    # (向量化构造: 调仓日按 float_mv 加权, 区间内持有, 成本前近似)
    sig = p[p["trade_date"].isin(rb_set)][["trade_date", "ts_code", "_r_dy", "_r_lvol",
                                           "float_mv"]].copy()
    sig["_s"] = 0.5 * sig["_r_dy"].fillna(0.5) + 0.5 * sig["_r_lvol"].fillna(0.5)
    def _top20(x: pd.Series) -> pd.Series:
        out = pd.Series(False, index=x.index)
        m = x.notna()
        pct = x[m].rank(method="first", pct=True)
        out[m] = pct >= 0.8
        return out
    sig["_in"] = sig.groupby("trade_date")["_s"].transform(_top20)
    sig = sig[sig["_in"]]
    sig = sig.rename(columns={"trade_date": "sig_date"})
    w0 = sig.pivot_table(index="sig_date", columns="ts_code", values="float_mv")
    w0 = w0.div(w0.sum(axis=1), axis=0)
    wm = w0.reindex(ret_m.index).ffill().reindex(columns=ret_m.columns).fillna(0.0)
    dv_style_dr = (wm * ret_m).sum(axis=1).dropna()
    dv_style_dr.index = pd.to_datetime(dv_style_dr.index)
    eqs = (1 + dv_style_dr).cumprod()
    yrs = len(dv_style_dr) / TRADING_DAYS
    logger.info(f"红利低波风格基准(近似): 年化={eqs.iloc[-1]**(1/yrs)-1:.2%} "
                f"夏普={dv_style_dr.mean()/dv_style_dr.std()*np.sqrt(252):.3f} "
                f"回撤={-(eqs/eqs.cummax()-1).min():.2%}")

    # ---- 周度对齐 ----
    def weekly(s: pd.Series) -> pd.Series:
        return (1 + s.dropna()).resample("W-FRI").prod() - 1

    Y = weekly(dr)
    X = pd.DataFrame({k: weekly(v) for k, v in style_ls.items()})
    df = pd.concat([Y.rename("combo3"), X], axis=1).dropna()
    logger.info(f"周度样本 {len(df)} 个")

    # ---- 回归 1: Combo3 ~ MKT + SMB + VMG + LVOL + MOM ----
    X1 = sm.add_constant(df[["MKT", "SMB", "LVOL", "MOM"]])
    r1 = sm.OLS(df["combo3"], X1).fit()
    alpha_w = r1.params["const"]
    print("\n" + "=" * 100)
    print("  风格归因回归 1: Combo3周收益 ~ MKT+SMB+LVOL(低波)+MOM")
    print("=" * 100)
    out1 = pd.DataFrame({"β": r1.params, "t值": r1.tvalues, "p值": r1.pvalues})
    print(out1.round(3).to_string())
    print(f"  R²={r1.rsquared:.3f}  alpha周={alpha_w*100:.3f}%  alpha年化={(alpha_w*52)*100:.2f}%  "
          f"alpha t={r1.tvalues['const']:.2f}")

    # ---- 回归 2: Combo3 ~ 红利低波风格基准 (单因子基准) ----
    df2 = pd.concat([Y.rename("combo3"), weekly(dv_style_dr).rename("DV_style")],
                    axis=1).dropna()
    X2 = sm.add_constant(df2[["DV_style"]])
    r2 = sm.OLS(df2["combo3"], X2).fit()
    print("\n" + "=" * 100)
    print("  风格归因回归 2: Combo3周收益 ~ 自建红利低波风格基准(Top20%流通市值加权,成本前近似)")
    print("=" * 100)
    out2 = pd.DataFrame({"β": r2.params, "t值": r2.tvalues, "p值": r2.pvalues})
    print(out2.round(3).to_string())
    print(f"  R²={r2.rsquared:.3f}  alpha年化={(r2.params['const']*52)*100:.2f}%  "
          f"alpha t={r2.tvalues['const']:.2f}")

    # ---- Combo3 vs 风格基准 直接对比 ----
    print("\n" + "=" * 100)
    print("  Combo3 vs 自建红利低波风格基准 (2021-2025, Combo3含真实成本,基准为成本前近似)")
    print("=" * 100)
    for label, s_ in (("Combo3", dr), ("红利低波基准", dv_style_dr)):
        eq = (1 + s_.fillna(0)).cumprod()
        years = len(s_) / TRADING_DAYS
        print(f"  {label:10s} 年化={eq.iloc[-1]**(1/years)*100-100:6.2f}%  "
              f"夏普={s_.mean()/s_.std()*np.sqrt(252):.3f}  "
              f"回撤={-(eq/eq.cummax()-1).min()*100:6.2f}%  "
              f"波动={s_.std()*np.sqrt(252)*100:6.2f}%")
    print("=" * 100)

    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out1.round(4).to_csv(ROOT / "outputs" / "final5" / f"attrib_reg1_{tag}.csv",
                         encoding="utf-8-sig")
    out2.round(4).to_csv(ROOT / "outputs" / "final5" / f"attrib_reg2_{tag}.csv",
                         encoding="utf-8-sig")


if __name__ == "__main__":
    main()
