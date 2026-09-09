# -*- coding: utf-8 -*-
"""
量化系统桌面 App（PyWebview 壳）。

双击/命令行运行本文件：
    1. 后台启动 FastAPI（uvicorn，127.0.0.1:8000）
    2. 打开原生桌面窗口（Windows 用系统 WebView2）
    3. 窗口关闭后自动停止服务

开发模式（浏览器）仍可：python web/app.py
打包：python build_exe.py（PyInstaller 单文件 exe）
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
PORT = int(os.environ.get("QUANT_WEB_PORT", "8000"))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def start_server() -> None:
    """后台启动 uvicorn，直到端口就绪。

    frozen（PyInstaller 打包后）环境必须 log_config=None，否则 uvicorn 默认接
    管 logging 时 StreamHandler 会被劫持成 None，崩溃在
    "Cannot log to objects of type 'NoneType'"。
    """
    import logging
    logging.raiseExceptions = False                        # 不抛 logging 异常
    logging.getLogger().handlers = []                       # 清掉 None handler
    import uvicorn
    from web.app import app

    config = uvicorn.Config(
        app, host=HOST, port=PORT,
        log_level="warning",
        log_config=None,                                   # 不接管 logging
        access_log=False,                                  # 关闭访问日志
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if not _port_free(PORT):
            return
        time.sleep(0.3)
    raise RuntimeError(f"FastAPI 启动超时: {HOST}:{PORT}")


def _log(msg: str) -> None:
    """启动日志：frozen 时写 exe 同级 startup.log（用户双击打不开时看这个）。"""
    print(msg, flush=True)
    try:
        if getattr(sys, "frozen", False):
            log_path = Path(sys.executable).resolve().parent / "startup.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:                                      # noqa: BLE001
        pass


class WinApi:
    """提供给前端 JS 的窗口控制（无边框模式下替代原生标题栏按钮）。"""

    @staticmethod
    def _w():
        import webview
        return webview.windows[0]

    def min_win(self) -> None:
        self._w().minimize()

    def max_win(self) -> None:
        self._w().maximize()

    def restore_win(self) -> None:
        self._w().restore()

    def close_win(self) -> None:
        self._w().destroy()


def main() -> None:
    _log(f"QuantDesktop 启动  python={getattr(sys, 'frozen', False)}  port={PORT}")
    try:
        # 若端口被占用（开发模式已在跑），直接复用
        if not _port_free(PORT):
            _log(f"检测到服务已在 {HOST}:{PORT} 运行，直接打开窗口")
        else:
            start_server()
            _log(f"后端已启动: http://{HOST}:{PORT}")

        import webview

        _log("创建原生窗口 (WebView2, 无边框)...")
        webview.create_window(
            "Quant·He 因子工作台",
            f"http://{HOST}:{PORT}",
            width=1280, height=820,
            min_size=(1024, 700),
            frameless=True,            # 去掉原生标题栏，消除与页面的割裂
            easy_drag=False,           # 拖拽由页面内 pywebview-drag-region 接管
            js_api=WinApi(),
        )
        webview.start()
        _log("窗口已关闭")
    except Exception as e:                                 # noqa: BLE001
        import traceback
        _log(f"启动失败: {type(e).__name__}: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
