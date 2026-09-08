# -*- coding: utf-8 -*-
"""自定义因子表达式引擎（受限安全求值）。

目标：让桌面端用户在「因子工作台」里用一行表达式定义自己的选股因子，
例如  rank(close / ts_mean(close, 60))          价格相对 60 日均线的位置
     rank(_ep + _dy)                            价值+红利合成
     rank(ts_delta(_roe, 60))                   ROE 改善幅度

安全模型：表达式先解析为 Python AST 再做白名单校验——
  * 变量只允许面板数据列（VARIABLES 注册表）；
  * 函数只允许注册表里的截面/时序/数学函数；
  * 不允许属性访问、下标、lambda、comprehension 等任何其他节点。
因此用户输入永远不接触 eval/exec，无法构造注入面。

求值语义：
  * 时序函数 ts_*：按 (ts_code, trade_date) 排序后逐股票滚动计算；
  * 截面函数 rank/zscore：按 trade_date 分组横截面标准化；
  * 表达式最终值统一 rank(pct=True) 到 [0,1]，与 12 个基础因子
    的排名列（_r_*）同量纲，可直接进入合成分加权。
"""

from __future__ import annotations

import ast
import re

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 注册表

# 变量：面板列 → 中文名（供前端提示）。
# 注意：_dy/dv_ttm 在 2125 面板缓存中整列缺失（历史缓存生成问题，_r_dy 排名列
# 不受影响），不注册为表达式变量，防止用户写出必然全空的因子。
VARIABLES = {
    "close": "收盘价（复权）",
    "raw_close": "未复权收盘价",
    "open": "开盘价",
    "amount": "成交额（千元）",
    "float_mv": "流通市值",
    "turnover_rate": "换手率",
    "bps": "每股净资产",
    "eps_ttm": "EPS(TTM)",
    "roe_ttm": "ROE(TTM) 原始值",
    "gross_margin": "毛利率",
    "debt_ratio": "资产负债率",
    "_bm": "账面市值比 B/M",
    "_ep": "盈利收益率 E/P",
    "_roe": "ROE(TTM)",
    "_gm": "毛利率",
    "_lev": "负杠杆(-资产负债率)",
    "_mom": "动量（250日-20日收益）",
    "_ret20": "近20日收益",
    "_ret250": "近250日收益",
    "_vol20": "20日收益波动",
    "_logamt": "对数成交额",
    "_logmv": "对数流通市值",
}

# 函数：名 → (实现类型, 中文说明)
FUNCTIONS = {
    "rank": ("cross", "截面百分位排名 → [0,1]"),
    "zscore": ("cross", "截面 z 分数"),
    "ts_delay": ("ts", "n 日前的值，如 ts_delay(close, 20)"),
    "ts_delta": ("ts", "与 n 日前之差，如 ts_delta(_roe, 60)"),
    "ts_mean": ("ts", "n 日滚动均值"),
    "ts_std": ("ts", "n 日滚动标准差"),
    "abs": ("math", "绝对值"),
    "log": ("math", "自然对数"),
    "sqrt": ("math", "平方根"),
    "sign": ("math", "符号函数"),
}

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


# ---------------------------------------------------------------- 校验

def validate_expr(expr: str) -> None:
    """校验表达式合法性；不合法抛 ValueError（中文消息，可直接给前端）。"""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("表达式不能为空")
    if len(expr) > 500:
        raise ValueError("表达式过长（上限 500 字符）")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e.msg}（位置 {e.offset}）") from None

    def _walk(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            _walk(node.body)
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div,
                                        ast.Pow)):
                raise ValueError("只支持 + - * / ^ 运算符")
            _walk(node.left)
            _walk(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise ValueError("只支持一元正负号")
            _walk(node.operand)
        elif isinstance(node, ast.Name):
            if node.id not in VARIABLES:
                raise ValueError(f"未知变量: {node.id}（可用变量见提示）")
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError("只允许数字字面量")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "<表达式>")
                raise ValueError(f"未知函数: {name}（可用函数见提示）")
            kind = FUNCTIONS[node.func.id][0]
            if kind == "ts":
                if len(node.args) != 2 or not _is_pos_int(node.args[1]):
                    raise ValueError(f"{node.func.id}(变量, n) 的 n 必须是正整数字面量")
            else:
                if len(node.args) != 1:
                    raise ValueError(f"{node.func.id} 只接受 1 个参数")
            for a in node.args:
                _walk(a)
        else:
            raise ValueError(f"表达式包含不支持的语法: {type(node).__name__}")

    _walk(tree)


