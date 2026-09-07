# -*- coding: utf-8 -*-
"""
统一配置加载。

设计要点：
1. 所有配置放 config/*.yaml，敏感信息（数据库密码等）走 .env 环境变量；
2. YAML 中写 ${VAR} 占位，加载时按「环境变量 > defaults 段 > 报错」的顺序插值；
3. 平台无关：项目根目录由 __file__ 推导，Windows / Linux 通用。

用法：
    from src.common.config import get_config
    db_cfg = get_config("database")["mysql"]
"""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote_plus

import yaml
from dotenv import load_dotenv

# 项目根目录：src/common/config.py -> 上三级
# frozen（PyInstaller 打包）时：--add-data 的 config/outputs 是 _MEIPASS 里的
# 只读副本，改配置不持久。重定向到 exe 同级目录（可写、随 exe 分发）；
# 首次运行时把内嵌默认配置复制过去。开发模式（非 frozen）仍是源码目录。
if getattr(sys, "frozen", False):
    _EXE_DIR = Path(sys.executable).resolve().parent
    _INNER = Path(getattr(sys, "_MEIPASS", _EXE_DIR))
    _outer_cfg = _EXE_DIR / "config"
    if not _outer_cfg.exists() and (_INNER / "config").exists():
        try:
            import shutil
            shutil.copytree(_INNER / "config", _outer_cfg, dirs_exist_ok=True)
        except Exception:                                     # noqa: BLE001
            pass
    PROJECT_ROOT = _EXE_DIR
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

# 占位符形如 ${DB_HOST}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_dotenv_once() -> None:
    """加载项目根目录下的 .env（幂等）。"""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)


def get_project_root() -> Path:
    return PROJECT_ROOT


def _resolve_placeholders(node: Any, defaults: Dict[str, Any]) -> Any:
    """递归把字符串里的 ${VAR} 替换成实际值。

    取值顺序：环境变量 > 该 YAML 的 defaults 段 > 原样保留（后面校验会报错）。
    """
    if isinstance(node, dict):
        return {k: _resolve_placeholders(v, defaults) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_placeholders(v, defaults) for v in node]
    if isinstance(node, str):

        def _sub(m: "re.Match[str]") -> str:
            var = m.group(1)
            if var in os.environ and os.environ[var] != "":
                return os.environ[var]
            if var in defaults:
                return str(defaults[var])
            return m.group(0)  # 保留原样，由 validate 阶段报错

        return _PLACEHOLDER.sub(_sub, node)
    return node


