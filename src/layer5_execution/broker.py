# -*- coding: utf-8 -*-
"""
Broker 抽象接口（第 5 层执行层）。

定义所有交易通道的统一契约，QMT / easytrader / 未来其他通道都实现这套接口，
上层（下单脚本、impact 校准）只依赖抽象，不感知具体券商。

接口设计以「拿真实成交回报」为核心目标：
    place_order  -> 委托单落库（execution_order）
    on_trade     -> 成交回报回调（execution_trade）—— 这是 impact_coef 回归的原材料
    sync_status  -> 主动对账券商端委托/成交状态（兜底：回调可能丢）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional


@dataclass
class Order:
    """委托单（统一表示，与具体券商解耦）。"""
    ts_code: str
    side: str                 # buy / sell
    volume: int               # 股
    price: Optional[float] = None      # 限价单价格；市价单为 None
    order_type: str = "limit"          # limit / market
    status: str = "submitted"          # submitted/filled/partial/canceled/rejected
    xt_order_id: Optional[str] = None  # 券商端委托号
    strategy_tag: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class Trade:
    """成交回报：委托价 vs 实际成交价 —— 冲击成本的直接观测。"""
    order_id: int
    ts_code: str
    side: str
    order_price: Optional[float]
    deal_price: float
    volume: int
    deal_time: datetime = field(default_factory=datetime.now)


class BaseBroker(ABC):
    """交易通道抽象。所有方法幂等、可重入（断线重连后状态不丢）。"""

    @abstractmethod
    def connect(self) -> bool:
        """建立与券商通道的连接。失败抛异常。"""

    @abstractmethod
    def disconnect(self) -> None:
        """断开连接。"""

    @property
    @abstractmethod
    def connected(self) -> bool:
        ...

    # ------------------------------------------------------------ 下单

    @abstractmethod
    def place_order(self, order: Order) -> str:
        """提交委托单，返回券商端委托号（失败抛异常）。

        调用方负责：价格/数量校验、风控闸门（见 layer4）、涨跌停/停牌检查。
        Broker 只做「提交」和「落库」。
        """

    @abstractmethod
    def cancel_order(self, xt_order_id: str) -> bool:
        """撤单。成功返回 True。"""

    # ------------------------------------------------------------ 查询

    @abstractmethod
    def get_positions(self) -> List[dict]:
        """当前持仓列表：[{ts_code, volume, available, cost_price, market_value}]"""

    @abstractmethod
    def get_orders(self, date: str | None = None) -> List[Order]:
        """当日委托列表（用于对账）。"""

    @abstractmethod
    def get_trades(self, date: str | None = None) -> List[Trade]:
        """当日成交回报列表（用于对账/补录）。"""

    # ------------------------------------------------------------ 成交回调

    def on_trade(self, callback: Callable[[Trade], None]) -> None:
        """注册成交回报回调（可多次注册）。

        回调签名: fn(trade: Trade)。Broker 在收到券商端成交回报时调用，
        默认行为是落库（由实现方决定），此方法给上层额外订阅的机会。
        """


class BrokerError(Exception):
    """交易通道错误（连接失败/下单被拒/成交异常等）。"""