def _is_pos_int(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value > 0:
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return False
    return False


def validate_key(key: str) -> None:
    if not _KEY_RE.match(key or ""):
        raise ValueError("因子 key 只允许小写字母开头，含小写字母/数字/下划线，≤20 字符")
    if key in VARIABLES or key in FUNCTIONS:
        raise ValueError(f"key 与保留名冲突: {key}")


# ---------------------------------------------------------------- 求值

def eval_factor(panel: pd.DataFrame, expr: str, factor_name: str = "") -> pd.Series:
    """在面板上计算表达式，返回与 panel 同索引、值域 [0,1] 的因子序列。

    要求 panel 含 ts_code / trade_date 列及表达式引用的所有变量列。
    引用的列在面板中整列为空（无数据）时抛 ValueError —— 防止静默产出
    全 NaN 因子把组合候选清零。
    """
    validate_expr(expr)
    label = f"[{factor_name}] " if factor_name else ""
    tree = ast.parse(expr.strip(), mode="eval")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in panel.columns:
            if panel[node.id].notna().sum() == 0:
                raise ValueError(
                    f"{label}变量 {node.id} 在当前面板中没有任何数据"
                    "（可能是离线缓存未包含该列），请换用其他变量")

    # 时序函数需要逐股票时间升序；sort_values 是 cython 快路径（百万行秒级）
    df = panel.sort_values(["ts_code", "trade_date"], kind="stable")

    raw = _eval_node(tree.body, df)

    # 消除量纲：截面百分位 → [0,1]（与 _r_* 基础排名列同量纲）
    scored = raw.groupby(df["trade_date"]).rank(pct=True)
    return scored.reindex(panel.index)


def _eval_node(node: ast.AST, df: pd.DataFrame) -> pd.Series:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, df)
    if isinstance(node, ast.Constant):
        return pd.Series(float(node.value), index=df.index)
    if isinstance(node, ast.Name):
        return pd.to_numeric(df[node.id], errors="coerce")
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand, df)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        l = _eval_node(node.left, df)
        r = _eval_node(node.right, df)
        if isinstance(node.op, ast.Add):
            return l + r
        if isinstance(node.op, ast.Sub):
            return l - r
        if isinstance(node.op, ast.Mult):
            return l * r
        if isinstance(node.op, ast.Div):
            return l.replace(0, np.nan) / r.replace(0, np.nan)
        if isinstance(node.op, ast.Pow):
            return l ** r
    if isinstance(node, ast.Call):
        fname = node.func.id
        kind = FUNCTIONS[fname][0]
        if kind == "cross":
            x = _eval_node(node.args[0], df)
            g = x.groupby(df["trade_date"])
            if fname == "rank":
                return g.rank(pct=True)
            m = g.transform("mean")
            s = g.transform("std").replace(0, np.nan)
            return (x - m) / s
        if kind == "ts":
            x = _eval_node(node.args[0], df)
            n = node.args[1].value
            g = x.groupby(df["ts_code"], sort=False)
            if fname == "ts_delay":
                return g.shift(n)
            if fname == "ts_delta":
                return x - g.shift(n)
            # 按股票分组的 cython rolling（groupby.rolling 快路径）
            gr = x.groupby(df["ts_code"], sort=False).rolling(n, min_periods=n)
            out = gr.mean() if fname == "ts_mean" else gr.std()
            return out.reset_index(level=0, drop=True)
        if kind == "math":
            x = _eval_node(node.args[0], df)
            if fname == "abs":
                return x.abs()
            if fname == "log":
                return np.log(x.where(x > 0))
            if fname == "sqrt":
                return np.sqrt(x.where(x >= 0))
            if fname == "sign":
                return np.sign(x)
    raise ValueError(f"表达式包含不支持的语法: {type(node).__name__}")


# ---------------------------------------------------------------- 试算

def smoke_test(expr: str) -> pd.Series:
    """在合成小面板上试算表达式，返回得分序列（抛异常=不合法）。"""
    rng = np.random.default_rng(7)
    days = pd.to_datetime(["2024-01-%02d" % d for d in range(1, 11)]).date
    codes = ["%06d.SZ" % (i * 1111) for i in range(1, 6)]
    rows = []
    for d in days:
        for c in codes:
            r = {k: float(rng.uniform(1, 100)) for k in VARIABLES}
            r.update({"ts_code": c, "trade_date": d})
            rows.append(r)
    panel = pd.DataFrame(rows)
    return eval_factor(panel, expr)
