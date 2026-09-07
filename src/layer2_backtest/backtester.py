# -*- coding: utf-8 -*-
"""
向量化回测引擎（Backtester）。

设计取舍（用户已拍板）：
    1. 向量化（Pandas 整列运算），非事件驱动 —— 5000 只 × 10 年也能秒级跑完
    2. 信号为连续权重（-1 ~ 1），0 / 1 全仓切换只是它的特例
    3. A 股完整交易成本：佣金（万3双边+最低5元）+ 印花税（卖出，按 2023-08-28 分段）
       + 过户费（双边，十万分之一）
    4. 滑点：开盘价 ± 0.1%（买入上滑、卖下滑），可配置
    5. 停牌 / 涨停 / 跌停不可成交，当日维持原持仓、不顺延
    6. 严令禁止 look-ahead bias：T 日收盘产生的信号，T+1 开盘才生效

撮合与收益的精确口径（这是验收时手动验算的依据）：
    设 position[t] 为 T 日实际持仓权重，diff = position[t] - position[t-1]。

    无调仓日（diff = 0）：
        日收益 = position[t] × (close[t] / close[t-1] - 1)

    调仓日（diff ≠ 0，按开盘价 ± 滑点成交）：
        日收益 = position[t-1] × (exec_price / close[t-1] - 1)   ← 昨日持仓全部按成交价结算
               + position[t]    × (close[t] / exec_price - 1)     ← 今日持仓从成交价持有到收盘
               - |diff| × 费率                                    ← 调仓成本（按当日净值）
        其中 exec_price = open[t] × (1 + slip)   当 diff > 0（净买入）
                        = open[t] × (1 - slip)   当 diff < 0（净卖出）

    该公式对 0/1 全仓切换是精确的；对部分调仓（如 0.6→0.8）是
    "exec_price ≈ open" 的一阶近似，误差在滑点量级，可接受。
    做空（position < 0）同样成立：diff < 0 卖出开空、diff > 0 买回平空。

成本费率（A股完整版）：
    买入：佣金 + 过户费
    卖出：佣金 + 过户费 + 印花税（按日期分段）
    佣金单笔最低 5 元：按成交金额 × 费率与 5 元取大（向量化路径同样生效）
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from src.common.logger import logger
from src.layer2_backtest.metrics import (
    annual_return,
    annual_volatility,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_return,
)

# 印花税历史分段（与 config/costs.yaml 一致）
STAMP_DUTY_SCHEDULE = [
    (date(2008, 9, 19), date(2023, 8, 27), 0.001),     # 千一
    (date(2023, 8, 28), date(2099, 12, 31), 0.0005),   # 万五（2023-08-28 下调）
]

def _load_cost_defaults() -> dict:
    """成本默认值单一事实源：config/costs.yaml。

    修复：此前 DEFAULT_COST 硬编码（万3佣金/10bps 滑点），与 costs.yaml
    （万2.5）口径不一致；现统一从 costs.yaml 读，显式传入仍可覆盖。
    """
    try:
        from src.common.config import get_config
        c = get_config("costs")
        return {
            "commission": float(c["commission"]["rate"]),
            "min_commission": float(c["commission"]["min_fee"]),
            "transfer_fee": float(c["transfer_fee"]["rate"]),
            "stamp_duty": float(c["stamp_tax"]["rate"]),
            "slip_bps": float(c["slippage"].get("base_bps", 10.0)),
        }
    except Exception:                                     # noqa: BLE001
        return {"commission": 0.0003, "min_commission": 5.0,
                "transfer_fee": 0.00001, "stamp_duty": 0.001,
                "slip_bps": 10.0}


DEFAULT_COST = _load_cost_defaults()


class Backtester:
    """向量化回测引擎（单标的，连续权重）。"""

    def __init__(
        self,
        data: pd.DataFrame,
        initial_capital: float = 1_000_000,
        commission: float = DEFAULT_COST["commission"],
        min_commission: float = DEFAULT_COST["min_commission"],
        transfer_fee: float = DEFAULT_COST["transfer_fee"],
        stamp_duty: float = DEFAULT_COST["stamp_duty"],
        slip_bps: float = DEFAULT_COST["slip_bps"],
        min_fee_floor: float = 10_000.0,
        use_historical_stamp: bool = True,
        cost_model: str = "full",
    ):
        """
        data: 需含列 date / open / close（high, low, volume 可选）。
              停牌与涨跌停标记可选：
                is_suspended (0/1)  缺失时用 volume<=0 推断
                is_limit_up / is_limit_down (0/1)  缺失时不限制
        cost_model: 'full'=A股完整成本 | 'simple'=仅佣金（万3单边折算）| 传 dict 自定义
        """
        # ---------------- 数据准备 ----------------
        if "date" in data.columns and not isinstance(data.index, pd.DatetimeIndex):
            data = data.set_index("date")
        data = data.copy()

        missing = {"open", "close"} - set(data.columns)
        assert not missing, f"data 缺少必需列: {missing}"

        for c in ("open", "close"):
            data[c] = pd.to_numeric(data[c], errors="coerce")

        self.data = data.sort_index()
        self.initial_capital = float(initial_capital)

        # ---------------- 成本参数 ----------------
        if isinstance(cost_model, dict):
            self.commission = float(cost_model.get("commission", DEFAULT_COST["commission"]))
            self.min_commission = float(cost_model.get("min_commission", 0.0))
            self.transfer_fee = float(cost_model.get("transfer_fee", DEFAULT_COST["transfer_fee"]))
            self.stamp_duty = float(cost_model.get("stamp_duty", DEFAULT_COST["stamp_duty"]))
            self.min_fee_floor = float(cost_model.get("min_fee_floor", 10_000.0))
            self.use_historical_stamp = bool(cost_model.get("use_historical_stamp", True))
        elif cost_model == "simple":
            # 用户最初规格：单边万三（双向等效双边 0.0006，这里按真实双边口径处理）
            self.commission = 0.0003
            self.min_commission = 0.0
            self.transfer_fee = 0.0
            self.stamp_duty = 0.0
            self.min_fee_floor = 0.0
            self.use_historical_stamp = False
        else:  # full
            self.commission = float(commission)
            self.min_commission = float(min_commission)
            self.transfer_fee = float(transfer_fee)
            self.min_fee_floor = float(min_fee_floor)
            self.stamp_duty = float(stamp_duty)
            self.use_historical_stamp = bool(use_historical_stamp)

        self.slip = float(slip_bps) / 1e4

        # ---------------- 停牌 / 涨跌停标记 ----------------
        if "is_suspended" in self.data.columns:
            self._suspended = self.data["is_suspended"].astype(bool)
        elif "volume" in self.data.columns:
            self._suspended = self.data["volume"].fillna(0) <= 0
            logger.debug("data 无 is_suspended 列，已用 volume<=0 推断停牌")
        else:
            self._suspended = pd.Series(False, index=self.data.index)

        self._limit_up = self.data["is_limit_up"].astype(bool) if "is_limit_up" in self.data.columns \
            else pd.Series(False, index=self.data.index)
        self._limit_down = self.data["is_limit_down"].astype(bool) if "is_limit_down" in self.data.columns \
            else pd.Series(False, index=self.data.index)

    # ================================================================ 主流程

    def run(self, signals: pd.Series) -> dict:
        """执行回测。

        signals: 每日信号，连续权重 -1~1（1=满仓做多，0=空仓，-1=满仓做空，0.6=六成仓）。
                 长度与索引必须与 data 一致。索引可以是 date 或与 data.index 对齐。
        规则：
            - 信号 T 日收盘后产生，T+1 开盘成交（signals.shift(1)）
            - 次日停牌/涨停(买)/跌停(卖)则不成交，维持原持仓、不顺延
        返回:
            {'equity_curve': Series, 'position': Series, 'trades': DataFrame,
             'daily_returns': Series, 'metrics': dict}
        """
        # ---------------- 信号校验与对齐 ----------------
        assert signals is not None, "signals 不能为空"
        assert len(signals) == len(self.data), (
            f"信号长度 {len(signals)} 与数据长度 {len(self.data)} 不一致"
        )

        if isinstance(signals, pd.Series):
            target = signals.reindex(self.data.index).astype(float)
            assert not target.isna().any(), "signals 索引与 data 未对齐（存在缺失）"
        else:
            target = pd.Series(np.asarray(signals, dtype=float), index=self.data.index)

        target = target.clip(-1.0, 1.0)
        # T+1：T 日信号，T+1 开盘生效
        target = target.shift(1).fillna(0.0)

        # ---------------- 实际持仓（受限日维持原仓位） ----------------
        diff = target.diff().fillna(0.0)
        # 涨停日想加仓（diff>0）买不进；跌停日想减仓（diff<0）卖不出
        blocked = self._suspended | (self._limit_up & (diff > 0)) | (self._limit_down & (diff < 0))
        if int(blocked.sum()):
            logger.info(f"受限未成交 {int(blocked.sum())} 个交易日（停牌/涨跌停）")

        pos = target.copy()
        pos[blocked] = np.nan
        pos = pos.ffill().fillna(0.0)      # 受限日沿用最近一次实际持仓

        # ---------------- 日收益 ----------------
        close = self.data["close"].astype(float)
        open_ = self.data["open"].astype(float)
        close_prev = close.shift(1)
        pos_prev = pos.shift(1).fillna(0.0)
        diff = pos - pos_prev

        # 无调仓日：close-to-close
        ret = pos_prev * (close / close_prev - 1.0)

        # 调仓日：按开盘价 ± 滑点成交
        is_rb = diff.abs() > 1e-10
        nav_base = None                    # 成本基数（前一日无成本净值）
        if bool(is_rb.any()):
            exec_price = pd.Series(
                np.where(diff > 0, open_ * (1 + self.slip), open_ * (1 - self.slip)),
                index=self.data.index,
            )
            fee_rate = self._fee_rate(diff)

            # 成本基数 = 前一日「无成本」净值（M1 修复，对齐 layer3 口径）。
            # 用固定 initial_capital 会让成本占比随净值增长单调下降：净值翻倍后
            # 同样权重的调仓真实金额是 2 倍，算出来的成本却还是原来那点，等于
            # 凭空造出「规模越大越省钱」的假象，回测收益系统性虚高。
            ret_gross = pos_prev * (close / close_prev - 1.0)
            ret_gross_rb = (pos_prev * (exec_price / close_prev - 1.0)
                            + pos * (close / exec_price - 1.0))
            ret_gross = ret_gross.where(~is_rb, ret_gross_rb).fillna(0.0)
            ret_gross.iloc[0] = 0.0
            nav_base = (self.initial_capital * (1.0 + ret_gross).cumprod()).shift(1)
            nav_base = nav_base.fillna(self.initial_capital)

            # 真实成本：佣金单笔最低 min_commission（A 股 5 元），
            # 但成交额 < min_fee_floor 的小额调仓只收比例佣金（豁免，防小额成本畸高）
            notional = diff.abs() * nav_base
            active = notional > 0
            comm = np.where(active.to_numpy(),
                            self._commission_for(notional.to_numpy()), 0.0)
            other = notional * (fee_rate - self.commission)   # 过户费 + 印花税
            # 量纲：ret 是收益率（无单位），成本是绝对金额，必须除以本金转比例
            cost = (pd.Series(comm, index=diff.index) + other) / nav_base
            ret_rb = (
                pos_prev * (exec_price / close_prev - 1.0)
                + pos * (close / exec_price - 1.0)
                - cost
            )
            ret = ret.where(~is_rb, ret_rb)

        ret = ret.fillna(0.0)
        # 首日没有前收盘，持仓为 0，收益为 0（首日 target 已 shift 为空）
        ret.iloc[0] = 0.0

        # ---------------- 净值与成交明细 ----------------
        nav = (1.0 + ret).cumprod() * self.initial_capital
        equity = nav.rename("equity")

        trades = self._build_trades(pos, diff,
                                    exec_price if bool(is_rb.any()) else None,
                                    nav, nav_base)

        # ---------------- 指标 ----------------
        metrics = self.calc_metrics(equity, returns=ret, position=pos, trades=trades)

        return {
            "equity_curve": equity,
            "position": pos.rename("position"),
            "daily_returns": ret.rename("daily_return"),
            "trades": trades,
            "metrics": metrics,
        }

    # ================================================================ 成本

    def _commission_for(self, notional) -> Any:
        """单笔佣金：比例佣金与最低佣金取大，小额调仓豁免最低佣金。

        M1 修复（2026-09-07）：原先 run() 里带 min_fee_floor 豁免、
        _build_trades() 里一律强收 min_commission，两个口径并存 —— 净值曲线
        实际扣减的成本和成交明细里列的佣金对不上，对账必然差一截。
        抽成唯一实现，两处共用，杜绝再次分裂。

        豁免逻辑：成交额 < min_fee_floor 时只收比例佣金。真实场景里这类小额
        通常是权重微调，若强收 5 元最低佣金，比例成本会畸高到失真。
        """
        n = np.asarray(notional, dtype=float)
        floor = np.where(n >= self.min_fee_floor, self.min_commission, 0.0)
        return np.maximum(n * self.commission, floor)

    def _stamp_duty_for(self, d) -> float:
        """按日期取印花税率。"""
        if not self.use_historical_stamp:
            return self.stamp_duty
        dd = d.date() if hasattr(d, "date") else d
        for s, e, r in STAMP_DUTY_SCHEDULE:
            if s <= dd <= e:
                return r
        return self.stamp_duty

    def _fee_rate(self, diff: pd.Series) -> pd.Series:
        """每笔调仓的费率（按方向）：买入 = 佣金+过户费，卖出 = 佣金+过户费+印花税。"""
        dates = pd.to_datetime(self.data.index)
        stamp = pd.Series([self._stamp_duty_for(d) for d in dates], index=self.data.index)

        buy_rate = self.commission + self.transfer_fee
        sell_rate = self.commission + self.transfer_fee + stamp

        rates = np.where(diff > 0, buy_rate, sell_rate)
        return pd.Series(rates, index=self.data.index)

    # ================================================================ 成交明细

    def _build_trades(
        self,
        pos: pd.Series,
        diff: pd.Series,
        exec_price: Optional[pd.Series],
        nav: pd.Series,
        nav_base: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """从持仓变化生成成交明细。

        nav_base 是 run() 里算的成本基数（前一日无成本净值）。传进来是为了让
        明细里的佣金与净值曲线实际扣减的成本**同源** —— 不传则退回用含成本的
        nav，两者会差一截，对账对不上（M1 修复）。
        """
        chg = diff.abs() > 1e-10
        if not bool(chg.any()):
            return pd.DataFrame(columns=["date", "side", "price", "weight", "fee_rate"])

        base = nav_base if nav_base is not None else nav
        rows = []
        nav_prev = base.shift(1).fillna(self.initial_capital)
        for t in self.data.index[chg]:
            d = diff.loc[t]
            side = "buy" if d > 0 else "sell"
            price = float(exec_price.loc[t]) if exec_price is not None else float(self.data.loc[t, "open"])
            fee = self._fee_rate(pd.Series({t: d})).loc[t]
            # 调仓金额 ≈ |Δw| × 昨日净值（与 run() 同一个基数）
            notional = abs(d) * nav_prev.loc[t]
            commission = float(self._commission_for(notional))
            rows.append({
                "date": t,
                "side": side,
                "price": round(price, 4),
                "weight": round(float(d), 6),
                "notional": round(float(notional), 2),
                "commission": round(commission, 2),
                "stamp_tax": round(float(abs(d) * nav_prev.loc[t] * (fee - self.commission - self.transfer_fee)), 2),
            })

        return pd.DataFrame(rows).reset_index(drop=True)

    # ================================================================ 绩效指标

    def calc_metrics(
        self,
        equity_curve: pd.Series,
        returns: Optional[pd.Series] = None,
        position: Optional[pd.Series] = None,
        trades: Optional[pd.DataFrame] = None,
    ) -> Dict[str, float]:
        """核心绩效指标：年化收益、最大回撤、夏普、胜率、盈亏比、换手率。"""
        eq = equity_curve.astype(float)
        m: Dict[str, float] = {
            "总收益率": total_return(eq),
            "年化收益率": annual_return(eq),
            "年化波动率": annual_volatility(eq),
            "夏普比率": sharpe_ratio(eq),
            "索提诺比率": sortino_ratio(eq),
            "最大回撤": max_drawdown(eq),
            "交易数": 0,
            "胜率": np.nan,
            "盈亏比": np.nan,
            "盈利因子": np.nan,
            "年化换手率": np.nan,
        }

        # ---- 换手率：年化单边 = Σ|Δw| ÷ 年数 ----
        if returns is not None and position is not None:
            turnover = position.diff().abs().sum()
            years = len(eq) / 252.0
            if years > 0:
                m["年化换手率"] = turnover / years

        # ---- 交易级胜率 / 盈亏比（持仓期法，与净值一致可验算） ----
        if trades is not None and len(trades):
            m["交易数"] = int(len(trades) // 2)
            pnl = self._holding_period_pnl(eq, position)
            if pnl:
                wins = [p for p in pnl if p > 0]
                losses = [p for p in pnl if p <= 0]
                m["胜率"] = len(wins) / len(pnl) if pnl else np.nan
                avg_win = np.mean(wins) if wins else 0.0
                avg_loss = abs(np.mean(losses)) if losses else 0.0
                m["盈亏比"] = (avg_win / avg_loss) if avg_loss > 0 else np.nan
                gross_win = sum(wins)
                gross_loss = abs(sum(losses))
                m["盈利因子"] = (gross_win / gross_loss) if gross_loss > 0 else np.nan

        return m

    def _holding_period_pnl(self, equity: pd.Series, position: Optional[pd.Series]) -> list:
        """持仓期收益：每段"持仓 != 0"的连续区间，收益 = 段末净值 / 段前净值 - 1。

        段前净值取开仓日前一日的净值；首段无前值时用初始资金。
        与净值曲线口径一致，可手动验算。
        """
        if position is None:
            return []
        hold = position.abs() > 1e-10
        prev_nav = equity.shift(1)
        entry = None
        pnl: list = []
        for i in equity.index:
            if hold.loc[i]:
                if entry is None:
                    v = prev_nav.loc[i]
                    entry = v if pd.notna(v) else equity.iloc[0]
            else:
                if entry is not None:
                    pnl.append(float(equity.loc[i] / entry - 1.0))
                    entry = None
        if entry is not None:
            pnl.append(float(equity.iloc[-1] / entry - 1.0))
        return pnl

    # ================================================================ 画图

    def plot(
        self,
        benchmark: Optional[pd.Series] = None,
        title: str = "策略净值",
        save_path: Optional[Path | str] = None,
    ):
        """净值曲线（对比基准可选）。保存 PNG，返回 figure。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC"]
        plt.rcParams["axes.unicode_minus"] = False

        nav = self._last_run["equity_curve"] if hasattr(self, "_last_run") else None
        if nav is None:
            raise RuntimeError("请先调用 run() 再画图")

        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(nav.index, nav / self.initial_capital, label="策略净值", linewidth=1.5)

        if benchmark is not None:
            b = benchmark.reindex(nav.index)
            b = b / b.iloc[0]
            ax.plot(b.index, b, label="基准", linewidth=1.2, alpha=0.8)

        ax.set_title(title)
        ax.set_ylabel("净值（初始=1）")
        ax.legend()
        ax.grid(alpha=0.3)

        if save_path is None:
            save_path = Path(__file__).resolve().parents[2] / "outputs" / "figures" / "backtest_nav.png"
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"净值曲线已保存: {save_path}")
        plt.close(fig)
        return fig


if __name__ == "__main__":
    # 自检：用模拟数据跑一个简单 0/1 策略，验证数字
    rng = np.random.default_rng(0)
    n = 300
    idx = pd.bdate_range("2023-01-02", periods=n)
    price = 10 * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    df = pd.DataFrame({
        "date": idx,
        "open": price * (1 + rng.normal(0, 0.003, n)),
        "high": price * 1.01,
        "low": price * 0.99,
        "close": price,
        "volume": rng.integers(1e6, 5e6, n).astype(float),
    })

    # 双均线信号：MA10 vs MA30
    ma_s = price[:]  # noqa
    s = pd.Series(price, index=idx)
    fast, slow = s.rolling(10).mean(), s.rolling(30).mean()
    sig = (fast > slow).astype(int)

    bt = Backtester(df, initial_capital=1_000_000)
    result = bt.run(sig)
    bt._last_run = result
    print("=== 双均线(10/30) 模拟数据回测 ===")
    for k, v in result["metrics"].items():
        print(f"  {k:12s} {v}")
    print("\n成交明细（前 5 笔）:")
    print(result["trades"].head(5).to_string(index=False))
