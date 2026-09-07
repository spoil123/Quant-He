# -*- coding: utf-8 -*-
"""
A 股交易成本模型（backtrader CommInfoBase 实现）。

三项费用：
    佣金      券商收取，买卖双向，通常万 2.5，单笔最低 5 元
    印花税    国家税务，仅卖出收取。2023-08-28 由千一下调为万五
    过户费    中国结算，沪深双边，成交金额的十万分之一

为什么要按日期切换印花税率：
    回测区间 2019-2023 横跨 2023-08-28 这个调税节点。用单一费率会让
    政策变化前后的成本都不对，虽然影响不大（单边 5 个基点），但既然要算就算准。

用法：
    comminfo = AStockCommission(commission=0.00025, min_commission=5.0)
    cerebro.broker.addcommissioninfo(comminfo)
    # 在策略里按日期更新印花税：
    comminfo.update_by_date(self.data.datetime.date(0))
"""

from __future__ import annotations

from datetime import date

import backtrader as bt

# 印花税历史分段：(生效起始日, 结束日, 税率)
STAMP_DUTY_SCHEDULE = [
    (date(2008, 9, 19), date(2023, 8, 27), 0.001),     # 千一，单边卖出
    (date(2023, 8, 28), date(2099, 12, 31), 0.0005),   # 万五（2023-08-28 起）
]


class AStockCommission(bt.CommInfoBase):
    """A 股佣金 / 印花税 / 过户费 综合成本。"""

    params = (
        ("commission", 0.00025),      # 佣金率，双边
        ("min_commission", 5.0),      # 单笔最低佣金
        ("stamp_duty", 0.0005),       # 印花税率，仅卖出（会被 update_by_date 覆盖）
        ("transfer_fee", 0.00001),    # 过户费率，双边
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),            # 费率按绝对值解释，不再除以 100
    )

    def update_by_date(self, d) -> None:
        """按日期切换印花税率。"""
        if d is None:
            return
        if isinstance(d, date):
            dd = d
        else:
            dd = d.date() if hasattr(d, "date") else d
        for start, end, rate in STAMP_DUTY_SCHEDULE:
            if start <= dd <= end:
                self.p.stamp_duty = rate
                return

    def _getcommission(self, size, price, pseudoexec):
        """返回该笔交易的总费用（正数）。

        size > 0 买入，size < 0 卖出。
        """
        value = abs(size) * price
        if value <= 0:
            return 0.0

        # 佣金（双边），单笔不低于 5 元
        comm = value * self.p.commission
        if self.p.min_commission and comm < self.p.min_commission:
            comm = self.p.min_commission

        # 过户费（双边）
        transfer = value * self.p.transfer_fee

        # 印花税（仅卖出）
        stamp = value * self.p.stamp_duty if size < 0 else 0.0

        return comm + transfer + stamp


def estimate_cost(value: float, is_sell: bool = False,
                  commission: float = 0.00025, min_commission: float = 5.0,
                  stamp_duty: float = 0.0005, transfer_fee: float = 0.00001) -> float:
    """独立估算一笔交易的成本（不依赖 backtrader，用于快速测算）。"""
    comm = max(value * commission, min_commission)
    transfer = value * transfer_fee
    stamp = value * stamp_duty if is_sell else 0.0
    return comm + transfer + stamp


if __name__ == "__main__":
    c = AStockCommission()
    for d in [date(2022, 5, 10), date(2023, 9, 1)]:
        c.update_by_date(d)
        buy_cost = estimate_cost(100_000, is_sell=False, stamp_duty=c.p.stamp_duty)
        sell_cost = estimate_cost(100_000, is_sell=True, stamp_duty=c.p.stamp_duty)
        print(f"{d}  印花税={c.p.stamp_duty:.5f}")
        print(f"   10万元买入成本 {buy_cost:8.2f} 元（{buy_cost / 100000 * 1e4:.2f} 基点）")
        print(f"   10万元卖出成本 {sell_cost:8.2f} 元（{sell_cost / 100000 * 1e4:.2f} 基点）")
        print(f"   一次完整买卖往返 {buy_cost + sell_cost:8.2f} 元"
              f"（{(buy_cost + sell_cost) / 100000 * 1e4:.2f} 基点）")
