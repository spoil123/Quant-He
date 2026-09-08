# -*- coding: utf-8 -*-
"""给离线面板缓存补真实行业映射（一次性修复）。

背景：panel_cache_2125.pkl 当初生成时 industry 列全是 'UNKNOWN'，
导致 RiskManager 行业上限把全组合当唯一行业砍到 25%（75% 被动现金）。
本脚本从申万分类表按「有效日 <= 交易日」取最新行业（无前视），就地回写缓存。

用法：
    python scripts/patch_panel_industry.py [缓存路径1 缓存路径2 ...]
    不带参数 = 修补项目与 release 两份默认缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.logger import logger
from src.common.paths import get_final5_dir, PANEL_CACHE_NAME
from src.layer1_data.fetcher.industry import build_industry_snapshot


def patch_cache(path: Path, sw: pd.DataFrame) -> None:
    logger.info(f"修补 {path}")
    panel = pd.read_pickle(path)
    logger.info(f"  面板 {len(panel):,} 行，原 industry 唯一值: "
                f"{panel['industry'].unique()[:5].tolist()}")

    # 每只股票的有效期序列 → searchsorted 逐股票回填（无前视：effective<=trade_date）
    sw = sw.sort_values(["ts_code", "effective_date"])
    by_code = {c: g for c, g in sw.groupby("ts_code")}
    codes = panel["ts_code"].to_numpy()
    dates = pd.to_datetime(panel["trade_date"]).astype("int64").to_numpy()
    out = np.full(len(panel), "UNKNOWN", dtype=object)
    miss_codes = set()
    for code, idx in panel.groupby("ts_code", sort=False).indices.items():
        g = by_code.get(code)
        if g is None or g.empty:
            miss_codes.add(code)
            continue
        eff = g["effective_date"].astype("int64").to_numpy()
        pos = np.searchsorted(eff, dates[idx], side="right") - 1
        ok = pos >= 0
        rows = g.iloc[pos[ok]]
        out[np.asarray(idx)[ok]] = rows["industry_code"].to_numpy()

    panel["industry"] = out
    known = float((panel["industry"] != "UNKNOWN").mean())
    logger.info(f"  回填后真实行业覆盖率 {known:.1%}（无映射代码 {len(miss_codes)} 只）")
    tmp = path.with_suffix(".pkl.tmp")
    panel.to_pickle(tmp)
    tmp.replace(path)
    logger.info(f"  已写回 {path}")


def main() -> None:
    paths = [Path(a) for a in sys.argv[1:]] or \
            [get_final5_dir() / PANEL_CACHE_NAME,
             ROOT / "release" / "Quant-He桌面版" / "outputs" / "final5" / PANEL_CACHE_NAME]
    sw = build_industry_snapshot()
    if sw is None or sw.empty:
        sys.exit("申万分类表为空（检查 MySQL）")
    sw = sw.rename(columns={"effective_date": "effective_date"})
    sw["effective_date"] = pd.to_datetime(sw["effective_date"], errors="coerce")
    sw = sw.dropna(subset=["effective_date"])
    logger.info(f"申万分类 {len(sw):,} 条，覆盖 {sw['ts_code'].nunique()} 只股票")
    for p in paths:
        if p.exists():
            patch_cache(p, sw)
        else:
            logger.warning(f"跳过不存在的缓存: {p}")


if __name__ == "__main__":
    main()
