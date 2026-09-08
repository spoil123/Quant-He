# -*- coding: utf-8 -*-
"""统一路径解析：项目根 / 产物根（outputs）。

背景：frozen（PyInstaller）exe 可能在三种位置运行——
  1. 独立分发目录（release/Quant-He桌面版/）：config 与 panel 缓存随包，
     读写全部走 exe 同级目录；
  2. 开发项目 dist/QuantDesktop/：真实缓存与历史产物在项目根 outputs/；
  3. exe 同级已有 config 但缓存缺失：回源重建后写在 exe 同级 outputs。

此前 web/api.py 与 scripts/run_combo_strategy.py 各写了一套推断规则，
在场景 1 下读写下分裂（API 读项目根、脚本写 exe 同级），导致桌面端
"回测成功但结果面板永远显示旧数据"。本模块是唯一事实来源，两端共用。
"""

from __future__ import annotations

import sys
from pathlib import Path

PANEL_CACHE_NAME = "panel_cache_2125.pkl"


def get_project_root() -> Path:
    """frozen → exe 同级目录（可写、随包分发）；开发 → 源码根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_outputs_root() -> Path:
    """产物根目录：exe/项目根下的 outputs/。

    开发场景特例：exe 位于 <项目>/dist/<名>/ 且 exe 同级没有面板缓存、
    而项目根有 —— 说明这是"在项目里临时跑打包产物"，真实缓存与历史
    产物都在项目根，重定向过去（与 run_combo_strategy 原逻辑一致）。
    """
    root = get_project_root() / "outputs"
    if getattr(sys, "frozen", False):
        exe_dir = get_project_root()
        if (root / "final5" / PANEL_CACHE_NAME).exists():
            return root
        if len(exe_dir.parents) > 1:
            proj = exe_dir.parents[1]
            if (proj / "outputs" / "final5" / PANEL_CACHE_NAME).exists():
                return proj / "outputs"
    return root


def get_final5_dir() -> Path:
    return get_outputs_root() / "final5"


def get_panel_cache() -> Path:
    return get_final5_dir() / PANEL_CACHE_NAME
