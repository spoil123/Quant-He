# -*- coding: utf-8 -*-
"""
第 5 层：数据流水线编排（一键日常任务）。

把四个环节串成一条可重复执行的流水线：
    1. 增量更新数据（日线 / 财务 / 指数）
    2. 跑 Top50 策略回测
    3. 跑风控报告
    4. 数据健康巡检

每个环节独立 try/except：一个失败不阻塞后续，结果结构化返回，
失败详情写入 update_log 或返回 dict，供调度器/告警消费。
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.common.logger import logger

ROOT = Path(__file__).resolve().parents[2]


def parse_wf_summary(stdout: str) -> Dict:
    """从 walk_forward 脚本输出提取 WF 总绩效行 -> 指标 dict。

    实际输出形如（行首有 "- " 前缀，不能 startswith("WF总绩效")）：
      - WF总绩效(2017-2023) ... - -0.1347 -0.0212 -0.1965 0.4383 -0.0484 0
    用正则抓括号内测试期与后随 5 个浮点指标（总收益/年化/夏普/回撤/卡玛）。
    找不到匹配返回空 dict。
    """
    for line in (stdout or "").splitlines():
        if "WF总绩效" not in line:
            continue
        m = re.search(r"WF总绩效\((\d{4}-\d{4})\)", line)
        nums = re.findall(r"-?\d+\.\d+", line)[:5]
        if m and len(nums) >= 5:
            return {"测试期": m.group(1), "总收益": nums[0],
                    "年化": nums[1], "夏普": nums[2],
                    "回撤": nums[3], "卡玛": nums[4]}
    return {}


class Pipeline:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or ROOT)
        self.py = sys.executable

    # ================================================================ 环节

    def update_data(
        self,
        only: str = "daily",
        start: str = "2015-01-01",
        workers: int = 12,
        force_full: bool = False,
    ) -> Dict:
        """增量更新数据（走 scripts/update_data.py）。"""
        cmd = [self.py, "-u", str(self.root / "scripts" / "update_data.py"),
               "--only", only, "--start", start, "--workers", str(workers)]
        if force_full:
            cmd.append("--force-full")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                               cwd=str(self.root))
            tail = (r.stdout or "").strip().splitlines()[-5:]
            return {"ok": r.returncode == 0, "step": f"update_{only}",
                    "returncode": r.returncode, "log_tail": tail,
                    "error": (r.stderr or "")[-500:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "step": f"update_{only}", "error": "timeout 3600s"}

    def run_strategy(self, start: str, end: str, out: str = "outputs/top50") -> Dict:
        """跑 Top50 策略回测（scripts/run_top50_strategy.py）。"""
        cmd = [self.py, "-u", str(self.root / "scripts" / "run_top50_strategy.py"),
               "--start", start, "--end", end, "--out", out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                               cwd=str(self.root))
            # 从输出提取绩效摘要
            metrics = {}
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if "  " in line and ":" not in line and "=" not in line:
                    parts = line.split()
                    if len(parts) == 2 and parts[1].replace(".", "").replace("-", "").isdigit():
                        metrics[parts[0]] = parts[1]
            return {"ok": r.returncode == 0, "step": "strategy",
                    "returncode": r.returncode, "metrics_tail": metrics,
                    "error": (r.stderr or "")[-500:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "step": "strategy", "error": "timeout 3600s"}

    def run_risk(self, start: str, end: str, out: str = "outputs/risk") -> Dict:
        """跑风控报告（scripts/example_risk.py）。"""
        cmd = [self.py, "-u", str(self.root / "scripts" / "example_risk.py"),
               "--start", start, "--end", end, "--out", out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                               cwd=str(self.root))
            return {"ok": r.returncode == 0, "step": "risk",
                    "returncode": r.returncode,
                    "log_tail": (r.stdout or "").strip().splitlines()[-8:],
                    "error": (r.stderr or "")[-500:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "step": "risk", "error": "timeout 3600s"}

    def run_walk_forward(self, out: str = "outputs/walk_forward") -> Dict:
        """跑 Walk-Forward 滚动验证（scripts/walk_forward.py --prod）。

        用生产配置权重验证外推表现：连续 fold 拼接净值是否跑赢基准、
        权重是否出现退化（红灯）。纳入流水线后可由调度器定期触发，
        自动监测「因子权重退化」—— 不再依赖手动跑研究脚本。
        """
        cmd = [self.py, "-u", str(self.root / "scripts" / "walk_forward.py"),
               "--prod", "--out", out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                               cwd=str(self.root))
            # 从输出提取 WF 总绩效行
            summary = parse_wf_summary(r.stdout or "")
            return {"ok": r.returncode == 0, "step": "walk_forward",
                    "returncode": r.returncode, "wf_summary": summary,
                    "log_tail": (r.stdout or "").strip().splitlines()[-8:],
                    "error": (r.stderr or "")[-500:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "step": "walk_forward", "error": "timeout 7200s"}

    def monitor(self, out_dir: str = "outputs/monitor") -> Dict:
        """数据健康巡检。"""
        from src.layer5_scheduler.monitor import DataMonitor
        dm = DataMonitor()
        report = dm.report()
        dm.print_report(report)
        path = dm.save_report(report, str(self.root / out_dir))
        return {"ok": "error" not in report, "step": "monitor", "report_path": path,
                "summary": {k: v for k, v in report.items()
                            if k in ("覆盖率", "数据新鲜度", "数据质量")}}

    # ================================================================ 一键

    def run_daily(
        self,
        strategy_range: Optional[tuple] = None,
        with_update: bool = True,
        with_strategy: bool = True,
        with_risk: bool = True,
        with_monitor: bool = True,
        with_walk_forward: bool = False,
    ) -> Dict:
        """日常一键：更新 → 回测 → 风控 → 巡检（可选 walk-forward）。

        strategy_range: (start, end)，默认用 strategy.yaml 的样本外区间。
        with_walk_forward: 是否跑因子权重外推验证（P1-5 纳入流水线）。
        """
        if strategy_range is None:
            from src.common.config import get_config
            bt = get_config("strategy")["backtest"]
            strategy_range = (bt["start"], bt["end"])
        start, end = strategy_range

        results: List[Dict] = []
        if with_update:
            results.append(self.update_data(only="daily"))
        if with_strategy:
            results.append(self.run_strategy(start, end))
        if with_risk:
            results.append(self.run_risk(start, end))
        if with_walk_forward:
            results.append(self.run_walk_forward())
        if with_monitor:
            results.append(self.monitor())

        ok = all(r.get("ok", False) for r in results)
        logger.info(f"流水线完成: {'全部成功' if ok else '存在失败'} "
                    f"({sum(1 for r in results if r.get('ok'))}/{len(results)})")

        # P1-5 主动推送：有失败环节立即推送（配置见 config/notify.yaml）
        if not ok:
            from src.common.notify import notify_on_failure
            notify_on_failure({"ok": ok, "steps": results})

        return {"ok": ok, "steps": results,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def print_summary(self, result: Dict) -> None:
        print("\n" + "=" * 60)
        print(f"  流水线结果  {result.get('finished_at', '')}")
        print("=" * 60)
        for r in result.get("steps", []):
            status = "✅" if r.get("ok") else "❌"
            print(f"  {status} {r.get('step', '?')}", end="")
            if not r.get("ok") and r.get("error"):
                print(f"  -> {r['error'][:80]}")
            else:
                print()
        print("=" * 60)
