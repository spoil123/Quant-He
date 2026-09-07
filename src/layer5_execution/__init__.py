# -*- coding: utf-8 -*-
"""
第 5 层：模拟盘执行层（P1-11）。

目标：接 QMT(MiniQMT/xtquant) 模拟盘，拿「委托价 vs 成交价」的真实成交回报，
回归 impact_coef —— 把成本系数从「经验值」变成「实证值」。

分层设计（防止被单一券商绑定）：
    BaseBroker      抽象接口：下单 / 撤单 / 查持仓 / 查成交
    QmtBroker       xtquant 适配器（券商官方通道，回调拿真实成交回报）
    TradeStore      成交落库（execution_order / execution_trade）
    impact.py       从成交回报回归 impact_coef（脚本 scripts/calibrate_impact.py 调用）

为什么直连 xtquant 而不是 easytrader：
    easytrader 的 miniqmt 模式底层也是 xtquant，多一层封装多一层坏；
    且同花顺模式是 pywinauto 模拟鼠标键盘（合规灰色、客户端升级即失效）。
    本项目要的是「真实成交回报」——xtquant 的 on_stock_trade 回调最直接。
"""
