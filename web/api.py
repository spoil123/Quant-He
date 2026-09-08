# -*- coding: utf-8 -*-
"""前端数据 API：读产物 CSV + MySQL 监控 + 因子配置读写。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
CONFIG_DIR = ROOT / "config"
FACTOR_YAML = CONFIG_DIR / "factors.yaml"

# frozen（PyInstaller）时 __file__ 指向 _MEIPASS 解压目录（只读副本），
# 配置/产物必须走统一重定向：
#   - 配置 → exe 同级 config\（可写持久，src.common.config 已处理）
#   - 产物 → 项目根 outputs\（exe 位于 <项目>/dist/QuantDesktop/，parents[1] 即项目根；
#             若 exe 脱离项目目录独立分发，退回 exe 同级 outputs\）
try:
    from src.common.config import CONFIG_DIR as _CFG_DIR
    CONFIG_DIR = _CFG_DIR
    FACTOR_YAML = CONFIG_DIR / "factors.yaml"
    COMBO_YAML = CONFIG_DIR / "combo.yaml"
    if getattr(sys, "frozen", False):
        _exe_dir = Path(sys.executable).resolve().parent
        _proj = _exe_dir.parents[1] if len(_exe_dir.parents) > 1 else _exe_dir
        if (_proj / "outputs").exists():
            OUT = _proj / "outputs"
        else:
            OUT = _exe_dir / "outputs"
except Exception:                                          # noqa: BLE001
    COMBO_YAML = CONFIG_DIR / "combo.yaml"


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


def combo_config() -> dict:
    """当前 combo.yaml + 成员因子目录 + 可选区间。"""
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
        "top_n": int(combo.get("top_n", 50)),
        "max_weight": float(combo.get("max_weight", 0.05)),
        "period": {"start": str(period.get("start", "2021-01-01")),
                   "end": str(period.get("end", "2025-12-31"))},
        "catalog": _combo_catalog(),
        "locked": True,
        "note": "v1.2 起组合锁定为报告中的最优组合 Combo3 · 动量红利低波，"
                "成员与权重不可增减修改；「重新回测」按当前固定配置运行",
    }


def save_combo_config(payload: dict) -> dict:
    """v1.2 起组合锁定：不允许增删成员/改权重/改持仓参数。

    Combo3（MD-Mom50 + DV-LV50 + GM-LV50 等权，Top50、单票上限 5%）
    是报告中交付且经样本外验证的最优组合，界面层任何改动都会造成
    应用展示与报告数字不可追溯。研究性调整请直接编辑 config/combo.yaml。
    """
    return {"error": "组合已锁定（v1.2）：成员因子与权重固定为报告中的 Combo3，"
                     "不允许添加或删减因子。如需研究性调整请直接编辑 config/combo.yaml。"}


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
