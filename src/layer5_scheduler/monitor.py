# -*- coding: utf-8 -*-
"""
第 5 层：数据健康监控。

基于数据库已有的视图（v_coverage_by_code / v_data_quality / v_coverage_by_date）
做例行体检，回答三个问题：
    1. 覆盖全不全？   已入库 vs 全市场，缺口代码清单
    2. 数据新不新？   每只股票 last_date 是否到最近交易日，过期清单
    3. 质量好不好？   v_data_quality 里的问题按类型统计

输出：结构化 dict + 可落盘 CSV / 控制台报告。供调度流水线每日巡检。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from src.common.db import read_sql
from src.common.logger import logger


class DataMonitor:
    def __init__(self, universe_table: str = "stock_basic"):
        self.universe_table = universe_table

    # ================================================================ 查询

    def latest_trade_date(self) -> Optional[str]:
        """最近交易日（<= 今天），统一 YYYY-MM-DD 字符串。

        修复：不用 CURDATE()（依赖 DB 与本机时区一致），改为 Python 传今天日期，
        消除时区假设。
        """
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = read_sql(
            "SELECT MAX(trade_date) AS d FROM trade_calendar "
            "WHERE is_trading = 1 AND trade_date <= :today",
            {"today": today},
        )
        if df.empty or df.iloc[0, 0] is None:
            return None
        return pd.to_datetime(df.iloc[0, 0]).strftime("%Y-%m-%d")

    def coverage(self) -> pd.DataFrame:
        """每只股票的覆盖区间（来自视图）。rows 是保留字，需反引号。"""
        return read_sql(
            "SELECT ts_code, first_date, last_date, `rows` FROM v_coverage_by_code"
        )

    def quality_issues(self) -> pd.DataFrame:
        """数据质量问题（来自视图）。"""
        return read_sql(
            "SELECT trade_date, ts_code, adj_type, issue FROM v_data_quality"
        )

    def _recent_alerts(self, days: int = 2) -> List[str]:
        """读最近 N 天本地告警归档（logs/alerts/YYYY-MM-DD.md）。

        告警渠道未启用/未送达时，告警只落归档 —— 巡检报告里带出来，
        让"发不出去"的告警至少在下一次运行时被看见。
        """
        from src.common.notify import _ALERT_DIR
        out: List[str] = []
        for i in range(days):
            d = pd.Timestamp.now() - pd.Timedelta(days=i)
            p = _ALERT_DIR / f"{d:%Y-%m-%d}.md"
            if not p.exists():
                continue
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip().startswith("- [")]
            for ln in lines:
                out.append(ln.strip())
        return out[-20:]

    # ================================================================ 体检

    def report(self, stale_days: int = 5) -> Dict:
        """生成数据健康报告。stale_days：超过 N 个交易日没更新视为过期。"""
        latest = self.latest_trade_date()
        if latest is None:
            return {"error": "trade_calendar 无数据"}

        # 1. 覆盖
        n_universe = int(read_sql(
            f"SELECT COUNT(*) AS n FROM {self.universe_table} "
            "WHERE is_delisted = 0"
        ).iloc[0, 0])
        cov = self.coverage()
        n_cov = int(cov["ts_code"].nunique()) if not cov.empty else 0
        gap_codes = []
        if not cov.empty:
            all_codes = set(read_sql(
                f"SELECT ts_code FROM {self.universe_table} WHERE is_delisted = 0"
            )["ts_code"].astype(str))
            gap_codes = sorted(all_codes - set(cov["ts_code"].astype(str)))

        # 2. 最新性（用交易日历对齐：last_date 与最近交易日的间隔交易日数）
        stale = []
        if not cov.empty and latest:
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            cal = read_sql(
                "SELECT trade_date FROM trade_calendar WHERE is_trading = 1 "
                "AND trade_date <= :today "
                "ORDER BY trade_date DESC LIMIT 30",
                {"today": today},
            )["trade_date"]
            cal = pd.to_datetime(cal).dt.strftime("%Y-%m-%d").tolist()
            idx = {d: i for i, d in enumerate(cal)}
            cov2 = cov.copy()
            cov2["ts_code"] = cov2["ts_code"].astype(str)
            # 统一成 YYYY-MM-DD 字符串（datetime64 astype(str) 会带时间后缀导致匹配失败）
            cov2["last_date"] = pd.to_datetime(cov2["last_date"]).dt.strftime("%Y-%m-%d")
            cov2["lag"] = cov2["last_date"].map(
                lambda d: idx.get(latest, 99) - idx.get(d, 99) if d in idx else 99
            )
            stale = cov2[cov2["lag"] > stale_days][
                ["ts_code", "last_date", "lag"]
            ].to_dict("records")

        # 3. 质量
        issues = self.quality_issues()
        issue_stats = (
            issues.groupby("issue")["ts_code"].count().to_dict() if not issues.empty else {}
        )
        n_issues = int(len(issues)) if not issues.empty else 0

        # 4. 最近抓取失败 + 失败率告警
        failed = read_sql(
            "SELECT ts_code, message, started_at FROM update_log "
            "WHERE task_name = 'daily_price' AND status = 'failed' "
            "ORDER BY id DESC LIMIT 20"
        )
        recent_failed = failed.to_dict("records") if not failed.empty else []

        # 5. 覆盖率告警（阈值：< 95% 视为异常）
        cov_rate = round(n_cov / n_universe, 4) if n_universe else 0.0
        alerts: List[str] = []
        if n_universe and cov_rate < 0.95:
            alerts.append(f"覆盖率仅 {cov_rate:.1%}，低于 95% 阈值")
        if n_issues:
            alerts.append(f"数据质量问题 {n_issues} 条（abnormal_change 等）")
        if len(stale) > 50:
            alerts.append(f"过期股票 {len(stale)} 只，建议增量更新")

        # 6. 本地告警归档回显（渠道未启用时，告警只落这里 —— 巡检要带出来）
        archived = self._recent_alerts(days=2)
        if archived:
            alerts.append(f"近 2 日有 {len(archived)} 条归档告警未确认（见 logs/alerts/）")
            alerts.extend(f"  ↳ {a}" for a in archived)

        report = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "latest_trade_date": latest,
            "覆盖率": {"全市场": n_universe, "已入库": n_cov,
                      "覆盖率": cov_rate, "缺口数": len(gap_codes)},
            "数据新鲜度": {"过期股票数": len(stale), "过期阈值(交易日)": stale_days},
            "数据质量": {"问题总数": n_issues, "按类型": issue_stats},
            "告警": alerts,
            # 覆盖口径透明化：B 股（200/900）为外币计价，A 股策略明确排除；
            # 北交所（43/83/87/92）已纳入抓取（2026-08-30 起）
            "覆盖口径": "A 股（沪深主板/创业板/科创板/北交所）；B 股(200/900)为外币计价，策略口径排除",
            "stale_codes": stale[:50],
            "gap_codes": gap_codes[:50],
            "recent_failed": recent_failed,
        }

        # P1-5 主动推送：有告警才推（配置见 config/notify.yaml，未启用时静默降级）
        if alerts:
            from src.common.notify import notify
            body = "\n".join(f"  ⚠ {a}" for a in alerts)
            if recent_failed:
                body += f"\n  最近抓取失败 {len(recent_failed)} 条"
            notify(f"数据健康告警（{latest}）", body, level="warning")

        return report

    def print_report(self, report: Optional[Dict] = None) -> None:
        r = report or self.report()
        if "error" in r:
            print(f"[监控] {r['error']}")
            return
        print("\n" + "=" * 56)
        print(f"  数据健康报告  {r['generated_at']}")
        print("=" * 56)
        print(f"  最近交易日: {r['latest_trade_date']}")
        c = r["覆盖率"]
        print(f"  覆盖: {c['已入库']}/{c['全市场']}  ({c['覆盖率']:.2%})  缺口 {c['缺口数']}")
        print(f"  新鲜度: 过期 {r['数据新鲜度']['过期股票数']} 只")
        q = r["数据质量"]
        print(f"  质量: 问题 {q['问题总数']} 条 {q['按类型'] if q['按类型'] else ''}")
        if r.get("告警"):
            for a in r["告警"]:
                print(f"  ⚠ {a}")
        if r["stale_codes"]:
            print(f"  过期示例: {r['stale_codes'][:5]}")
        print(f"  口径: {r.get('覆盖口径', '')}")
        if r["recent_failed"]:
            print(f"  最近失败: {len(r['recent_failed'])} 条")
        print("=" * 56)

    def save_report(self, report: Dict, out_dir) -> str:
        """报告落盘（CSV），返回文件路径。"""
        import json
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        path = Path(out_dir) / f"monitor_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        logger.info(f"监控报告已保存: {path}")
        return str(path)
