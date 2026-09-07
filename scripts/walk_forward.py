# -*- coding: utf-8 -*-
"""
Walk-Forward 滚动验证：训练期定权 → 测试期验证（2026-08-31 新增）。

背景：此前只有"样本内(2015-18)定权 → 样本外(2019-23)验证一次"，且权重/参数
在历史里来回倒腾、每次重新拟合。walk-forward 是行业标准纪律：
  - 每个 fold：只用【训练期】数据定因子权重（滚动 IC 比例），【测试期】只验证
  - 测试期结果不回流训练（验证不过 = 红灯，回因子层重来，而不是调参再验）
  - 连续 fold 拼接成完整净值，才是真正可外推的绩效

fold 结构（训练 24 个月 / 测试 12 个月，逐 12 个月滚动）：
  2015-2016 训 → 2017 测；2016-2017 训 → 2018 测；...；2021-2022 训 → 2023 测

内存（P0-1 修复 2026-08-31）：
  此前滚动 IC 模式在 fold5（训练 2019-2020，全市场 ~4400 只）内存溢出从未完整
  跑通。根因：ic_weights 里 add_forward_returns 对【全列宽】训练切片做
  sort_values+copy，宽表（30+ 列 × 240 万行）复制一次即 ~1.5GB 峰值。
  修复：
    1. ic_weights 定权只需 trade_date/ts_code/close + score_cols，
       先做【列裁剪】再 add_forward_returns，训练切片从 30 列降到 ~7 列；
    2. 每个 fold 定权后立即 del + gc.collect()，释放训练切片大块内存。
  add_forward_returns 本身按组 shift 即可，不需要对整个面板先排序再 copy。

缓存（P2-6 修复 2026-08-31）：
  全量面板构建 + 因子计算 + 分块预处理是整脚本最贵的一步（~2-3 分钟）。
  面板内容只依赖数据区间 + factors.yaml 配置，把最终产物（含 score_cols）
  落 parquet 缓存（按 区间+配置 的 hash 分键），命中直接加载。
  依赖 factors.yaml 内容 hash —— 改因子配置自动失效换新键。

用法：
    python scripts/walk_forward.py [--out outputs/walk_forward] [--no-cache]
    python scripts/walk_forward.py --prod    # 用生产配置权重，跳过滚动 IC 定权
产物：outputs/walk_forward/walk_forward_YYYYMMDD_HHMM.csv（每 fold 绩效 + 总绩效）
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.common.config import get_config
from src.common.logger import logger
from src.layer3_strategy.factors.composite import (
    composite_score,
    compute_all_factors,
    preprocess_all_factors,
)
from src.layer3_strategy.factors.evaluation import calc_ic
from src.layer3_strategy.panel import add_forward_returns, build_factor_panel
from src.layer3_strategy.portfolio_backtest import (
    PortfolioBacktester,
    monthly_rebalance_dates,
)

PANEL_START = "2014-04-01"     # 首个训练期(2015)前留 ~271 天 lookback（动量 250+21）+ 缓冲
PANEL_END = "2023-12-31"

# 缓存目录：walk_forward 输出目录下的 cache 子目录
CACHE_SUBDIR = "cache"


def _bare(name: str) -> str:
    """列名 → 裸名：f_reversal_score -> reversal（与 _custom_weighted 的键一致）。"""
    return name.replace("f_", "").replace("_score", "")


def _panel_cache_key(start: str, end: str, adj_type: str,
                     with_financial: bool, with_industry: bool) -> str:
    """面板缓存键：数据区间 + 面板选项 + factors.yaml 全文 hash。

    因子配置变化（权重/方向/开关）必然改变产物，必须换键，否则会用到
    旧配置算出的 score_cols 做合成 —— 结果错得悄无声息。
    """
    fcfg = get_config("factors")
    payload = json.dumps({
        "start": start, "end": end, "adj": adj_type,
        "fin": with_financial, "ind": with_industry,
        "factors": fcfg,
    }, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def load_panel_cached(out_dir: Path) -> tuple[pd.DataFrame, list | None] | None:
    """从缓存加载 (panel, score_cols)。未命中返回 None。"""
    key = _panel_cache_key(PANEL_START, PANEL_END, "qfq", True, True)
    cache_dir = out_dir / CACHE_SUBDIR
    pkl = cache_dir / f"panel_{key}.parquet"
    meta = cache_dir / f"panel_{key}.json"
    if not (pkl.exists() and meta.exists()):
        return None
    try:
        panel = pd.read_parquet(pkl)
        score_cols = json.loads(meta.read_text(encoding="utf-8"))
        logger.info(f"[面板缓存命中] {pkl.name}（{len(panel):,} 行）")
        return panel, score_cols
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"面板缓存读取失败，重新构建: {type(e).__name__}: {e}")
        return None


def save_panel_cached(out_dir: Path, panel: pd.DataFrame, score_cols: list) -> None:
    """面板 + score_cols 落 parquet 缓存。"""
    key = _panel_cache_key(PANEL_START, PANEL_END, "qfq", True, True)
    cache_dir = out_dir / CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        panel.to_parquet(cache_dir / f"panel_{key}.parquet", index=False)
        (cache_dir / f"panel_{key}.json").write_text(
            json.dumps(score_cols, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[面板缓存写入] {cache_dir / f'panel_{key}.parquet'}")
    except Exception as e:                                  # noqa: BLE001
        logger.warning(
            f"面板缓存写入失败（本次执行不受影响，但下次将重新构建 ~2-3 分钟）: "
            f"{type(e).__name__}: {e}")


def build_panel_and_factors(out_dir: Path, use_cache: bool = True) -> tuple[pd.DataFrame, list]:
    """构建全量面板 + 因子 + 分块预处理（带缓存）。"""
    if use_cache:
        cached = load_panel_cached(out_dir)
        if cached is not None:
            return cached

    logger.info(f"构建全量面板 {PANEL_START} ~ {PANEL_END}")
    panel = build_factor_panel(PANEL_START, PANEL_END, with_financial=True,
                               with_industry=True, adj_type="qfq")
    if panel.empty:
        sys.exit("面板为空")
    panel, names = compute_all_factors(panel)
    # 分块预处理：winsorize/标准化/中性化均为【截面】操作（按 trade_date 分组），
    # 按年切块独立处理，峰值内存从 795 万行整体降为单年 ~130 万行。
    # 全量一次 preprocess 会崩（26 列 × 795 万行 consolidate 1.5GB 分配失败）。
    year_col = pd.to_datetime(panel["trade_date"]).dt.year
    chunks, score_cols = [], None
    for yr in sorted(year_col.unique()):
        sub = panel[year_col == yr].copy()
        sub, sc = preprocess_all_factors(sub, names)
        chunks.append(sub)
        if score_cols is None:
            score_cols = sc
        del sub
        gc.collect()
    panel = pd.concat(chunks, ignore_index=True)
    del chunks
    gc.collect()
    logger.info(f"面板 {len(panel):,} 行(分块预处理), 因子 {score_cols}")

    # P2-7：因子有效样本率报告（构建一次，各 fold 复用同一份诊断）
    try:
        from src.layer3_strategy.factors.coverage import factor_coverage_report
        factor_coverage_report(panel, score_cols)
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"因子覆盖率报告失败（不影响主流程）: {type(e).__name__}: {e}")

    save_panel_cached(out_dir, panel, score_cols)
    return panel, score_cols


def ic_weights(panel: pd.DataFrame, score_cols: list, train_s, train_e) -> dict:
    """训练期 IC 比例定权：只保留正 IC 因子，负 IC 置 0。

    【修复 2026-08-31】返回键用【裸名】——此前返回 f_reversal_score 带前缀
    列名，而 composite_score/_custom_weighted 查权重用裸名 reversal，
    每次 lookup miss → 全部权重 0 → 静默退回等权。整个"定权"是假象。

    【内存修复 P0-1】只保留定权需要的列（trade_date/ts_code/close + score_cols）
    再 add_forward_returns。此前对全列宽训练切片排序+复制，fold5 时
    30+ 列 × 240 万行 的一次 copy 就顶爆内存。
    """
    cut = pd.Timestamp(train_e) - pd.Timedelta(days=60)
    need = ["trade_date", "ts_code", "close"] + list(score_cols)
    tr = panel[
        (panel["trade_date"] >= pd.Timestamp(train_s).date())
        & (panel["trade_date"] <= cut.date())
    ][need]
    tr = add_forward_returns(tr, periods=(20,))
    w = {}
    for col in score_cols:
        s = calc_ic(tr, col, "fwd_ret_20d")
        ic = float(s.mean()) if s is not None and len(s) else 0.0
        w[_bare(col)] = max(ic, 0.0)          # 键用裸名，与合成模块对齐
    tot = sum(w.values())
    # 训练切片用完即释放 —— 7 个 fold 串行，不释放会在最后一个 fold 前
    # 把峰值内存顶到崩溃
    del tr
    gc.collect()
    if tot <= 0:
        return {_bare(c): 1.0 / len(score_cols) for c in score_cols}   # 退化等权
    return {k: v / tot for k, v in w.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-Forward 滚动验证")
    ap.add_argument("--out", default="outputs/walk_forward")
    ap.add_argument("--prod", action="store_true",
                    help="用生产配置(custom 权重)替代滚动 IC 定权，验证生产权重的外推表现")
    ap.add_argument("--no-cache", action="store_true",
                    help="跳过面板缓存，强制重新构建（数据更新后手动用一次）")
    args = ap.parse_args()

    scfg = get_config("strategy")
    sel = scfg["selection"]
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 全量面板 + 因子（带缓存；未命中才构建，各 fold 切片复用） ----
    panel, score_cols = build_panel_and_factors(out_dir, use_cache=not args.no_cache)

    # ---- folds：训练 24 个月 → 测试 12 个月 ----
    folds = [
        ("2015-01-01", "2016-12-31", "2017-01-01", "2017-12-31"),
        ("2016-01-01", "2017-12-31", "2018-01-01", "2018-12-31"),
        ("2017-01-01", "2018-12-31", "2019-01-01", "2019-12-31"),
        ("2018-01-01", "2019-12-31", "2020-01-01", "2020-12-31"),
        ("2019-01-01", "2020-12-31", "2021-01-01", "2021-12-31"),
        ("2020-01-01", "2021-12-31", "2022-01-01", "2022-12-31"),
        ("2021-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
    ]

    # --prod 模式：用生产配置（factors.yaml custom 权重）替代滚动 IC 定权，
    # 回答"我生产跑的那套权重，外推到底行不行"
    prod_w = None
    if args.prod:
        fcfg = get_config("factors")
        prod_w = fcfg["composite"].get("weights", {}) or {}
        print(f"生产权重模式: {prod_w}")

    rows = []
    equities = []
    for train_s, train_e, test_s, test_e in folds:
        logger.info(f"fold: 训练 {train_s}~{train_e} → 测试 {test_s}~{test_e}")
        # 权重：--prod 用生产配置，否则训练期滚动 IC
        w = prod_w if prod_w is not None else ic_weights(panel, score_cols, train_s, train_e)
        w_norm = {k: round(v, 4) for k, v in w.items()}
        logger.info(f"  权重: {w_norm}")

        # 测试期【双界截断】[test_s, test_e]——修复：此前只截下界，
        # test_panel 一路取到 2023-12-31，每个 fold 都是"当年选股持有到 2023"
        # 的混合体，绩效被后续年份污染。
        t0 = pd.Timestamp(test_s).date()
        t1 = pd.Timestamp(test_e).date()
        test_panel = panel[(panel["trade_date"] >= t0)
                           & (panel["trade_date"] <= t1)].copy()
        test_panel = test_panel.drop(columns=["fwd_ret_20d"], errors="ignore")
        test_panel["composite_score"] = composite_score(
            test_panel, score_cols,
            factor_cfg={"composite": {"method": "custom", "weights": w}})
        rb = monthly_rebalance_dates(sorted(test_panel["trade_date"].unique()),
                                     start=test_s, end=test_e)
        bt = PortfolioBacktester(
            initial_capital=float(scfg["backtest"]["initial_cash"]))
        weights_df = bt.build_weights(
            test_panel, rb, top_n=int(sel["top_n"]), weighting=sel["weighting"],
            max_weight=float(sel["max_weight"]), min_weight=float(sel["min_weight"]))
        res = bt.run(test_panel, weights_df, start_date=test_s)
        m = res["metrics"]
        eq = res["equity_curve"]
        eq.name = f"fold_{test_s[:4]}"
        equities.append(eq)
        calmar = (m["年化收益率"] / m["最大回撤"]) if m.get("最大回撤") else 0
        rows.append({"训练期": f"{train_s[:4]}-{train_e[:4]}",
                     "测试期": f"{test_s[:4]}-{test_e[:4]}",
                     "权重": str(w_norm),
                     "总收益": round(m["总收益率"], 4),
                     "年化": round(m["年化收益率"], 4),
                     "夏普": round(m["夏普比率"], 4),
                     "回撤": round(m["最大回撤"], 4),
                     "卡玛": round(calmar, 4),
                     "调仓次数": m["调仓次数"]})
        print(f"  {test_s[:4]} 测试期: 收益 {m['总收益率']:.2%}  夏普 {m['夏普比率']:.3f}  "
              f"回撤 {m['最大回撤']:.2%}")

    # ---- 汇总：逐段链接拼接净值 → walk-forward 总绩效 ----
    # 不用 pd.concat：pandas 对多段 datetime index 的 concat 会触发巨大数组
    # 分配（block merge 对齐路径），纯 list 拼接 + to_datetime 重建最稳。
    if equities:
        from src.layer2_backtest.metrics import (annual_return, annual_volatility,
                                                  max_drawdown, sharpe_ratio,
                                                  total_return)
        dates_l: list = []
        vals_l: list = []
        prev = 1.0
        for eq in equities:
            seg = eq.to_numpy() / float(eq.iloc[0]) * prev
            dates_l.extend(list(eq.index))
            vals_l.extend(seg.tolist())
            prev = float(seg[-1])
        total_idx = pd.to_datetime(dates_l)
        tot = pd.Series(vals_l, index=total_idx)
        rows.append({"训练期": "-", "测试期": "WF总绩效(2017-2023)",
                     "权重": "-",
                     "总收益": round(total_return(tot), 4),
                     "年化": round(annual_return(tot), 4),
                     "夏普": round(sharpe_ratio(tot), 4),
                     "回撤": round(max_drawdown(tot), 4),
                     "卡玛": round(annual_return(tot) / max_drawdown(tot), 4)
                     if max_drawdown(tot) else 0,
                     "调仓次数": 0})

    df = pd.DataFrame(rows)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    path = out_dir / f"walk_forward_{tag}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print("\n" + "=" * 90)
    print(df.to_string(index=False))
    print("=" * 90)
    print(f"产物: {path}")


if __name__ == "__main__":
    main()
