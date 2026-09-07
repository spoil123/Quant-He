# -*- coding: utf-8 -*-
"""
第 2 层验收示例：双均线策略（MA5/MA20）+ Backtester + 手动验算。

三步走：
    1. 从数据库读浦发银行(600000) 前复权日线（2023 起，库内样本区间）
    2. 双均线金叉/死叉生成 0/1 信号，跑向量化回测
    3. 手动验算：抽 3 个调仓日，用撮合公式手算当日收益，与引擎输出逐笔比对
    4. 输出净值曲线图

验收标准：手算值与引擎输出一致（浮点误差 < 1e-6），
          年化/回撤按指标定义复算能对上。

用法：python scripts/example_dual_ma.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from src.common.db import read_sql
from src.layer2_backtest.backtester import Backtester, STAMP_DUTY_SCHEDULE
from src.layer2_backtest.metrics import annual_return, max_drawdown

CODE = "600000"
START = "2023-01-01"
FAST, SLOW = 5, 20
SLIP = 10.0 / 1e4                     # 滑点 0.1%


def load_data() -> pd.DataFrame:
    df = read_sql("""
        SELECT trade_date, open, high, low, close, volume,
               is_suspended, is_limit_up, is_limit_down
        FROM daily_price
        WHERE ts_code = :c AND adj_type = 'qfq' AND trade_date >= :s
        ORDER BY trade_date
    """, {"c": CODE, "s": START})
    df = df.rename(columns={"trade_date": "date"})
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def dual_ma_signal(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """金叉=持仓(1)，死叉=空仓(0)。均线未成型期（前 slow 天）视为空仓。"""
    ma_f, ma_s = close.rolling(fast).mean(), close.rolling(slow).mean()
    sig = (ma_f > ma_s).astype(float)
    return sig.fillna(0.0)


def stamp_duty_for(d) -> float:
    dd = d.date() if hasattr(d, "date") else d
    for s, e, r in STAMP_DUTY_SCHEDULE:
        if s <= dd <= e:
            return r
    return 0.001


def manual_verify(result: dict, data: pd.DataFrame, bt: Backtester, n: int = 3) -> None:
    """抽 n 个调仓日，按撮合公式手算当日收益，与引擎输出逐笔比对。"""
    pos = result["position"]
    daily = result["daily_returns"]
    diff = pos.diff()

    close = data["close"].astype(float)
    open_ = data["open"].astype(float)
    close_prev = close.shift(1)
    pos_prev = pos.shift(1)

    rb_days = diff.index[diff.abs() > 1e-10]
    checked = 0
    print("\n" + "=" * 78)
    print("手动验算：按撮合公式复算调仓日收益，与引擎对比")
    print("=" * 78)
    for t in rb_days:
        if checked >= n:
            break
        d = float(diff.loc[t])
        # 成交价 = 开盘价 ± 滑点
        exec_price = float(open_.loc[t]) * (1 + SLIP) if d > 0 else float(open_.loc[t]) * (1 - SLIP)
        # 费率：买入=佣金+过户费，卖出=佣金+过户费+印花税
        fee = bt.commission + bt.transfer_fee + (stamp_duty_for(t) if d < 0 else 0.0)

        p_prev = float(pos_prev.loc[t])
        p_now = float(pos.loc[t])
        c_prev = float(close_prev.loc[t])
        c_now = float(close.loc[t])

        # 手算（与引擎 ret_rb 同一公式）
        manual = (
            p_prev * (exec_price / c_prev - 1.0)
            + p_now * (c_now / exec_price - 1.0)
            - abs(d) * fee
        )
        engine = float(daily.loc[t])
        err = abs(manual - engine)
        side = "买入" if d > 0 else "卖出"
        status = "✅ 一致" if err < 1e-6 else "❌ 不一致"
        print(
            f"  {pd.Timestamp(t).date()}  {side}  Δw={d:+.1f}  "
            f"成交价={exec_price:.4f}  手算={manual:+.6%}  引擎={engine:+.6%}  "
            f"差={err:.2e}  {status}"
        )
        checked += 1

    if checked == 0:
        print("  没有发生调仓，跳过")


def verify_metrics(result: dict, equity: pd.Series) -> None:
    """复算年化与最大回撤，验证指标口径。"""
    print("\n" + "=" * 78)
    print("指标复算：年化收益率 / 最大回撤")
    print("=" * 78)

    # 年化（几何法，A 股 252 交易日口径，与 metrics.py 一致）
    years = len(equity) / 252.0
    manual_ann = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0)
    engine_ann = result["metrics"]["年化收益率"]
    print(f"  年化: 手算={manual_ann:.6%}  引擎={engine_ann:.6%}  "
          f"差={abs(manual_ann - engine_ann):.2e}  "
          f"{'✅ 一致' if abs(manual_ann - engine_ann) < 1e-9 else '❌'}")

    # 最大回撤
    peak = equity.cummax()
    dd = equity / peak - 1.0
    manual_mdd = float(-dd.min())
    engine_mdd = result["metrics"]["最大回撤"]
    print(f"  回撤: 手算={manual_mdd:.6%}  引擎={engine_mdd:.6%}  "
          f"差={abs(manual_mdd - engine_mdd):.2e}  "
          f"{'✅ 一致' if abs(manual_mdd - engine_mdd) < 1e-9 else '❌'}")


def main() -> None:
    data = load_data()
    print(f"数据: {CODE}  前复权  {data.index[0].date()} ~ {data.index[-1].date()}  "
          f"{len(data)} 个交易日")

    # 双均线信号
    sig = dual_ma_signal(data["close"], FAST, SLOW)
    n_signal = int((sig == 1).sum())
    print(f"信号: 双均线 MA{FAST}/MA{SLOW}，持仓日 {n_signal}/{len(sig)}")

    bt = Backtester(data, initial_capital=1_000_000,
                    commission=0.0003, transfer_fee=0.00001,
                    slip_bps=10.0)
    result = bt.run(sig)
    bt._last_run = result

    print("\n" + "=" * 78)
    print(f"双均线(MA{FAST}/MA{SLOW}) 回测结果  {CODE}")
    print("=" * 78)
    for k, v in result["metrics"].items():
        if isinstance(v, float):
            if k in ("总收益率", "年化收益率", "年化波动率", "最大回撤"):
                print(f"  {k:12s} {v:>10.2%}")
            else:
                print(f"  {k:12s} {v:>10.4f}")
        else:
            print(f"  {k:12s} {v}")

    # 手动验算
    manual_verify(result, data, bt)
    verify_metrics(result, result["equity_curve"])

    # 成交明细
    print("\n成交明细:")
    if len(result["trades"]):
        print(result["trades"].to_string(index=False))
    else:
        print("  无成交")

    # 净值曲线（基准 = 买入持有）
    bh = data["close"] / data["close"].iloc[0]
    bt.plot(benchmark=bh, title=f"双均线(MA{FAST}/{SLOW})  {CODE} 净值曲线",
            save_path=PROJECT_ROOT / "outputs" / "figures" / "example_dual_ma.png")
    print(f"\n净值曲线已保存: outputs/figures/example_dual_ma.png")


if __name__ == "__main__":
    main()
