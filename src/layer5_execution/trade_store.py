# -*- coding: utf-8 -*-
"""
成交记录落库（第 5 层执行层）。

职责：
    - 委托单/成交回报写入 execution_order / execution_trade 两张表
    - 供 impact.py 读取成交记录做回归（委托价 vs 成交价）

与 repository.py 的分工：
    repository 管行情/财务等「市场数据」；
    TradeStore 只管「自己的交易记录」—— 数据性质不同，不混在一张表里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from src.common.db import execute, read_sql
from src.common.logger import logger
from src.layer5_execution.broker import Order, Trade


class TradeStore:
    """交易记录存取。所有方法独立可用，不依赖具体 Broker。"""

    def __init__(self, broker: str = "qmt"):
        self.broker = broker

    # ================================================================ 写入

    def save_order(self, order: Order) -> int:
        """保存委托单，返回主键 id。

        H3 修复：用 INSERT 的 lastrowid 取回自增主键 —— 旧实现
        「SELECT MAX(id)」在另一连接执行，QMT 回调线程并发时会取到
        别的线程刚插入的行，导致 order_id 串线错配。
        """
        sql = """
            INSERT INTO execution_order
                (broker, ts_code, side, order_type, price, volume,
                 status, xt_order_id, strategy_tag, created_at)
            VALUES
                (:broker, :code, :side, :otype, :price, :vol,
                 :status, :xid, :tag, :ct)
        """
        params = {
            "broker": self.broker, "code": order.ts_code, "side": order.side,
            "otype": order.order_type, "price": order.price, "vol": order.volume,
            "status": order.status, "xid": order.xt_order_id,
            "tag": order.strategy_tag, "ct": order.created_at,
        }
        _, oid = execute(sql, params, need_lastrowid=True)
        return int(oid or 0)

    def save_trade(self, trade: Trade) -> int:
        """保存成交回报，返回主键 id（lastrowid，防并发串线）。"""
        sql = """
            INSERT INTO execution_trade
                (order_id, broker, ts_code, side, order_price, deal_price,
                 volume, deal_amount, deal_time)
            VALUES
                (:oid, :broker, :code, :side, :op, :dp, :vol, :amt, :dt)
        """
        params = {
            "oid": trade.order_id, "broker": self.broker, "code": trade.ts_code,
            "side": trade.side, "op": trade.order_price, "dp": trade.deal_price,
            "vol": trade.volume,
            "amt": (trade.deal_price * trade.volume) if trade.deal_price else None,
            "dt": trade.deal_time,
        }
        _, tid = execute(sql, params, need_lastrowid=True)
        return int(tid or 0)

    def update_order_status(self, order_id: int, status: str) -> None:
        execute(
            "UPDATE execution_order SET status = :s WHERE id = :id",
            {"s": status, "id": order_id},
        )

    def filled_volume(self, order_id: int) -> int:
        """该委托单累计已成交量（分笔成交会累加）。"""
        df = read_sql(
            "SELECT COALESCE(SUM(volume), 0) AS v FROM execution_trade "
            "WHERE order_id = :oid",
            {"oid": order_id},
        )
        if df.empty:
            return 0
        return int(pd.to_numeric(df.iloc[0]["v"], errors="coerce") or 0)

    def advance_order_on_fill(self, order_id: int) -> str:
        """成交回报后推进委托单状态：submitted -> partial -> filled。

        H3 修复（2026-09-07）：成交回调原先只 save_trade、从不更新委托单状态，
        订单成交后永久停在 submitted。两个后果：

          1. 状态机断裂 —— 无法区分「已报未成交 / 部分成交 / 全部成交」，
             对账、去重、幂等全部失效；
          2. 错配 —— _match_order_id 按 status="submitted" 筛候选，既然状态
             永不变化，全部历史委托都留在候选池里，新成交会被关联到任意
             同票同向的旧委托单上。

        判定用累计成交量，天然支持分笔成交：
          累计 == 0 -> 维持 submitted；0 < 累计 < 委托量 -> partial；
          累计 >= 委托量 -> filled。
        已终结状态（filled/canceled/rejected）不回退，避免撤单后又被成交回报
        改成 filled。
        """
        if not order_id:
            return ""
        row = read_sql(
            "SELECT volume, status FROM execution_order WHERE id = :id",
            {"id": order_id},
        )
        if row.empty:
            return ""
        vol = pd.to_numeric(row.iloc[0]["volume"], errors="coerce")
        cur = str(row.iloc[0]["status"] or "")
        if cur in ("filled", "canceled", "rejected"):
            return cur                                   # 已终结，不回退

        filled = self.filled_volume(order_id)
        if vol is not None and vol > 0 and filled >= float(vol):
            new = "filled"
        elif filled > 0:
            new = "partial"
        else:
            new = cur or "submitted"

        if new != cur:
            self.update_order_status(order_id, new)
            logger.info(f"委托单 #{order_id} 状态 {cur or '空'} -> {new}"
                        f"（累计成交 {filled}/{int(vol) if vol else 0} 股）")
        return new

    # ================================================================ 查询

    def load_trades(self, start: str | None = None, end: str | None = None,
                    min_volume: int = 0) -> pd.DataFrame:
        """读取成交回报（impact 回归的原材料）。

        返回列：trade_date / ts_code / side / order_price / deal_price /
                volume / deal_amount / order_id / broker

        broker 是样本来源标识（'paper'=模拟撮合，其他/空=真实券商回报），
        impact 回归据此判断结论能否回写 costs.yaml（模拟样本不得冒充实证值）。
        """
        sql = """
            SELECT t.ts_code, t.side, t.order_price, t.deal_price,
                   t.volume, t.deal_amount, t.deal_time,
                   DATE(t.deal_time) AS trade_date,
                   t.order_id, t.broker
            FROM execution_trade t
            WHERE 1=1
        """
        params: Dict = {}
        if start:
            sql += " AND t.deal_time >= :s"
            params["s"] = start
        if end:
            sql += " AND t.deal_time <= :e"
            params["e"] = end
        sql += " ORDER BY t.deal_time"
        df = read_sql(sql, params)
        if df.empty:
            return df
        for c in ("order_price", "deal_price", "volume", "deal_amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if min_volume > 0:
            df = df[df["volume"] >= min_volume]
        return df

    def load_orders(self, start: str | None = None, end: str | None = None,
                    status: str | None = None) -> pd.DataFrame:
        """读取委托单。"""
        sql = "SELECT * FROM execution_order WHERE 1=1"
        params: Dict = {}
        if start:
            sql += " AND created_at >= :s"
            params["s"] = start
        if end:
            sql += " AND created_at <= :e"
            params["e"] = end
        if status:
            sql += " AND status = :st"
            params["st"] = status
        sql += " ORDER BY id"
        return read_sql(sql, params)

    def stats(self) -> Dict[str, int]:
        """两张表的行数概览。"""
        out = {}
        for t in ("execution_order", "execution_trade", "impact_calib"):
            try:
                out[t] = int(read_sql(f"SELECT COUNT(*) c FROM {t}").iloc[0, 0])
            except Exception:                                # noqa: BLE001
                out[t] = -1
        return out


if __name__ == "__main__":
    store = TradeStore()
    print(store.stats())
