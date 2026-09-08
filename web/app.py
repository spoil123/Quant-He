# -*- coding: utf-8 -*-
"""量化系统 Web 前端（FastAPI 入口）。

启动：
    python web/app.py            # http://localhost:8000
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web import api, runner

app = FastAPI(title="量化交易系统", version="1.7")

STATIC = ROOT / "web" / "static"


# ================================================================ 数据 API

@app.get("/api/overview")
def get_overview():
    return api.overview()


@app.get("/api/backtests")
def get_backtests():
    return api.backtests()


@app.get("/api/backtest/{tag}")
def get_backtest(tag: str):
    return api.backtest_detail(tag)


@app.get("/api/factors")
def get_factors():
    return api.factors()


# v1.3 因子工作台：组合配置可写（custom_blend 自定义因子混合 / members 配方模式）


@app.get("/api/factor-config")
def get_factor_config():
    return api.factor_config()


@app.get("/api/combo-config")
def get_combo_config():
    return api.combo_config()


@app.put("/api/combo-config")
def put_combo_config(payload: dict):
    return api.save_combo_config(payload)


@app.get("/api/factor-pool")
def get_factor_pool():
    return api.factor_pool()


@app.post("/api/custom-factors")
def post_custom_factors(payload: dict):
    """全量保存用户自定义表达式因子（增/改/删都传整表）。"""
    return api.save_custom_factors(payload)


@app.get("/api/combo-results")
def get_combo_results():
    return api.combo_results()


@app.get("/api/risk")
def get_risk():
    return api.risk()


@app.get("/api/monitor")
def get_monitor():
    return api.monitor()


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    return runner.status(task_id)


@app.post("/api/run/{task_name}")
def run_task(task_name: str, params: dict = None):
    return {"task_id": runner.launch(task_name, params or {})}


# ================================================================ 静态页面

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    print("量化系统前端: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
