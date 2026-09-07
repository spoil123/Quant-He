# -*- coding: utf-8 -*-
"""
第 5 层：定时调度器。

两种运行方式：
    run_once()   手动触发一次（可指定环节），供 Windows 任务计划 / cron 调用
    daemon()     常驻进程，按 scheduler.yaml 的时间表循环执行（备选，无需外部调度）

时间表格式（config/scheduler.yaml）：
    schedule:
      daily_update: {time: "18:00", steps: [update, strategy, risk, monitor]}
      weekly_report: {weekday: "fri", time: "18:30", steps: [monitor]}

日常推荐用外部调度（Windows 任务计划）调 run_once —— 更可靠、可审计；
daemon 模式适合无外部调度器的环境。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.common.config import get_config
from src.common.logger import logger
from src.layer5_scheduler.pipeline import Pipeline

# 步骤名 → 流水线方法
_STEP_METHODS = {
    "update": "update_data",
    "strategy": "run_strategy",
    "risk": "run_risk",
    "monitor": "monitor",
    "walk_forward": "run_walk_forward",
}

_TRADE_DAYS: set = set()
_TRADE_DAYS_FOR: str = ""          # 缓存对应的自然日，跨天自动重载


def load_trade_days() -> set:
    """加载交易日历（按自然日缓存，跨天自动重载）。

    M3 修复（2026-09-07）：daemon 原先只比对「时分」，周末和法定节假日照跑
    daily_update —— 没有新行情却发起全量更新请求，既浪费配额又容易触发
    数据源限流；更糟的是假期里 monitor 会天天报「数据不新鲜」的假告警。
    trade_calendar 已入库，直接拿来判断。

    失效保护（重要）：日历只覆盖到某个年份（当前到 2026-12-31），跨年后
    若不做保护，所有日期都判定为「非交易日」，调度会被静默停摆。因此
    日历最大日期 < 今天时返回空集合 = 不拦截。
    """
    global _TRADE_DAYS, _TRADE_DAYS_FOR
    today = datetime.now().strftime("%Y-%m-%d")
    if _TRADE_DAYS_FOR == today and _TRADE_DAYS:
        return _TRADE_DAYS
    try:
        from src.common.db import read_sql
        df = read_sql("SELECT trade_date FROM trade_calendar WHERE is_trading = 1")
        days = {str(v.date() if hasattr(v, "date") else v)
                for v in df.get("trade_date", [])}
        if days:
            if max(days) < today:
                logger.warning(
                    f"交易日历只到 {max(days)}，已过期 —— 交易日判断自动降级为"
                    f"「不拦截」，请重抓 trade_calendar 恢复该功能")
                _TRADE_DAYS, _TRADE_DAYS_FOR = set(), today
                return set()
            _TRADE_DAYS, _TRADE_DAYS_FOR = days, today
            return days
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"读取交易日历失败（交易日判断降级为不拦截）: "
                       f"{type(e).__name__}: {e}")
    return set()


def is_trading_day(d: Optional[datetime] = None) -> bool:
    """今天（或指定日）是否交易日。日历不可用时返回 True —— 宁可多跑，不可停摆。"""
    days = load_trade_days()
    if not days:
        return True
    dt = d or datetime.now()
    return str(dt.date() if hasattr(dt, "date") else dt) in days


def _needs_trading_day(spec: dict) -> bool:
    """该任务是否只在交易日执行。

    配置可显式写 trade_day_only: true/false 覆盖；没写时按步骤推断 ——
    含 update/strategy/risk 的必须交易日跑（非交易日没有新行情、没有持仓
    变动，跑了纯属空转）；纯 walk_forward / monitor 巡检允许非交易日跑
    （周末算力空闲，正好跑耗时的滚动回测）。
    """
    v = spec.get("trade_day_only")
    if v is not None:
        return bool(v)
    steps = [str(s) for s in (spec.get("steps") or [])]
    return any(s in ("update", "strategy", "risk", "signal") for s in steps)


class Scheduler:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or get_config("scheduler")
        self.pipeline = Pipeline()
        self.strategy_range = None
        bt = self.cfg.get("strategy_range")
        if not bt:
            try:
                from src.common.config import get_config as _gc
                b = _gc("strategy")["backtest"]
                bt = {"start": b["start"], "end": b["end"]}
            except Exception:                                   # noqa: BLE001
                bt = {"start": "2019-01-01", "end": "2023-12-31"}
        self.strategy_range = (bt["start"], bt["end"])

    # ================================================================ 单次

    def run_once(self, steps: Optional[List[str]] = None) -> Dict:
        """按 steps 列表执行一次（默认执行全部环节）。"""
        steps = steps or list(_STEP_METHODS.keys())
        with_update = "update" in steps
        with_strategy = "strategy" in steps
        with_risk = "risk" in steps
        with_monitor = "monitor" in steps
        with_walk_forward = "walk_forward" in steps

        logger.info(f"调度器 run_once: steps={steps} 区间={self.strategy_range}")
        result = self.pipeline.run_daily(
            strategy_range=self.strategy_range,
            with_update=with_update,
            with_strategy=with_strategy,
            with_risk=with_risk,
            with_monitor=with_monitor,
            with_walk_forward=with_walk_forward,
        )
        self.pipeline.print_summary(result)
        return result

    # ================================================================ 常驻

    def daemon(self, interval_sec: int = 60, max_workers: int = 2) -> None:
        """常驻循环：每分钟检查一次，到点执行对应环节。

        任务在独立线程执行 —— 主循环不阻塞。否则 update/strategy 这类长任务
        （可达几十分钟）会吞掉同窗其它调度点（如周五 18:00 的 daily_update
        跑超 18:30，weekly_health 当周静默丢失）。
        """
        from concurrent.futures import ThreadPoolExecutor

        schedule = (self.cfg.get("schedule") or {})
        logger.info(f"调度器 daemon 启动: {len(schedule)} 个任务, "
                    f"检查间隔 {interval_sec}s")
        last_run: Dict[str, str] = {}
        executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="sched")
        futures: Dict[str, object] = {}

        while True:
            now = datetime.now()
            for name, spec in schedule.items():
                time_str = str(spec.get("time", ""))
                weekday = spec.get("weekday")  # mon..sun 或 None(每天)
                if time_str != now.strftime("%H:%M"):
                    continue
                if weekday and weekday != now.strftime("%a").lower()[:3]:
                    continue
                # 非交易日不跑行情相关任务（周末/法定假日没有新数据，跑了是空转）
                if _needs_trading_day(spec) and not is_trading_day(now):
                    logger.debug(f"任务 {name} 需要交易日，{now:%Y-%m-%d} 非交易日，跳过")
                    continue
                key = f"{name}:{now.strftime('%Y-%m-%d %H:%M')}"
                if last_run.get(name) == key:
                    continue
                fut = futures.get(name)
                if fut is not None and not fut.done():
                    # 上一轮任务还没跑完，跳过本轮，防重复触发
                    logger.warning(f"任务 {name} 仍在运行中，跳过本轮触发")
                    continue
                last_run[name] = key
                logger.info(f"触发定时任务: {name}")

                def _run(_name=name, _spec=spec) -> None:
                    try:
                        steps = _spec.get("steps") or ["update", "strategy",
                                                       "risk", "monitor"]
                        self.run_once(steps=steps)
                    except Exception as e:                      # noqa: BLE001
                        logger.error(f"定时任务 {_name} 失败: "
                                     f"{type(e).__name__}: {e}")

                futures[name] = executor.submit(_run)
            time.sleep(interval_sec)


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="第 5 层调度器")
    ap.add_argument("--daemon", action="store_true", help="常驻模式")
    ap.add_argument("--steps", default="", help="逗号分隔: update,strategy,risk,monitor")
    args = ap.parse_args()

    sched = Scheduler()
    if args.daemon:
        sched.daemon()
    else:
        steps = [s.strip() for s in args.steps.split(",") if s.strip()] or None
        result = sched.run_once(steps=steps)
        # P1-10 失败告警：单次执行有环节失败 → 非零退出码，供任务计划/告警消费
        sys.exit(0 if result.get("ok") else 1)
