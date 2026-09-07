# -*- coding: utf-8 -*-
"""
行业分类（申万）。

数据源：申万宏源官网公开的行业分类变动文件
    https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls
    字段：股票代码 / 计入日期 / 行业代码 / 更新日期
    特点：带「计入日期」，可以做时点对齐 —— 用 T 日所属的行业，而不是最新行业。
          这一条很重要：用当前行业去解释十年前的公司，等于引入未来信息。

为什么不用 akshare 的 stock_industry_clf_hist_sw()：
    该接口内部就是下载这个 xls，但它用默认 SSL 校验，而 swsresearch.com 的
    证书链在本机会验不过（SSLError）。这里直接 requests 下载并关闭证书校验
    （只读公开静态文件，风险可控），拿到的是同一份数据。

行业层级：申万代码 6 位，本项目取
    L1 = 前 2 位（约 38 组，粒度接近申万一级 31 个行业）
    L2 = 前 4 位（约 194 组）
    完整 6 位 = 三级（553 组）
行业名称：官网未公开代码->名称的对照文件（几个候选 URL 均 404），
    故名称用 "SW_L1_44" 形式。行业中性化只需要分组标识，不影响功能。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd
import requests

from src.common.config import PROJECT_ROOT
from src.common.logger import logger
from src.common.utils import normalize_code, to_date

SW_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
CACHE_DIR = PROJECT_ROOT / "data" / "raw"
CACHE_FILE = CACHE_DIR / "sw_classification.parquet"


def _download(force: bool = False) -> pd.DataFrame:
    """下载（或读缓存）申万行业分类文件。"""
    if CACHE_FILE.exists() and not force:
        try:
            df = pd.read_parquet(CACHE_FILE)
            logger.info(f"申万行业分类（缓存）: {len(df)} 行")
            return df
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"缓存读取失败，重新下载: {e}")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    # verify=False 说明：该站点证书链不完整，requests 默认校验会抛 SSLError。
    # 此处仅 GET 公开静态文件，不涉及凭据，风险可接受。
    try:
        resp = requests.get(SW_URL, headers=headers, timeout=60, verify=False)
        resp.raise_for_status()
    except Exception as e:                                # noqa: BLE001
        logger.error(f"申万行业分类下载失败: {type(e).__name__}: {e}")
        return pd.DataFrame()

    df = pd.read_excel(io.BytesIO(resp.content), dtype={"股票代码": "str", "行业代码": "str"})
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(CACHE_FILE, index=False)
    except Exception as e:                                # noqa: BLE001
        logger.debug(f"缓存写入失败（不影响主流程）: {e}")
    logger.info(f"申万行业分类下载完成: {len(df)} 行")
    return df


def fetch_sw_classification(force: bool = False) -> pd.DataFrame:
    """返回标准化后的申万行业分类表。

    列：ts_code / industry_code(L1) / industry_code_l2 / industry_code_l3
        / industry_name / effective_date / source
    """
    raw = _download(force=force)
    if raw is None or raw.empty:
        logger.warning("申万行业分类为空，行业中性化将退化为仅市值中性化")
        return pd.DataFrame(columns=["ts_code", "industry_code", "industry_code_l2",
                                     "industry_code_l3", "industry_name",
                                     "effective_date", "source"])

    df = pd.DataFrame()
    df["ts_code"] = raw["股票代码"].astype(str).map(normalize_code)
    code = raw["行业代码"].astype(str).str.strip()
    df["industry_code_l3"] = code
    df["industry_code_l2"] = code.str[:4]
    df["industry_code"] = code.str[:2]                      # L1，中性化默认用这个
    df["industry_name"] = "SW_L1_" + df["industry_code"]
    df["effective_date"] = pd.to_datetime(raw["计入日期"], errors="coerce").dt.date
    df["source"] = "sw_l1"

    df = df.dropna(subset=["ts_code", "effective_date"])
    df = df.drop_duplicates(subset=["ts_code", "industry_code", "effective_date"])
    df = df.sort_values(["ts_code", "effective_date"]).reset_index(drop=True)

    logger.info(
        f"申万行业分类：{len(df)} 行，{df['ts_code'].nunique()} 只股票，"
        f"L1 分组 {df['industry_code'].nunique()} 个"
    )
    return df[["ts_code", "industry_code", "industry_code_l2", "industry_code_l3",
               "industry_name", "effective_date", "source"]]


def build_industry_snapshot(
    sw_df: pd.DataFrame | None = None,
    trade_dates: Optional[Sequence] = None,
    ts_codes: Optional[Sequence[str]] = None,
    level: str = "industry_code",
) -> pd.DataFrame:
    """构建「每日 × 每股票 × 行业」面板（时点对齐）。

    只取 effective_date <= trade_date 的最新一条，天然无前视偏差。
    若传入 trade_dates，则返回完整面板；否则只返回分类表本身。
    """
    if sw_df is None or sw_df.empty:
        sw_df = fetch_sw_classification()
    if sw_df is None or sw_df.empty:
        return pd.DataFrame()

    sw = sw_df.copy()
    sw["effective_date"] = pd.to_datetime(sw["effective_date"], errors="coerce")
    sw = sw.dropna(subset=["effective_date"]).sort_values(["ts_code", "effective_date"])

    if trade_dates is None:
        return sw

    codes = list(ts_codes) if ts_codes is not None else sorted(sw["ts_code"].unique())
    left = pd.MultiIndex.from_product(
        [pd.to_datetime(pd.Series(list(trade_dates))), codes],
        names=["trade_date", "ts_code"],
    ).to_frame(index=False)

    right = sw[["ts_code", "effective_date", level]].rename(
        columns={"effective_date": "trade_date"}
    )
    panel = pd.merge_asof(
        left.sort_values("trade_date"),
        right.sort_values("trade_date"),
        by="ts_code",
        on="trade_date",
        direction="backward",
        allow_exact_matches=True,
    )
    panel["trade_date"] = panel["trade_date"].dt.date
    panel = panel.rename(columns={level: "industry"})
    panel["industry"] = panel["industry"].fillna("UNKNOWN")
    return panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")

    df = fetch_sw_classification()
    print(df.head(8).to_string())
    print(f"\n总行数 {len(df)}，股票 {df['ts_code'].nunique()} 只，L1 分组 {df['industry_code'].nunique()} 个")

    # 时点对齐演示：同一只股票在不同年份可能属于不同行业
    demo = df[df["ts_code"] == "000001"][["ts_code", "industry_code", "effective_date"]]
    print("\n平安银行(000001) 行业变动历史:")
    print(demo.to_string(index=False))
