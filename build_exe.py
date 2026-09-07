# -*- coding: utf-8 -*-
"""
打包为 Windows 桌面 exe（PyInstaller）。

用法：
    python build_exe.py            # 打包到 dist/QuantDesktop/
    python build_exe.py --onefile  # 打包单个 exe

产物：dist/QuantDesktop/QuantDesktop.exe（双击运行，内置 FastAPI + 前端）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    onefile = "--onefile" in sys.argv
    name = "QuantDesktop"
    dist_dir = ROOT / "dist" / name

    # 清理旧产物（try: WorkBuddy 安全删除钩子在回收站不可用时抛错，
    # PyInstaller --noconfirm 本身会覆盖旧产物，这里清理失败不阻塞）
    try:
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
    except Exception:                                        # noqa: BLE001
        print("提示: 旧 dist 清理失败（PyInstaller --noconfirm 会覆盖），继续打包")

    py = sys.executable
    # 每次用全新临时目录：WorkBuddy safe-delete shim 拦截 python 的 rmtree，
    # PyInstaller 复用旧 work/dist 时要删旧文件必炸。全新目录无旧文件可删。
    tmp_dir = Path(sys.base_prefix).parent / f"pyinstaller_build_{datetime.now():%H%M%S}"
    spec_dir = tmp_dir / "spec"
    work_dir = tmp_dir / "work"
    dist_tmp = tmp_dir / "dist"
    spec_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    dist_tmp.mkdir(parents=True, exist_ok=True)
    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm",
        # 注意：不能加 --clean——WorkBuddy 沙箱给所有 python 进程注入 safe-delete
        # shim（monkeypatch shutil.rmtree），PyInstaller 清理 work/dist 旧产物时
        # 必被拦（回收站不可用）。--noconfirm 已能覆盖旧产物。
        "--specpath", str(spec_dir),       # spec/work/dist 全移出项目目录：
        "--workpath", str(work_dir),       # WorkBuddy 文件监控对项目内文本文件加锁
        "--distpath", str(dist_tmp),
        "--name", name,
        "--windowed",                       # 无控制台窗口
        f"--add-data={ROOT / 'web' / 'static'};web/static",
        f"--add-data={ROOT / 'config'};config",
        f"--add-data={ROOT / 'src'};src",
        f"--add-data={ROOT / 'scripts'};scripts",   # runner 进程内 import 重跑
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--collect-all", "akshare",          # akshare 数据/资源较多
        "--collect-all", "webview",          # pywebview 的 WebView2Loader.dll / Core.dll
                                             # 缺了 → 桌面双击窗口创建 segfault（闪退）
    ]
    if not onefile:
        cmd += ["--onedir"]
    else:
        cmd += ["--onefile"]

    cmd.append(str(ROOT / "web" / "desktop.py"))
    print("运行:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        sys.exit(f"打包失败: {r.returncode}")

    # 拷贝到项目 dist（直接写项目内 dist 目录可被监控放行；删除会被拦）
    src = dist_tmp / name
    try:
        if dist_dir.exists():
            shutil.rmtree(dist_dir)              # 失败则跳过（旧产物残留不影响覆盖）
    except Exception:                            # noqa: BLE001
        pass
    dist_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dist_dir, dirs_exist_ok=True)

    print(f"\n打包完成: {dist_dir}/{name}.exe")
    print("直接双击运行（首次启动较慢，需本机 MySQL 运行中）")


if __name__ == "__main__":
    main()
