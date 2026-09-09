# -*- coding: utf-8 -*-
"""前端数据 API：读产物 CSV + MySQL 监控 + 因子配置读写。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
# frozen 下产物根与回测脚本共用 src.common.paths 唯一解析，
# 避免"脚本写 A 目录、API 读 B 目录"的分裂（v1.3 事故根因）。
try:
    from src.common.paths import get_outputs_root as _get_outputs_root
    OUT = _get_outputs_root()
except Exception:                                          # noqa: BLE001
    OUT = ROOT / "outputs"
CONFIG_DIR = ROOT / "config"
FACTOR_YAML = CONFIG_DIR / "factors.yaml"

try:
    from src.common.config import CONFIG_DIR as _CFG_DIR
    CONFIG_DIR = _CFG_DIR
    FACTOR_YAML = CONFIG_DIR / "factors.yaml"
    COMBO_YAML = CONFIG_DIR / "combo.yaml"
except Exception:                                          # noqa: BLE001
    COMBO_YAML = CONFIG_DIR / "combo.yaml"

# combo.yaml 读-改-写临界区：save_combo_config / save_custom_factors 并发时防丢失更新
COMBO_LOCK = threading.Lock()


def _persist_combo(raw: dict) -> str | None:
    """备份 + 原子写回 combo.yaml（tmp+replace，写一半崩溃不会截断原文件）。
    返回错误信息，None=成功。调用方必须持有 COMBO_LOCK。"""
    from datetime import datetime as _dt
    bak = CONFIG_DIR / f"combo.yaml.bak_{_dt.now():%Y%m%d_%H%M%S}"
    try:
        import shutil as _shutil
        if COMBO_YAML.exists():
            _shutil.copy2(COMBO_YAML, bak)
        tmp = COMBO_YAML.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(COMBO_YAML)
    except Exception as e:                                 # noqa: BLE001
        return f"写回失败（已备份到 {bak.name}）: {e}"
    return None


# ================================================================ 产物扫描

def _list_runs(dirname: str, prefix: str) -> list:
    """列出某目录下 prefix_*.csv 的运行 tag 列表（按时间倒序）。

    tag 必须以数字开头（%Y%m%d_%H%M）—— 否则 glob 会把同前缀的
    衍生文件（如 factor_ic_series_*) 也当成一次运行，且字符串排序
    会把 'series_...' 排到真实 tag 前面，读错文件。
    """
    d = OUT / dirname
    if not d.exists():
        return []
    tags = set()
    for f in d.glob(f"{prefix}_*.csv"):
        name = f.stem
        tag = name[len(prefix) + 1:] if prefix + "_" in name else ""
        if tag and tag[0].isdigit():
            tags.add(tag)
    return sorted(tags, reverse=True)


def _read_csv(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(path)
    except Exception:                                   # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    # NaN → None：JSON 标准不认 NaN，FastAPI 序列化时直接 500
    return df.astype(object).where(pd.notna(df), None)


def _read_metrics(path: Path) -> dict | None:
    """指标 CSV：指标名作行（index_col=0），数值在第一列。数值强转，转不动再当字符串。"""
    try:
        m = pd.read_csv(path, index_col=0)
    except Exception:                                   # noqa: BLE001
        return None
    if m is None or m.empty:
        return None
    row = m.iloc[:, 0]
    out = {}
    for k, v in row.items():
        if pd.isna(v):
            continue
        try:
            out[str(k)] = round(float(v), 4)
        except (TypeError, ValueError):
            out[str(k)] = str(v)
    return out


# ================================================================ 总览
# v1.2：总览固定展示报告中的最优组合 Combo3（outputs/final5/combo_metrics_*），
#       旧 top50 单因子产物仅作兜底（无 combo 产物时才读）。

def _combo_info(tag: str) -> dict:
    """读取某次组合回测的配置快照（名称/成员/区间）。"""
    cfg_json = OUT / "final5" / f"combo_config_{tag}.json"
    if cfg_json.exists():
        try:
            c = json.loads(cfg_json.read_text(encoding="utf-8"))
            return {"name": c.get("combo", {}).get("name", ""),
                    "members": c.get("combo", {}).get("members", {}),
                    "period": c.get("period", {}),
                    "top_n": c.get("combo", {}).get("top_n"),
                    "max_weight": c.get("combo", {}).get("max_weight")}
        except Exception:                                 # noqa: BLE001
            pass
    return {}


def _overview_combo(tag: str, runs: list) -> dict:
    """总览（Combo3 主线）：组合指标 + 净值 + 配置快照。

    注意：outputs/risk 里的风控双线属于旧 top50 回测，与 Combo3 不是
    同一条净值，混画会误导，故 combo 视图不拼风控线（风控页仍可单独看）。
    """
    out = {"tag": tag, "runs": runs, "source": "combo",
           "locked": True, "combo": _combo_info(tag)}
    m = _read_metrics(OUT / "final5" / f"combo_metrics_{tag}.csv")
    if m:
        out["metrics"] = m
    eq = _read_csv(OUT / "final5" / f"combo_equity_{tag}.csv")
    if eq is not None and not eq.empty:
        eq["trade_date"] = eq["trade_date"].astype(str)
        out["equity"] = eq.to_dict("records")
    return out


def overview() -> dict:
    """最新回测的指标 + 净值曲线。优先 Combo3 组合产物，旧 top50 兜底。"""
    combo_tags = _list_combo_tags()
    if combo_tags:
        return _overview_combo(combo_tags[0], combo_tags)

    tags = _list_runs("top50", "metrics")
    if not tags:
        return {"error": "尚无回测产物"}
    tag = tags[0]
    out = {"tag": tag, "runs": tags, "source": "top50"}

    m = _read_metrics(OUT / "top50" / f"metrics_{tag}.csv")
    if m:
        out["metrics"] = m

    eq = _read_csv(OUT / "top50" / f"equity_{tag}.csv")
    if eq is not None and not eq.empty:
        eq["trade_date"] = eq["trade_date"].astype(str)
        out["equity"] = eq.to_dict("records")

    # 风控双线（equity_plain / equity_risk，同 tag 或最新）
    r_tags = _list_runs("risk", "equity_plain")
    if r_tags:
        rtag = r_tags[0]
        p = _read_csv(OUT / "risk" / f"equity_plain_{rtag}.csv")
        r = _read_csv(OUT / "risk" / f"equity_risk_{rtag}.csv")
        if p is not None and r is not None:
            out["equity_plain"] = p.to_dict("records")
            out["equity_risk"] = r.to_dict("records")
            out["risk_tag"] = rtag
    return out


# ================================================================ 回测明细

def backtests() -> list:
    """回测历史：优先 Combo3 组合回测，无则退回旧 top50。"""
    combo_tags = _list_combo_tags()
    if combo_tags:
        runs = []
        for tag in combo_tags:
            m = _read_metrics(OUT / "final5" / f"combo_metrics_{tag}.csv")
            if not m:
                continue
            info = _combo_info(tag)
            runs.append({
                "tag": tag,
                "名称": info.get("name", ""),
                "总收益率": m.get("总收益率"),
                "年化收益率": m.get("年化收益率"),
                "夏普比率": m.get("夏普比率"),
                "最大回撤": m.get("最大回撤"),
            })
        return runs

    tags = _list_runs("top50", "metrics")
    runs = []
    for tag in tags:
        m = _read_metrics(OUT / "top50" / f"metrics_{tag}.csv")
        if not m:
            continue
        runs.append({
            "tag": tag,
            "名称": "旧 Top50 单因子回测",
            "总收益率": m.get("总收益率"),
            "年化收益率": m.get("年化收益率"),
            "夏普比率": m.get("夏普比率"),
            "最大回撤": m.get("最大回撤"),
        })
    return runs


def backtest_detail(tag: str) -> dict:
    """回测明细：final5 下的 combo tag 读组合产物，否则读旧 top50。"""
    out = {"tag": tag}
    is_combo = (OUT / "final5" / f"combo_metrics_{tag}.csv").exists()
    mdir = OUT / "final5" if is_combo else OUT / "top50"
    m = _read_metrics(mdir / f"{'combo_' if is_combo else ''}metrics_{tag}.csv")
    if m:
        out["metrics"] = m
    if is_combo:
        out["combo"] = _combo_info(tag)
    eq = _read_csv(mdir / f"{'combo_' if is_combo else ''}equity_{tag}.csv")
    if eq is not None and not eq.empty:
        eq["trade_date"] = eq["trade_date"].astype(str)
        out["equity"] = eq.to_dict("records")
    w = _read_csv(mdir / f"{'combo_' if is_combo else ''}weights_{tag}.csv")
    if w is not None and not w.empty:
        w["trade_date"] = w["trade_date"].astype(str)
        out["weights"] = w.head(500).to_dict("records")
    return out


# ================================================================ 因子配置读写

def _handlers() -> list:
    """registry 里已注册的因子算子名（可切换 handler 的白名单）。"""
    try:
        from src.layer3_strategy.factors.registry import FACTOR_REGISTRY
        return sorted(FACTOR_REGISTRY.keys())
    except Exception:                                    # noqa: BLE001
        return []


def factor_config() -> dict:
    """当前 factors.yaml 中可编辑的配置（因子启用/方向/参数 + 合成方式/权重）。"""
    if not FACTOR_YAML.exists():
        return {"error": f"配置文件不存在: {FACTOR_YAML}"}
    raw = yaml.safe_load(FACTOR_YAML.read_text(encoding="utf-8")) or {}
    factors = raw.get("factors", {}) or {}
    comp = raw.get("composite", {}) or {}
    out_factors = {}
    for key, v in factors.items():
        v = v or {}
        out_factors[key] = {
            "name": v.get("name", key),
            "enabled": bool(v.get("enabled", True)),
            "handler": v.get("handler", ""),
            "category": v.get("category", ""),
            "direction": int(v.get("direction", 1)),
            "params": dict(v.get("params", {}) or {}),
            "desc": v.get("desc", ""),
        }
    return {
        "factors": out_factors,
        "composite": {
            "method": comp.get("method", "custom"),
            "weights": dict(comp.get("weights", {}) or {}),
            "fillna": comp.get("fillna", "zero"),
            "orthogonalize": bool(comp.get("orthogonalize", False)),
        },
        "handlers": _handlers(),
    }


def save_factor_config(payload: dict) -> dict:
    """v1.2 起因子集锁定：应用不允许增删/启停/改权任何因子。

    本报告（Combo3）的因子清单与权重是经过样本内检验 + 样本外验证的
    固定配置，界面上任何增减都会让展示结果与报告不可追溯。如确需研究
    性调整，请直接改 config/factors.yaml 后走命令行回测。
    """
    return {"error": "因子集已锁定（v1.2）：应用内不允许添加、删减、启停因子或修改权重。"
                     "如需研究性调整请直接编辑 config/factors.yaml。"}


# ================================================================ 组合回测（final5 因子目录 + combo.yaml）

def _combo_catalog() -> list:
    """5 个达标因子目录（name/desc/子权重/默认持仓），来自 backtest_final5.FACTORS。"""
    try:
        from scripts.backtest_final5 import FACTORS
        out = []
        for f in FACTORS:
            w = dict(f.get("w", {}) or {})
            tot = sum(w.values()) or 1.0
            out.append({
                "name": f["name"],
                "desc": f.get("desc", ""),
                "topn": f.get("topn"),
                "max_weight": f.get("mw"),
                "sub_weights": {k: round(v / tot, 3) for k, v in
                                sorted(w.items(), key=lambda x: -x[1])},
            })
        return out
    except Exception as e:                                # noqa: BLE001
        return [{"error": f"因子目录加载失败: {type(e).__name__}: {e}"}]


# ================================================================ 因子池（基础因子，可自由增减组合）

# 12 个基础因子的元信息（与 backtest_final5.COMP_COLS 对应）
FACTOR_META = {
    "mom":   {"name": "动量",       "category": "价格",  "desc": "过去一年收益（剔除近月），捕捉中期趋势"},
    "rev20": {"name": "短期反转",   "category": "价格",  "desc": "近 20 日收益反转，捕捉超涨超跌回归"},
    "dy":    {"name": "股息率",     "category": "红利",  "desc": "高股息溢价，红利策略核心"},
    "bm":    {"name": "账面市值比", "category": "价值",  "desc": "B/M 价值溢价，便宜的好股票"},
    "ep":    {"name": "盈利收益率", "category": "价值",  "desc": "E/P 价值溢价，估值锚"},
    "size":  {"name": "市值",       "category": "规模",  "desc": "小市值溢价（A 股长期有效）"},
    "roe":   {"name": "ROE",        "category": "质量",  "desc": "盈利能力溢价，质量策略核心"},
    "gm":    {"name": "毛利率",     "category": "质量",  "desc": "毛利率溢价，商业模式优劣代理"},
    "lev":   {"name": "杠杆",       "category": "风险",  "desc": "低杠杆溢价，财务稳健性"},
    "lvol":  {"name": "低波动",     "category": "风险",  "desc": "低波动异象，低波动股票长期跑赢"},
    "tur":   {"name": "换手率",     "category": "交易",  "desc": "低换手溢价，冷门股效应"},
    "illiq": {"name": "非流动性",   "category": "交易",  "desc": "Amihud 非流动性溢价"},
}


def _factor_usage() -> dict:
    """各基础因子在 5 个 preset 配方中的合计权重（0~1，作为流行度参考）。"""
    usage = {}
    for f in _combo_catalog():
        if "error" in f:
            continue
        for k, v in (f.get("sub_weights") or {}).items():
            usage[k] = round(usage.get(k, 0.0) + float(v), 3)
    return usage


def factor_pool() -> dict:
    """因子池：12 个基础因子 + 用户自定义表达式因子 + 元信息 + 配方使用度。"""
    usage = _factor_usage()
    out = []
    for key, meta in FACTOR_META.items():
        out.append({"key": key, **meta, "recipe_usage": usage.get(key, 0.0),
                    "custom": False})
    custom = []
    for f in _custom_factors():
        custom.append({**f, "category": "自定义", "recipe_usage": 0.0,
                       "custom": True})
    return {"factors": out + custom, "catalog": _combo_catalog(),
            "variables": _expr_variables(), "functions": _expr_functions()}


def _custom_factors() -> list:
    """combo.yaml 里用户自建的表达式因子列表。"""
    if not COMBO_YAML.exists():
        return []
    try:
        raw = yaml.safe_load(COMBO_YAML.read_text(encoding="utf-8")) or {}
        lst = ((raw.get("combo", {}) or {}).get("custom_factors")) or []
        return [f for f in lst if isinstance(f, dict) and f.get("key") and f.get("expr")]
    except Exception:                                      # noqa: BLE001
        return []


def _expr_variables() -> dict:
    try:
        from src.common.factor_expr import VARIABLES
        return VARIABLES
    except Exception:                                      # noqa: BLE001
        return {}


def _expr_functions() -> dict:
    try:
        from src.common.factor_expr import FUNCTIONS
        return {k: v[1] for k, v in FUNCTIONS.items()}
    except Exception:                                      # noqa: BLE001
        return {}


def save_custom_factors(payload: dict) -> dict:
    """全量保存用户自定义因子（新增/编辑/删除都由前端传整表）。

    payload: {factors: [{key, name, expr, desc}]}
    每个因子过三关：key 规范 / AST 白名单 / 合成小面板试算。
    删除的因子若仍被 custom_blend 引用，自动从混合中移除。
    """
    if not COMBO_YAML.exists():
        return {"error": f"配置文件不存在: {COMBO_YAML}"}
    try:
        from src.common.factor_expr import (
            validate_expr, validate_key, smoke_test,
        )
    except Exception as e:                                 # noqa: BLE001
        return {"error": f"表达式引擎加载失败: {e}"}

    factors = payload.get("factors")
    if not isinstance(factors, list):
        return {"error": "factors 必须是数组"}
    if len(factors) > 20:
        return {"error": "自定义因子最多 20 个"}

    seen = set()
    clean = []
    for f in factors:
        if not isinstance(f, dict):
            return {"error": "factors 元素必须是对象"}
        key = str(f.get("key", "")).strip()
        name = str(f.get("name", "")).strip() or key
        expr = str(f.get("expr", "")).strip()
        desc = str(f.get("desc", "")).strip()
        try:
            validate_key(key)
            validate_expr(expr)
            smoke_test(expr)
        except ValueError as e:
            return {"error": f"因子 [{name or key}] 校验未通过: {e}"}
        if key in seen:
            return {"error": f"因子 key 重复: {key}"}
        seen.add(key)
        clean.append({"key": key, "name": name, "expr": expr, "desc": desc})

    raw = yaml.safe_load(COMBO_YAML.read_text(encoding="utf-8")) or {}
    combo = raw.setdefault("combo", {}) or {}
    combo["custom_factors"] = clean

    # 清理 custom_blend 里对已删因子的引用
    blend = combo.get("custom_blend") or {}
    if isinstance(blend, dict) and blend:
        dropped = [k for k in blend if k not in seen and k not in FACTOR_META]
        if dropped:
            for k in dropped:
                blend.pop(k)
            combo["custom_blend"] = blend or None

    err = _persist_combo(raw)
    if err:
        return {"error": err}
    return {"ok": True, "factors": clean}


def combo_config() -> dict:
    """当前 combo.yaml + 成员因子目录 + 自定义混合 + 可选区间。"""
    if not COMBO_YAML.exists():
        return {"error": f"配置文件不存在: {COMBO_YAML}"}
    raw = yaml.safe_load(COMBO_YAML.read_text(encoding="utf-8")) or {}
    combo = raw.get("combo", {}) or {}
    period = raw.get("period", {}) or {}
    members = combo.get("members", {}) or {}
    return {
        "name": combo.get("name", ""),
        "method": combo.get("method", "custom"),
        "members": {k: float(v) for k, v in members.items()},
        "custom_blend": {k: float(v) for k, v in (combo.get("custom_blend") or {}).items()},
        "custom_factors": _custom_factors(),
        "top_n": int(combo.get("top_n", 50)),
        "max_weight": float(combo.get("max_weight", 0.05)),
        "period": {"start": str(period.get("start", "2021-01-01")),
                   "end": str(period.get("end", "2025-12-31"))},
        "catalog": _combo_catalog(),
        "factor_meta": FACTOR_META,
        "note": "custom_blend 非空时按自定义基础因子混合回测（成员配方模式被忽略）；"
                "为空时按 members 配方组合回测",
    }


def save_combo_config(payload: dict) -> dict:
    """校验并写回 combo.yaml（备份 + 原子写）。

    两种模式（互斥，custom_blend 优先）：
      - custom_blend: {基础因子key: 权重}  自定义因子混合（因子工作台）
      - members:      {配方名: 权重}       preset 配方组合
    另可改 name / top_n / max_weight / period。
    """
    if not COMBO_YAML.exists():
        return {"error": f"配置文件不存在: {COMBO_YAML}"}
    catalog_names = [f["name"] for f in _combo_catalog() if "name" in f]
    raw = yaml.safe_load(COMBO_YAML.read_text(encoding="utf-8")) or {}
    combo = raw.setdefault("combo", {}) or {}

    # ---- 自定义因子混合 ----
    if "custom_blend" in payload:
        blend = payload.get("custom_blend") or {}
        if not isinstance(blend, dict):
            return {"error": "custom_blend 必须是对象 {因子key: 权重}"}
        custom_keys = {f.get("key") for f in (combo.get("custom_factors") or [])
                       if isinstance(f, dict) and f.get("key")}
        if blend:
            for k, v in blend.items():
                if k not in FACTOR_META and k not in custom_keys:
                    return {"error": f"未知因子: {k}（可用: {sorted(set(FACTOR_META) | custom_keys)}）"}
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return {"error": f"权重 {k} 必须是数字"}
                if fv <= 0:
                    return {"error": f"权重 {k} 必须 > 0（不参与就删掉该项）"}
            combo["custom_blend"] = {k: round(float(v), 4) for k, v in blend.items()}
            combo["name"] = "自定义因子混合"
        else:
            combo["custom_blend"] = None    # 置空 → 回到配方模式

    # ---- 配方成员模式 ----
    new_members = payload.get("members")
    if new_members is not None:
        if not isinstance(new_members, dict) or not new_members:
            return {"error": "members 必须是非空对象 {因子名: 权重}"}
        for k, v in new_members.items():
            if k not in catalog_names:
                return {"error": f"未知成员因子: {k}（可选: {catalog_names}）"}
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return {"error": f"成员权重 {k} 必须是数字"}
            if fv <= 0:
                return {"error": f"成员权重 {k} 必须 > 0"}
        combo["members"] = {k: round(float(v), 4) for k, v in new_members.items()}

    if "method" in payload:
        m = payload["method"]
        if m not in ("custom", "equal_weight"):
            return {"error": f"method 必须是 custom/equal_weight, 收到 {m}"}
        combo["method"] = m
    if not combo.get("custom_blend"):
        if combo.get("method", "custom") == "custom" and combo.get("members"):
            if sum(float(v) for v in combo["members"].values()) <= 0:
                return {"error": "custom 模式下成员权重之和必须 > 0"}

    if "top_n" in payload:
        try:
            tn = int(payload["top_n"])
        except (TypeError, ValueError):
            return {"error": "top_n 必须是整数"}
        if not 5 <= tn <= 300:
            return {"error": "top_n 取值范围 5~300"}
        combo["top_n"] = tn
    if "max_weight" in payload:
        try:
            mw = float(payload["max_weight"])
        except (TypeError, ValueError):
            return {"error": "max_weight 必须是数字"}
        if not 0.005 <= mw <= 0.5:
            return {"error": "max_weight 取值范围 0.005~0.5"}
        combo["max_weight"] = round(mw, 4)
    if "name" in payload and isinstance(payload["name"], str) and payload["name"].strip():
        combo["name"] = payload["name"].strip()

    period = raw.setdefault("period", {}) or {}
    for key in ("start", "end"):
        if key in payload.get("period", {}):
            try:
                pd.Timestamp(str(payload["period"][key]))
            except Exception:                             # noqa: BLE001
                return {"error": f"period.{key} 不是合法日期"}
            period[key] = str(payload["period"][key])
    if "period" in payload and period.get("start") and period.get("end"):
        if str(period["start"]) >= str(period["end"]):
            return {"error": "period.start 必须早于 period.end"}

    err = _persist_combo(raw)
    if err:
        return {"error": err}
    return {"ok": True, "config": combo_config()}


def _list_combo_tags() -> list:
    d = OUT / "final5"
    if not d.exists():
        return []
    prefix = "combo_metrics_"
    tags = {f.stem[len(prefix):] for f in d.glob(prefix + "*.csv") if len(f.stem) > len(prefix)}
    return sorted(tags, reverse=True)


def combo_results() -> dict:
    """最新一次组合回测的指标/净值/调仓明细 + 成员单因子参考绩效。"""
    tags = _list_combo_tags()
    if not tags:
        return {"error": "尚无组合回测产物，请先在「组合回测」页运行"}
    tag = tags[0]
    out = {"tag": tag, "runs": tags}
    m = _read_metrics(OUT / "final5" / f"combo_metrics_{tag}.csv")
    if m:
        out["metrics"] = m
    cfg_json = OUT / "final5" / f"combo_config_{tag}.json"
    if cfg_json.exists():
        try:
            out["config"] = json.loads(cfg_json.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            pass
    eq = _read_csv(OUT / "final5" / f"combo_equity_{tag}.csv")
    if eq is not None and not eq.empty:
        eq["trade_date"] = eq["trade_date"].astype(str)
        out["equity"] = eq.to_dict("records")
    w = _read_csv(OUT / "final5" / f"combo_weights_{tag}.csv")
    if w is not None and not w.empty:
        w = w.copy()
        w["trade_date"] = w["trade_date"].astype(str)
        out["weights"] = w.head(500).to_dict("records")

    # 成员参考：最近一次 final5 统一回测的 5 因子绩效
    f5 = sorted((OUT / "final5").glob("final5_metrics_*.csv"), reverse=True)
    if f5:
        df = _read_csv(f5[0])
        if df is not None and not df.empty:
            cols = [c for c in ("因子", "结构", "年化收益", "夏普", "最大回撤", "超额年化")
                    if c in df.columns]
            out["members_ref"] = df[cols].to_dict("records")
            out["members_ref_tag"] = f5[0].stem[len("final5_metrics_"):]
    return out


# ================================================================ 因子

def factors() -> dict:
    ic_tags = _list_runs("factors", "factor_ic")
    if not ic_tags:
        return {"error": "尚无因子检验产物"}
    tag = ic_tags[0]
    out = {"tag": tag}

    ic = _read_csv(OUT / "factors" / f"factor_ic_{tag}.csv")
    if ic is not None and not ic.empty:
        ic["factor"] = ic["factor"].str.replace("f_", "").str.replace("_score", "")
        out["ic_table"] = ic.to_dict("records")

    ser = _read_csv(OUT / "factors" / f"factor_ic_series_{tag}.csv")
    if ser is not None and not ser.empty:
        ser = ser.copy()
        date_col = ser.columns[0]
        ser[date_col] = ser[date_col].astype(str)
        out["ic_series"] = ser.to_dict("records")

    qr = _read_csv(OUT / "factors" / f"factor_quantile_{tag}.csv")
    if qr is not None and not qr.empty:
        out["quantile"] = qr.to_dict("records")
    return out


# ================================================================ 风控

def risk() -> dict:
    tags = _list_runs("risk", "risk_report")
    if not tags:
        return {"error": "尚无风控产物"}
    tag = tags[0]
    out = {"tag": tag}

    rp = _read_csv(OUT / "risk" / f"risk_report_{tag}.csv")
    if rp is not None and not rp.empty:
        out["report"] = rp.to_dict("records")

    ev = _read_csv(OUT / "risk" / f"risk_events_{tag}.csv")
    if ev is not None and not ev.empty:
        out["events"] = ev.to_dict("records")

    p = _read_csv(OUT / "risk" / f"equity_plain_{tag}.csv")
    r = _read_csv(OUT / "risk" / f"equity_risk_{tag}.csv")
    if p is not None and r is not None:
        out["equity_plain"] = p.to_dict("records")
        out["equity_risk"] = r.to_dict("records")
    return out


# ================================================================ 监控（读库）

def monitor() -> dict:
    from src.layer5_scheduler.monitor import DataMonitor
    dm = DataMonitor()
    r = dm.report()
    if "error" in r:
        return r
    return {
        "latest_trade_date": r["latest_trade_date"],
        "coverage": r["覆盖率"],
        "freshness": r["数据新鲜度"],
        "quality": r["数据质量"],
        "alerts": r["告警"],
        "口径": r["覆盖口径"],
    }


# ================================================================ 任务状态

_TASKS: dict = {}


def task_status(task_id: str) -> dict:
    return _TASKS.get(task_id, {"status": "unknown"})
