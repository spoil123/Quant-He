# -*- coding: utf-8 -*-
"""pytest 全局配置：把项目根目录加入 sys.path，测试可直接 import src.*。

P1-7：单元测试不依赖数据库/网络 —— 全部用内存 DataFrame 构造输入，
保证「改代码敢跑测试」的快速反馈闭环。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