def _validate(node: Any, path: str = "") -> None:
    """检查是否还有未解析的 ${VAR}。"""
    if isinstance(node, dict):
        for k, v in node.items():
            _validate(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _validate(v, f"{path}[{i}]")
    elif isinstance(node, str):
        unresolved = _PLACEHOLDER.findall(node)
        if unresolved:
            raise ValueError(
                f"配置项 {path!r} 中的环境变量 {unresolved} 未提供值，"
                f"请在 .env 中设置（可参照 .env.example）"
            )


# ---------------------------------------------------------------- schema 校验
# P1-5：配置写错必须启动即报错，不许静默降级。
# 之前 composite.method 写错会静默退回等权、slip_model 写错会当 fixed ——
# 回测结果悄悄变味，最坑。现在 get_config 后统一过闸门，非法即 sys.exit。

# 枚举白名单
_COMPOSITE_METHODS = {"equal_weight", "custom"}
_SLIP_MODELS = {"liquidity", "fixed", "volume"}
# 校验项：(配置文件名, 相对路径) -> (规则, 参数)
# 注意：cfg 已是该 YAML 的顶层，路径不带文件名前缀
_SCHEMA_RULES = {
    ("factors", "composite.method"): ("enum", _COMPOSITE_METHODS),
    ("costs", "slippage.model"): ("enum", _SLIP_MODELS),
}


def _deep_get(node: Any, path: str) -> Any:
    cur: Any = node
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _require_bool(node: Any, path: str) -> None:
    """enabled 等开关字段必须是布尔，不能是 'true'/1/None。"""
    v = _deep_get(node, path)
    if v is None:
        return
    if not isinstance(v, bool):
        raise ValueError(
            f"配置项 {path!r} 必须是 true/false，当前是 {v!r}（{type(v).__name__}）"
        )


def _validate_weights(node: Any, path: str) -> None:
    """custom 合成权重之和必须 ≈ 1（容差 5%，防手滑写错后权重隐性偏移）。"""
    w = _deep_get(node, path)
    if w is None:
        return
    if not isinstance(w, dict) or not w:
        return
    try:
        total = sum(float(v) for v in w.values())
    except (TypeError, ValueError) as e:
        raise ValueError(f"配置项 {path!r} 权重值必须是数字: {w}") from e
    if abs(total - 1.0) > 0.05:
        raise ValueError(
            f"配置项 {path!r} 权重之和为 {total:.3f}，必须 ≈ 1（当前偏差 >5%）。"
            f"权重写错会导致因子合成比例与预期不符，请核对 weights"
        )


def validate_schema(name: str, cfg: Dict[str, Any]) -> None:
    """对已加载配置做 schema 校验，非法即抛 ValueError（由 get_config 转 sys.exit）。"""
    for (fname, path), (rule, arg) in _SCHEMA_RULES.items():
        if fname != name:
            continue
        v = _deep_get(cfg, path)
        if rule == "enum" and v is not None and v not in arg:
            raise ValueError(
                f"配置项 {path!r} 取值 {v!r} 非法，允许: {sorted(arg)}。"
                f"非法值会触发静默降级（如 slip_model 写错当 fixed），"
                f"回测结果失真，已拒绝启动"
            )

    # 布尔开关校验
    if name == "risk":
        _require_bool(cfg, "enabled")
        for sub in ("stop_loss", "circuit_breaker"):
            _require_bool(cfg, f"{sub}.enabled")
    if name == "factors":
        _validate_weights(cfg, "composite.weights")
        for fname, fc in (cfg.get("factors") or {}).items():
            if isinstance(fc, dict) and "enabled" in fc:
                if not isinstance(fc["enabled"], bool):
                    raise ValueError(
                        f"配置项 factors.{fname}.enabled 必须是 true/false，"
                        f"当前是 {fc['enabled']!r}"
                    )


@lru_cache(maxsize=None)
def get_config(name: str) -> Dict[str, Any]:
    """加载 config/<name>.yaml，返回插值后的字典（带缓存）。"""
    load_dotenv_once()
    cfg_path = CONFIG_DIR / f"{name}.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    defaults = raw.get("defaults", {}) or {}
    resolved = _resolve_placeholders(raw, defaults)
    _validate(resolved)

    # P1-5 schema 校验：配置写错直接拒绝启动（sys.exit），不许静默降级。
    # 用 dict 缓存命中时不会重复校验（配置不变，结果一样），安全。
    try:
        validate_schema(name, resolved)
    except ValueError as e:
        print(f"[配置校验失败] {name}.yaml: {e}", file=sys.stderr)
        sys.exit(f"[配置校验失败] {name}.yaml: {e}")
    return resolved


def get_db_url(hide_password: bool = False) -> str:
    """由 config/database.yaml 拼出 SQLAlchemy 连接串。

    密码必须 URL 编码：MySQL 密码里出现 @ # ! / % 等字符是常态（尤其随机生成的强密码），
    不编码的话这些字符会被当成 URL 分隔符，表现为各种诡异的连接错误，
    比如含中文时报 "latin-1 codec can't encode"。
    """
    mysql = get_config("database")["mysql"]
    pwd = str(mysql["password"] or "")
    encoded = quote_plus(pwd)
    url = (
        f"mysql+pymysql://{quote_plus(str(mysql['user']))}:{encoded}"
        f"@{mysql['host']}:{mysql['port']}/{mysql['database']}"
        f"?charset={mysql.get('charset', 'utf8mb4')}"
    )
    if hide_password:
        url = url.replace(f":{encoded}@", ":******@")
    return url


def reload_config() -> None:
    """清空缓存（测试或 .env 变更后使用）。"""
    get_config.cache_clear()


if __name__ == "__main__":
    # 自检：逐个加载配置文件，确认占位符都能解析
    for f in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = get_config(f.stem)
        print(f"[OK] {f.name:20s} 顶层键: {list(cfg.keys())}")
    print("\nDB URL:", get_db_url(hide_password=True))
