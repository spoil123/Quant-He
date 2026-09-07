# -*- coding: utf-8 -*-
"""第 5 层入口：日常流水线 / 定时调度。

用法：
    python scripts/run_daily.py                     # 全流程一次
    python scripts/run_daily.py --steps monitor     # 只巡检
    python scripts/run_daily.py --daemon            # 常驻调度
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.layer5_scheduler.scheduler import Scheduler


def main() -> None:
    ap = argparse.ArgumentParser(description="第 5 层：日常流水线 / 定时调度")
    ap.add_argument("--daemon", action="store_true", help="常驻调度模式")
    ap.add_argument("--steps", default="",
                    help="逗号分隔环节: update,strategy,risk,monitor（缺省=全部）")
    args = ap.parse_args()

    sched = Scheduler()
    if args.daemon:
        sched.daemon()
    else:
        steps = [s.strip() for s in args.steps.split(",") if s.strip()] or None
        result = sched.run_once(steps=steps)
        sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
