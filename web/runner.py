# -*- coding: utf-8 -*-
"""调参重跑：后台线程执行策略脚本（进程内 import 调用，非 subprocess）。

2026-08-31 改造：原实现 subprocess 调 scripts/*.py——PyInstaller 打包后
没有独立 python、scripts 也未打包，exe 里"重跑"是死的。改为进程内
importlib 调用 main()（脚本均有 __main__ 保护），线程 + 全局锁保证
同一时刻只跑一个任务。开发模式与 frozen 模式行为一致。
"""

from __future__ import annotations

import importlib
import sys
import threading
import uuid
from pathlib import Path

from src.common.logger import logger

ROOT = Path(__file__).resolve().parents[1]
_TASKS: dict = {}
_TASK_LOCK = threading.Lock()

# task_name -> (模块路径, 额外固定参数)
_TASK_MODULES = {
    "backtest": "scripts.run_top50_strategy",
    "risk": "scripts.example_risk",
    "factors": "scripts.factor_evaluation",
    "combo": "scripts.run_combo_strategy",
}


def _build_argv(task_name: str, params: dict) -> list:
    """拼脚本 argv。combo 的默认区间来自 combo.yaml（面板缓存范围），
    老任务的默认区间维持 2019-2023 不变。"""
    start = params.get("start")
    end = params.get("end")
    if task_name == "combo":
        argv = []
        if start:
            argv += ["--start", str(start)]
        if end:
            argv += ["--end", str(end)]
        return argv
    return ["--start", str(start or "2019-01-01"),
            "--end", str(end or "2023-12-31")]


def _run_inprocess(task_id: str, module_path: str, argv: list) -> None:
    """进程内运行脚本 main()：临时替换 sys.argv，跑完恢复。"""
    saved_argv = sys.argv[:]
    sys.argv = [module_path] + argv
    try:
        mod = importlib.import_module(module_path)
        try:
            mod.main()
            rc = 0
        except SystemExit as e:                     # 脚本 sys.exit(1)
            rc = int(e.code) if e.code else 0
        _TASKS[task_id] = {
            "status": "done" if rc == 0 else "failed",
            "returncode": rc,
            "log_tail": [],
            "error": "",
        }
    except Exception as e:                          # noqa: BLE001
        _TASKS[task_id] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    finally:
        sys.argv = saved_argv


def launch(task_name: str, params: dict) -> str:
    """启动后台任务，返回 task_id。task_name: backtest | risk | factors"""
    task_id = uuid.uuid4().hex[:8]
    module_path = _TASK_MODULES.get(task_name)
    if not module_path:
        _TASKS[task_id] = {"status": "failed", "error": f"unknown task {task_name}"}
        return task_id

    _TASKS[task_id] = {"status": "running", "task": task_name}
    argv = _build_argv(task_name, params)
    if task_name == "backtest":
        argv += ["--out", str(params.get("out", "outputs/top50"))]
    elif task_name == "risk":
        argv += ["--out", str(params.get("out", "outputs/risk"))]

    logger.info(f"前端触发后台任务 {task_name}: {task_id} argv={argv}")

    def _run():
        with _TASK_LOCK:                             # 同一时刻只跑一个任务
            _run_inprocess(task_id, module_path, argv)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return task_id


def status(task_id: str) -> dict:
    return _TASKS.get(task_id, {"status": "unknown"})
