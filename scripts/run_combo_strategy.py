# -*- coding: utf-8 -*-
"""组合因子回测执行器（桌面端「组合回测」页的后端）。

读取 config/combo.yaml：
    combo.members: {因子名: 组合权重}   成员必须来自 backtest_final5.FACTORS 目录
    combo.method:  custom | equal_weight
    combo.top_n / combo.max_weight      组合层选股参数
    period.start / period.end           回测区间

面板策略：
    优先读 outputs/final5/panel_cache_2125.pkl（离线可跑，无需 MySQL）；
    区间超出缓存范围或缓存缺失时回源 build_panel()（需要 MySQL，约 4-6 分钟）。
基准：沪深300，读不到（无库/无表）时自动退化为无基准对比。

产物（outputs/final5/，桌面端组合回测页直接读）：
    combo_equity_{tag}.csv    净值曲线
    combo_metrics_{tag}.csv   绩效指标
    combo_weights_{tag}.csv   调仓明细
    combo_config_{tag}.json   本次运行的完整配置快照（可追溯）

用法：
    python scripts/run_combo_strategy.py                 # 全按 combo.yaml
    python scripts/run_combo_strategy.py --start 2021-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config, get_project_root, reload_config  # noqa: E402
from src.common.logger import logger                                       # noqa: E402

OUT_DIR = get_project_root() / "outputs" / "final5"
PANEL_CACHE = OUT_DIR / "panel_cache_2125.pkl"
CACHE_RANGE = ("2021-01-01", "2025-12-31")   # 缓存面板覆盖区间（超出则回源建面板）


def _resolve_frozen_out_dir() -> None:
    """frozen（PyInstaller）时定位真实 outputs 目录（与 web/api.py 同规则）。

    get_project_root() 在 frozen 下指向 exe 同级目录，面板缓存若随包
    放在 exe 目录的 outputs/ 里没问题；若 exe 仍在项目 dist/ 里跑
    （开发场景），缓存实际在项目根 outputs/ —— 逐级向上找一份存在
    的面板缓存，找到即把 OUT_DIR / PANEL_CACHE 重定向过去。
    """
    global OUT_DIR, PANEL_CACHE
    if PANEL_CACHE.exists():
        return
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if len(exe_dir.parents) > 1:
            candidates.append(exe_dir.parents[1] / "outputs" / "final5")  # 项目根
        candidates.append(exe_dir / "outputs" / "final5")                  # exe 同级
    else:
        for p in Path(__file__).resolve().parents:
            if p.name in ("dist", "build"):
                continue
            cand = p / "outputs" / "final5"
            if cand != OUT_DIR:
                candidates.append(cand)
            if len(candidates) >= 3:
                break
    for cand in candidates:
        if (cand / "panel_cache_2125.pkl").exists():
            OUT_DIR = cand
            PANEL_CACHE = cand / "panel_cache_2125.pkl"
            logger.info(f"面板缓存重定向: {PANEL_CACHE}")
            return


def _import_factors_module():
    """导入 backtest_final5（FACTORS 目录 + COMP_COLS + build_panel + 基准）。

    frozen exe 里 scripts 是数据文件目录，靠 _MEIPASS 在 sys.path 上按
    命名空间包导入（与 web/runner.py 导入 run_top50_strategy 同机制）。
    """
    try:
        from scripts.backtest_final5 import COMP_COLS, FACTORS  # noqa: F401
        return sys.modules["scripts.backtest_final5"]
    except Exception:                                        # noqa: BLE001
        sys.path.insert(0, str(ROOT / "scripts"))
        import backtest_final5                               # noqa: F401
        return sys.modules["backtest_final5"]


def load_combo_yaml() -> dict:
    """读 combo.yaml（每次现读，不用 lru_cache —— 桌面端保存后立刻重跑）。"""
    reload_config()
    return get_config("combo")


def normalize_members(cfg: dict, catalog_names: list) -> tuple[dict, str]:
    """校验并归一化成员权重。返回 ({因子名: 归一化权重}, method)。"""
    combo = cfg.get("combo", {}) or {}
    method = combo.get("method", "custom")
    members = dict(combo.get("members", {}) or {})
    if not members:
        raise ValueError("combo.members 为空：至少选择一个成员因子")
    for name in members:
        if name not in catalog_names:
            raise ValueError(f"未知成员因子: {name}（可选: {catalog_names}）")
    if method == "equal_weight":
        w = {k: 1.0 / len(members) for k in members}
        return w, method
    w = {}
    for k, v in members.items():
        try:
            fv = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"成员权重 {k} 不是数字: {v!r}") from e
        if fv <= 0:
            raise ValueError(f"成员权重 {k} 必须 > 0（不参与就把成员删掉）")
        w[k] = fv
    tot = sum(w.values())
    return {k: v / tot for k, v in w.items()}, method


def load_panel(start: str, end: str, fmod) -> pd.DataFrame:
    """优先面板缓存（区间内），否则回源 MySQL 现建。"""
    in_cache = (start >= CACHE_RANGE[0] and end <= CACHE_RANGE[1])
    if PANEL_CACHE.exists() and in_cache:
        logger.info(f"读取面板缓存: {PANEL_CACHE.name}（离线模式，无需数据库）")
        t0 = time.time()
        p = pd.read_pickle(PANEL_CACHE)
        need = set(fmod.COMP_COLS.values())
        missing = need - set(p.columns)
        if missing:
            logger.warning(f"缓存缺少排名列 {sorted(missing)}，回源重建面板")
        else:
            logger.info(f"面板 {len(p):,} 行, 读取耗时 {time.time()-t0:.0f}s")
            return p
    elif not in_cache:
        logger.info(f"区间 {start}~{end} 超出缓存范围 {CACHE_RANGE}，回源 MySQL 建面板"
                    f"（约 4-6 分钟，需要数据库运行中）")
    else:
        logger.info(f"未找到面板缓存 {PANEL_CACHE}，回源 MySQL 建面板"
                    f"（约 4-6 分钟，需要数据库运行中）")
    t0 = time.time()
    p = fmod.build_panel(start, end)
    logger.info(f"面板 {len(p):,} 行, 构建耗时 {time.time()-t0:.0f}s")
    return p


def load_benchmark_safe(start: str, end: str, fmod) -> pd.Series | None:
    """沪深300 基准；数据库不可用时退化为 None（不阻塞离线回测）。"""
    try:
        bench = fmod.load_benchmark(start, end)
        return bench if len(bench) > 0 else None
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"基准数据读取失败（退化为无基准对比）: {type(e).__name__}: {e}")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="组合因子回测（config/combo.yaml 驱动）")
    ap.add_argument("--start", default=None, help="回测起始（默认取 combo.yaml period）")
    ap.add_argument("--end", default=None, help="回测结束（默认取 combo.yaml period）")
    args = ap.parse_args()

    t0 = time.time()
    cfg = load_combo_yaml()
    combo = cfg["combo"]
    period = cfg.get("period", {}) or {}
    start = args.start or str(period.get("start", CACHE_RANGE[0]))
    end = args.end or str(period.get("end", CACHE_RANGE[1]))

    fmod = _import_factors_module()
    _resolve_frozen_out_dir()
    catalog = {f["name"]: f for f in fmod.FACTORS}
    wnorm, method = normalize_members(cfg, sorted(catalog))

    logger.info("===== 组合因子回测 =====  区间 {} ~ {}  组合: {} ({})".format(
        start, end, combo.get("name", "?"), method))
    for k, v in sorted(wnorm.items(), key=lambda x: -x[1]):
        sub = catalog[k]["w"]
        stot = sum(sub.values())
        desc = " ".join(f"{sk}:{sv/stot:.0%}" for sk, sv in
                        sorted(sub.items(), key=lambda x: -x[1]))
        logger.info(f"  成员 {k} 组合权重 {v:.0%} | Top{catalog[k]['topn']} | 子权重 {desc}")

    panel = load_panel(start, end, fmod)
    if panel.empty:
        logger.error("面板为空，终止")
        sys.exit(1)

    # ---------------- 合成分：Σ 组合权重 × 成员得分 ----------------
    val = pd.Series(0.0, index=panel.index)
    for name, cw in wnorm.items():
        sub = catalog[name]["w"]
        stot = sum(sub.values())
        mscore = pd.Series(0.0, index=panel.index)
        for sk, sv in sub.items():
            col = fmod.COMP_COLS.get(sk)
            if col and col in panel.columns:
                mscore += (sv / stot) * panel[col].fillna(0.5)
        val += cw * mscore
    panel["_score"] = val

    # ---------------- 调仓日 + 回测 ----------------
    from src.layer3_strategy.portfolio_backtest import (
        PortfolioBacktester, monthly_rebalance_dates,
    )
    trade_dates = sorted(panel["trade_date"].unique())
    rb = monthly_rebalance_dates(trade_dates, start=start, end=end)
    logger.info(f"调仓日: {len(rb)} 个（每月末最后交易日）")

    scfg = get_config("strategy")
    bt = PortfolioBacktester(
        initial_capital=float(scfg.get("backtest", {}).get("initial_cash", 10_000_000)),
        cost_tier="conservative")
    top_n = int(combo.get("top_n", 50))
    max_w = float(combo.get("max_weight", 0.05))
    weights = bt.build_weights(panel, rb, score_col="_score", top_n=top_n,
                               weighting="float_mv", max_weight=max_w,
                               min_weight=0.001)
    if weights.empty:
        logger.error("权重为空（可能调仓日无候选），终止")
        sys.exit(1)
    bench = load_benchmark_safe(start, end, fmod)
    res = bt.run(panel, weights, start_date=start, benchmark=bench)
    metrics = res["metrics"]
    equity = res["equity_curve"]

    # ---------------- 输出 ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    equity.rename("equity").to_frame().to_csv(
        OUT_DIR / f"combo_equity_{tag}.csv", encoding="utf-8-sig")
    weights.to_csv(OUT_DIR / f"combo_weights_{tag}.csv", index=False,
                   encoding="utf-8-sig")
    pd.DataFrame([metrics]).T.to_csv(
        OUT_DIR / f"combo_metrics_{tag}.csv", encoding="utf-8-sig")
    snapshot = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": start, "end": end},
        "combo": {"name": combo.get("name"), "method": method,
                  "members": wnorm, "top_n": top_n, "max_weight": max_w},
        "member_sub_weights": {k: catalog[k]["w"] for k in wnorm},
        "metrics": {k: (float(v) if isinstance(v, (int, float, np.floating)) else str(v))
                    for k, v in metrics.items()},
    }
    (OUT_DIR / f"combo_config_{tag}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  组合回测完成  {combo.get('name', '?')}    {start} ~ {end}")
    print("=" * 60)
    for k, v in metrics.items():
        if isinstance(v, (int, float, np.floating)):
            print(f"  {k:<12s} {v:>14.4f}")
    print("=" * 60)
    print(f"耗时 {time.time()-t0:.0f}s，产物 tag={tag} 已写入 {OUT_DIR}")


if __name__ == "__main__":
    main()
