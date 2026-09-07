# -*- coding: utf-8 -*-
"""
QMT(MiniQMT) 交易适配器（第 5 层执行层）。

通道：迅投 MiniQMT 客户端 + 官方 xtquant SDK（券商合规接口）。
拿真实成交回报：xtquant 的 on_stock_trade 回调直接给成交价/成交量/成交时间，
与委托价对比即得冲击成本 —— 这正是 impact_coef 回归要的数据。

前置条件（本模块不负责安装）：
    1. 券商开通 QMT/MiniQMT 权限（多数支持 QMT 的券商免费开通）
    2. 安装 MiniQMT 客户端并登录（模拟盘/仿真账号也可）
    3. 把客户端里的 xtquant 目录拷到项目（或 pip install xtquant）

延迟导入设计：
    xtquant 只有装了 MiniQMT 的环境才有，没装时 import 会崩。
    这里在 connect() 时才导入 —— 让本模块可以被 import（如看文档），
    且没有 QMT 环境时给出明确报错而不是 ModuleNotFoundError 一闪而过。

配置（config/execution.yaml）：
    qmt:
      xt_path: "D:/迅投极速交易终端/userdata_mini"   # MiniQMT userdata_mini 路径
      account: "资金账号"
      session_id: 1
      path_xtquant: ""    # 留空=从 MiniQMT 环境自动找；也可填 xtquant 目录绝对路径
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Callable, Dict, List, Optional

from src.common.config import get_config
from src.common.logger import logger
from src.layer5_execution.broker import (
    BaseBroker,
    BrokerError,
    Order,
    Trade,
)
from src.layer5_execution.trade_store import TradeStore


def _import_xtquant(path_xtquant: Optional[str] = None):
    """延迟导入 xtquant。返回 (xt_trader 模块, xt_constant 模块)。

    path_xtquant 提供时把它加入 sys.path（适配器可脱离 MiniQMT 安装目录运行）。
    """
    if path_xtquant:
        p = str(path_xtquant)
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from xtquant import xttrader  # noqa: PLC0415
        from xtquant import xtconstant  # noqa: PLC0415
        return xttrader, xtconstant
    except ImportError as e:
        raise BrokerError(
            "无法导入 xtquant —— 需要 MiniQMT 环境。请：\n"
            "  1. 券商开通 QMT/MiniQMT 权限并安装客户端\n"
            "  2. 在 config/execution.yaml 的 qmt.path_xtquant 填入 xtquant 目录，"
            "或从客户端目录复制 xtquant 到可 import 位置\n"
            f"  原始错误: {e}"
        ) from e


class QmtBroker(BaseBroker):
    """MiniQMT(xtquant) 适配器。"""

    def __init__(self, cfg: Optional[dict] = None,
                 store: Optional[TradeStore] = None):
        self.cfg = cfg or get_config("execution").get("qmt", {})
        self.store = store or TradeStore(broker="qmt")
        self._xttrader = None
        self._xtconstant = None
        self._trader = None          # XtQuantTrader 实例
        self._account = None         # 资金账号
        self._connected = False
        self._trade_callbacks: List[Callable[[Trade], None]] = []

    # ------------------------------------------------------------ 连接

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """连接 MiniQMT。先建连接再订阅账号，失败抛 BrokerError。"""
        xttrader, xtconstant = _import_xtquant(self.cfg.get("path_xtquant"))
        self._xttrader = xttrader
        self._xtconstant = xtconstant

        path = self.cfg.get("xt_path")
        account = str(self.cfg.get("account", ""))
        session_id = int(self.cfg.get("session_id", 1))
        if not path:
            raise BrokerError("config/execution.yaml 缺 qmt.xt_path（MiniQMT userdata_mini 路径）")
        if not account:
            raise BrokerError("config/execution.yaml 缺 qmt.account（资金账号）")

        try:
            trader = xttrader.XtQuantTrader(path, session_id)
            callback = self._build_callback(xttrader)
            trader.register_callback(callback)
            trader.start()
            connect_result = trader.connect()
            if connect_result != 0:
                raise BrokerError(
                    f"MiniQMT 连接失败，错误码 {connect_result}（请确认客户端已启动并登录）"
                )
            self._trader = trader
            self._account = xttrader.StockAccount(account)
            trader.subscribe(self._account)
            self._connected = True
            logger.info(f"QMT 已连接: account={account} session={session_id}")
            return True
        except BrokerError:
            raise
        except Exception as e:                          # noqa: BLE001
            raise BrokerError(f"QMT 连接异常: {type(e).__name__}: {e}") from e

    def disconnect(self) -> None:
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:                            # noqa: BLE001
                pass
        self._connected = False

    def _build_callback(self, xttrader):
        """构造 XtQuantTraderCallback 子类实例。

        on_stock_trade 是成交回报的核心 —— 每笔成交回调一次，
        落库 + 转发给上层订阅者（impact 校准脚本）。
        """
        broker = self

        class _Cb(xttrader.XtQuantTraderCallback):
            def on_disconnected(self):
                broker._connected = False
                logger.error("QMT 连接断开")

            def on_stock_order(self, order):
                logger.debug(f"委托回报: {order.order_id} {order.stock_code} "
                             f"{order.order_type} status={order.order_status}")

            def on_stock_trade(self, trade):
                try:
                    t = Trade(
                        order_id=0,      # 订单 id 在落库时回填
                        ts_code=trade.stock_code,
                        side="buy" if trade.order_type ==
                             broker._xtconstant.STOCK_BUY else "sell",
                        order_price=float(trade.price) if trade.price else None,
                        deal_price=float(trade.traded_price),
                        volume=int(trade.traded_volume),
                        deal_time=datetime.now(),
                    )
                    # 先落库拿 order_id：优先用下单时写入的本地 id 精确关联
                    t.order_id = broker._resolve_order_id(trade)
                    broker.store.save_trade(t)
                    # 推进委托单状态：不推进的话订单永远停在 submitted，
                    # 状态机断裂且后续成交会错配到历史委托单上
                    if t.order_id:
                        try:
                            broker.store.advance_order_on_fill(t.order_id)
                        except Exception as e:            # noqa: BLE001
                            logger.warning(f"推进委托单 #{t.order_id} 状态失败: "
                                           f"{type(e).__name__}: {e}")
                    for cb in broker._trade_callbacks:
                        try:
                            cb(t)
                        except Exception:                # noqa: BLE001
                            logger.exception("成交回调异常")
                    logger.info(f"成交回报: {t.ts_code} {t.side} "
                                f"{t.volume}股 @ {t.deal_price}")
                except Exception as e:                   # noqa: BLE001
                    logger.error(f"处理成交回报失败: {type(e).__name__}: {e}")

        return _Cb()

    def _match_order_id(self, ts_code: str, side: str) -> int:
        """启发式兜底：按 (代码, 方向) 找最近一条**未终结**委托。

        只在 order_remark / xt_order_id 都拿不到时才走到这里（正常路径走
        _resolve_order_id）。必须排除已终结状态 —— 修复前状态机不推进、
        所有订单都停在 submitted，这里等于在全部历史委托里随便挑一个，
        同日同票多笔时必然错配。
        """
        try:
            df = self.store.load_orders()
        except Exception as e:                           # noqa: BLE001
            logger.debug(f"读取委托单失败: {e}")
            return 0
        if df.empty or "status" not in df.columns:
            return 0
        st = df["status"].astype(str)
        is_open = st.isin(("submitted", "partial", "pending", "nan", ""))
        cand = df[is_open & (df["ts_code"] == ts_code) & (df["side"] == side)]
        if cand.empty:
            return 0
        return int(cand.iloc[-1]["id"])

    def _resolve_order_id(self, trade) -> int:
        """从成交回报解析本地委托单 id（按可靠性递减的三级匹配）。

        H3 修复（2026-09-07）：旧实现只用 (代码, 方向) 找最近一条 submitted
        订单，同日同票下多笔单时先到的成交会错配到后下单的委托上。改为：

          1. order_remark —— place_order 把本地 order_id 写进 order_stock 的
             remark 参数，QMT 会在成交回报里原样带回，这是唯一不会错的关联；
          2. xt_order_id —— 用券商委托号反查 execution_order.xt_order_id；
          3. (ts_code, side) 最近未终结委托 —— 兜底。
        """
        rid = getattr(trade, "order_remark", None)
        if rid:
            try:
                n = int(str(rid).strip())
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass

        xid = getattr(trade, "order_id", None)
        if xid:
            try:
                df = self.store.load_orders()
                if not df.empty and "xt_order_id" in df.columns:
                    hit = df[df["xt_order_id"].astype(str) == str(xid)]
                    if not hit.empty:
                        return int(hit.iloc[-1]["id"])
            except Exception as e:                       # noqa: BLE001
                logger.debug(f"按券商委托号 {xid} 反查失败: {e}")

        side = ("buy" if trade.order_type == self._xtconstant.STOCK_BUY
                else "sell")
        return self._match_order_id(trade.stock_code, side)

    # ------------------------------------------------------------ 下单

    def place_order(self, order: Order) -> str:
        if not self._connected or self._trader is None:
            raise BrokerError("QMT 未连接，请先 connect()")

        # 落库拿 order id（先落，券商端失败再标记 rejected）
        order_id = self.store.save_order(order)

        price_type = (self._xtconstant.FIX_PRICE
                      if order.order_type == "limit"
                      else self._xtconstant.LATEST_PRICE)
        try:
            result = self._trader.order_stock(
                self._account, order.ts_code,
                (self._xtconstant.STOCK_BUY if order.side == "buy"
                 else self._xtconstant.STOCK_SELL),
                order.volume,
                price_type,
                float(order.price) if order.price else 0.0,
                str(order_id),       # order_remark: 用本地 id 做关联
            )
            if result is None or getattr(result, "error_id", -1) != 0:
                err = getattr(result, "error_msg", "未知错误")
                self.store.update_order_status(order_id, "rejected")
                raise BrokerError(f"下单被拒: {err}")
            xt_order_id = str(getattr(result, "order_id", order_id))
            self.store.update_order_status(order_id, "submitted")
            # 回填券商委托号
            from src.common.db import execute
            execute(
                "UPDATE execution_order SET xt_order_id = :x WHERE id = :id",
                {"x": xt_order_id, "id": order_id},
            )
            logger.info(f"委托已报: {order.ts_code} {order.side} "
                        f"{order.volume}股 @{order.price} → {xt_order_id}")
            return xt_order_id
        except BrokerError:
            raise
        except Exception as e:                          # noqa: BLE001
            self.store.update_order_status(order_id, "rejected")
            raise BrokerError(f"下单异常: {type(e).__name__}: {e}") from e

    def cancel_order(self, xt_order_id: str) -> bool:
        if not self._connected or self._trader is None:
            raise BrokerError("QMT 未连接")
        result = self._trader.cancel_order_stock(self._account, int(xt_order_id))
        ok = result is not None and getattr(result, "error_id", -1) == 0
        if ok:
            df = self.store.load_orders()
            if not df.empty:
                hit = df[df["xt_order_id"] == str(xt_order_id)]
                if not hit.empty:
                    self.store.update_order_status(int(hit.iloc[0]["id"]), "canceled")
            logger.info(f"撤单成功: {xt_order_id}")
        return ok

    # ------------------------------------------------------------ 查询

    def get_positions(self) -> List[dict]:
        if not self._connected or self._trader is None:
            raise BrokerError("QMT 未连接")
        positions = self._trader.query_stock_positions(self._account)
        return [
            {
                "ts_code": p.stock_code,
                "volume": int(p.volume),
                "available": int(p.can_use_volume),
                "cost_price": float(p.open_price),
                "market_value": float(p.market_value),
            }
            for p in (positions or [])
            if int(getattr(p, "volume", 0)) > 0
        ]

    def get_orders(self, date: str | None = None) -> List[Order]:
        if not self._connected or self._trader is None:
            raise BrokerError("QMT 未连接")
        orders = self._trader.query_stock_orders(self._account)
        out = []
        for o in orders or []:
            out.append(Order(
                ts_code=o.stock_code,
                side="buy" if o.order_type == self._xtconstant.STOCK_BUY else "sell",
                volume=int(o.order_volume),
                price=float(o.price) if o.price else None,
                status=self._order_status_str(o.order_status),
                xt_order_id=str(o.order_id),
            ))
        return out

    def get_trades(self, date: str | None = None) -> List[Trade]:
        if not self._connected or self._trader is None:
            raise BrokerError("QMT 未连接")
        trades = self._trader.query_stock_trades(self._account)
        out = []
        for t in trades or []:
            out.append(Trade(
                order_id=0,
                ts_code=t.stock_code,
                side="buy" if t.order_type == self._xtconstant.STOCK_BUY else "sell",
                order_price=float(t.price) if t.price else None,
                deal_price=float(t.traded_price),
                volume=int(t.traded_volume),
                deal_time=datetime.now(),
            ))
        return out

    @staticmethod
    def _order_status_str(status: int) -> str:
        """xtconstant 委托状态码 -> 统一状态串。"""
        mapping = {
            48: "submitted",   # 已报
            49: "submitted",   # 待报
            50: "filled",      # 已成交
            51: "partial",     # 部成
            52: "canceled",    # 已撤
            53: "rejected",    # 废单
        }
        return mapping.get(int(status), "submitted")

    # ------------------------------------------------------------ 成交订阅

    def on_trade(self, callback: Callable[[Trade], None]) -> None:
        self._trade_callbacks.append(callback)


if __name__ == "__main__":
    print("QmtBroker 适配器（需 MiniQMT 环境才能连接）")
    print("配置示例见 config/execution.yaml")
