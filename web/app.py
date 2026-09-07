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

app = FastAPI(title="量化交易系统", version="1.0")

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


@app.get("/api/factor-config")
def get_factor_config():
    return api.factor_config()


@app.put("/api/factor-config")
def put_factor_config(payload: dict):
    return api.save_factor_config(payload)


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
