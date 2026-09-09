# -*- coding: utf-8 -*-
"""
真实行情回放撮合（第 5 层执行层：无 QMT 时积累「真实市场结构」的样本）。

升级说明（v2，2026-09-06）：
    v1 用「已知真值」生成滑点 —— 回归只能证明链路能把设定值还原回来，
    不含任何真实市场信息，本质是自我循环。
    v2 改为【真实分钟行情回放】：
      委托价 = 当日开盘价
      订单   = 按参与率目标 × 当日真实成交额 × cap 计算金额
      撮合   = 沿当日真实 5 分钟 bar 序列吸收流动性（按金额比例吃 bar），
               直到满足订单金额；成交均价 = 被吸收部分的 VWAP。
      滑点   = (成交VWAP - 开盘价)/开盘价 —— 来自真实日内价格路径。
    边界（必须说清，别当实证用）：
      · 5 分钟 bar 是聚合数据，不含订单簿深度 —— 参与率 <5% 的订单首根
        bar 即满足，滑点退化为首根 bar VWAP。能观测到的是【真实调仓大单
        (5%~60% 档)沿日内路径的执行成本】，不是订单簿级的冲击系数。
      · 因此 v2 产出用于：① execution 层闭环验证（下单→撮合→落库→回归
        全链路）；② 标定真实市场滑点的量级/分布参考。
      · 参与率-冲击的实证系数仍必须等 QMT 模拟盘/实盘的真实成交回报
        （calibrate_impact 门控会继续拒绝把模拟样本当作实证回写）。

用法：
    python scripts/paper_trade_sim.py --n 300 --days 20   # 真实回放（默认）
    python scripts/paper_trade_sim.py --synthetic          # v1 链路自检（保留）
    跑完再执行 calibrate 回归（会标注 simulated，参考不回写）：
        python scripts/calibrate_impact.py --regress --save
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.common.logger import logger
from src.layer5_execution.broker import Order, Trade
from src.layer5_execution.trade_store import TradeStore

# 生成滑点用的「真值」（仅 --synthetic 链路自检模式使用）
TRUE_BASE_BPS = 2.0
TRUE_IMPACT = 1.0

# 新浪分钟接口：单次返回 ~1970 根 bar（1 分钟≈8 个交易日；5 分钟≈40 个交易日）
# 默认用 5 分钟，覆盖最近约 40 个交易日，与每日增量积累的节奏兼容。
_MIN_PERIOD = "5"


def _replay_one_day(bars: pd.DataFrame, target_participation: float,
                    cap: float = 0.10) -> dict | None:
    """在某一交易日的真实 5 分钟 bar 上回放一笔市价单。

    bars: 当日分钟数据（按时间升序），列含 open/high/low/close/volume/amount。
    返回成交摘要；流动性不足或数据异常返回 None。

    撮合规则（VWAP 路径吸收，按金额比例）：
      1. 委托价 = 当日第一根 bar 的开盘价（开盘市价单近似）
      2. 目标金额 = 参与率 × 当日真实成交额 × cap
      3. 沿时间吸收 bar 流动性：每根 bar 的可成交额 = bar.amount。
         · 若剩余目标 > bar.amount：整根成交，均价 = bar 的 VWAP(amount/volume)
         · 若剩余目标 <= bar.amount：只吃该 bar 的剩余部分，成交价 = 该 bar VWAP
      4. 成交均价 = Σ(VWAP × 吸收量) / Σ(吸收量)  —— 小单只落在开盘附近，
         大单覆盖更多 bar、均价向日内漂移，参与率-滑点关系由真实价格路径决定
      5. participation 用实际吸收金额 / (当日成交额 × cap)（与 impact.py 口径一致）
    """
    if bars is None or bars.empty:
        return None
    d = bars.sort_values("day").reset_index(drop=True)
    try:
        d["amount"] = pd.to_numeric(d["amount"], errors="coerce").fillna(0)
        d["volume"] = pd.to_numeric(d["volume"], errors="coerce").fillna(0)
        d["open"] = pd.to_numeric(d["open"], errors="coerce")
    except Exception:                                       # noqa: BLE001
        return None

    open_px = float(d["open"].iloc[0])
    if open_px <= 0 or open_px != open_px:
        return None
    daily_amount = float(d["amount"].sum())
    if daily_amount <= 0:
        return None

    target = target_participation * daily_amount * cap
    if target <= 0:
        return None

    # 沿 bar 按金额比例吸收：bar 均价 = amount/volume（真实 VWAP）
    remaining = target
    cum_value = 0.0            # 已吸收金额
    cum_vol = 0.0              # 已吸收股数
    for _, r in d.iterrows():
        bar_amount = float(r["amount"])
        bar_vol = float(r["volume"])
        if bar_amount <= 0 or bar_vol <= 0:
            continue
        vwap = bar_amount / bar_vol
        take = min(remaining, bar_amount)      # 本 bar 吸收金额（不超剩余目标）
        vol_take = take / vwap
        cum_value += take
        cum_vol += vol_take
        remaining -= take
        if remaining <= 0:
            break

    if cum_vol <= 0:
        return None

    deal_vwap = cum_value / cum_vol            # 吸收部分的均价（VWAP）
    actual_participation = cum_value / (daily_amount * cap)
    if deal_vwap <= 0:
        return None

    # 买入单：成交价 > 委托价 = 正滑点（成本）；卖出单反向由调用方处理
    slip_bps = (deal_vwap - open_px) / open_px * 10000
    return {
        "order_price": open_px, "deal_price": deal_vwap,
        "volume": int(cum_vol),
        "deal_amount": cum_value,
        "participation": min(actual_participation, 1.0),
        "slip_bps": float(slip_bps),
        "trade_date": str(d["day"].iloc[0])[:10],
    }


def replay(n_trades: int, days: int, rng: np.random.Generator,
           cap: float = 0.10) -> list[dict]:
    """从新浪真实分钟行情回放 n_trades 笔订单。

    覆盖最近 days 个交易日（5 分钟线最多约 40 天）。逐只股票拉分钟数据，
    模拟真实调仓节奏：随机选股票 → 随机目标参与率 → 随机交易日。
    """
    import akshare as ak

    # 候选股票池：沪深主板/创业板样本（避免北交所分钟接口不稳）
    codes = ["sh600000", "sh600036", "sh601318", "sh600519", "sh601988",
             "sz000001", "sz000002", "sz000333", "sz000651", "sz000858",
             "sh600030", "sh600276", "sh601166", "sh601857", "sh603288",
             "sz002415", "sz002594", "sz300059", "sz300750", "sz002714"]
    results: list[dict] = []
    attempts = 0
    max_attempts = n_trades * 8                       # 留足重试空间

    while len(results) < n_trades and attempts < max_attempts:
        attempts += 1
        code = codes[int(rng.integers(0, len(codes)))]
        try:
            bars = ak.stock_zh_a_minute(symbol=code, period=_MIN_PERIOD, adjust="")
        except Exception as e:                          # noqa: BLE001
            logger.debug(f"{code} 分钟行情不可用: {type(e).__name__}: {e}")
            continue
        if bars is None or bars.empty:
            continue

        # 随机取一个交易日的 bar（最近 days 个交易日里）
        bars["d"] = pd.to_datetime(bars["day"]).dt.date
        days_avail = sorted(bars["d"].unique())
        if not days_avail:
            continue
        pool = [d_ for d_ in days_avail
                if d_ >= days_avail[-1] - timedelta(days=days * 2)]
        if not pool:
            pool = days_avail
        day_sel = pool[int(rng.integers(0, len(pool)))]
        day_bars = bars[bars["d"] == day_sel]

        # 参与率采样：必须让订单吃到多根 bar，才能观测到跨 bar 价格路径。
        #   5 分钟 bar 单根约占全天成交 1~3%，参与率 <5% 的订单第一根 bar
        #   就满足，滑点退化为首根 bar VWAP（与参与率脱钩）。
        #   故采样 5%~60% 档（对应真实调仓大单），覆盖 2~40 根 bar 的路径。
        participation = float(np.exp(rng.uniform(np.log(0.05), np.log(0.6))))
        rec = _replay_one_day(day_bars, participation, cap=cap)
        if rec is None:
            continue
        rec["ts_code"] = code[3:]
        rec["side"] = "buy" if rng.random() < 0.5 else "sell"
        if rec["side"] == "sell":
            rec["slip_bps"] = -rec["slip_bps"]
        rec["target_participation"] = participation
        results.append(rec)

    return results


def replay_synthetic(n_trades: int, rng: np.random.Generator,
                     cap: float = 0.10) -> list[dict]:
    """v1 链路自检：用已知真值造样本，验证回归能还原设定值（保留旧行为）。"""
    import pandas as pd
    from src.common.db import read_sql

    codes = read_sql(
        "SELECT ts_code FROM stock_basic WHERE is_delisted = 0 "
        "ORDER BY RAND() LIMIT 50"
    )["ts_code"].tolist()
    px = read_sql(
        """SELECT trade_date, ts_code, open, amount FROM daily_price
           WHERE adj_type = 'none' AND trade_date >= '2026-07-01'
             AND ts_code IN ({})""".format(
            ",".join(f":c{i}" for i in range(len(codes)))),
        {f"c{i}": c for i, c in enumerate(codes)},
    )
    if px.empty:
        return []
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.date

    out: list[dict] = []
    for _ in range(n_trades):
        row = px.sample(1, random_state=None).iloc[0]
        open_px = float(row["open"])
        amount = float(row["amount"]) if pd.notna(row["amount"]) else 0.0
        if amount <= 0 or open_px <= 0:
            continue
        participation = float(rng.uniform(0.001, 0.05))
        order_amount = participation * amount * cap
        volume = int(order_amount / open_px / 100) * 100
        if volume < 100:
            continue
        slip_bps = TRUE_BASE_BPS + TRUE_IMPACT * np.sqrt(participation) * 100
        slip_bps += rng.normal(0, 3)
        side = "buy" if rng.random() < 0.5 else "sell"
        if side == "buy":
            deal_price = open_px * (1 + slip_bps / 1e4)
        else:
            deal_price = open_px * (1 - slip_bps / 1e4)
        out.append({
            "ts_code": row["ts_code"], "side": side,
            "order_price": open_px, "deal_price": round(float(deal_price), 3),
            "volume": volume,
            "deal_amount": round(float(deal_price) * volume, 2),
            "participation": participation, "slip_bps": float(slip_bps),
            "trade_date": str(row["trade_date"]),
            "target_participation": participation,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="模拟撮合（v2=真实分钟行情回放；--synthetic=v1 链路自检）")
    ap.add_argument("--n", type=int, default=300, help="模拟委托笔数")
    ap.add_argument("--days", type=int, default=20,
                    help="真实回放覆盖最近 N 个交易日（5 分钟线上限约 40）")
    ap.add_argument("--synthetic", action="store_true", help="v1 合成真值自检模式")
    args = ap.parse_args()

    rng = np.random.default_rng(7)
    cap = 0.10    # 参与率分母（与 impact.py / costs.yaml 一致）

    if args.synthetic:
        trades = replay_synthetic(args.n, rng, cap=cap)
        mode_note = "synthetic（v1 链路自检，回归应还原真值 impact=1.0）"
    else:
        trades = replay(args.n, args.days, rng, cap=cap)
        mode_note = f"真实分钟行情回放（最近 {args.days} 个交易日）"

    if not trades:
        logger.error("未生成任何成交样本 —— 检查新浪分钟接口可用性 / 股票池")
        return 1

    store = TradeStore(broker="paper")
    done = 0
    for rec in trades:
        side = rec["side"]
        o = Order(ts_code=rec["ts_code"], side=side,
                  volume=int(rec["volume"]),
                  price=round(rec["order_price"], 3),
                  strategy_tag="paper_replay_v2")
        oid = store.save_order(o)
        t = Trade(order_id=oid, ts_code=rec["ts_code"], side=side,
                  order_price=round(rec["order_price"], 3),
                  deal_price=round(rec["deal_price"], 3),
                  volume=int(rec["volume"]),
                  deal_time=datetime.combine(
                      datetime.strptime(rec["trade_date"], "%Y-%m-%d").date(),
                      datetime.min.time()))
        store.save_trade(t)
        # 推进委托单状态（2026-09-09，第三轮审计 M2）：此前模拟落库只写
        # submitted，DB 实证 300/300 全卡住 —— 全部历史单都是"未终结"候选，
        # 污染 _match_order_id 启发式，模拟与实盘混用时成交错配。
        store.advance_order_on_fill(oid)
        done += 1

    slip = pd.Series([r["slip_bps"] for r in trades])
    part = pd.Series([r["participation"] for r in trades])
    logger.info(
        f"模拟成交 {done} 笔（{mode_note}）："
        f"滑点中位 {slip.median():.2f} bps / P95 {slip.quantile(0.95):.2f} bps，"
        f"参与率中位 {part.median():.4f}"
    )
    print(f"\n回放完成。现在运行: python scripts/calibrate_impact.py "
          f"--regress --save\n（样本来源=simulated，结论仅供参考，不回写）\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
