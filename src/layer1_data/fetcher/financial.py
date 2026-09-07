# -*- coding: utf-8 -*-
"""
财务数据抓取与时点对齐。

三级数据源（P1-4，逐层解耦）：
  1. 主源  同花顺 stock_financial_analysis_indicator（akshare 生态）
  2. 备源1 新浪 stock_financial_abstract（akshare 生态，带真实公告日）
  3. 备源2 baostock（独立生态，见 baostock_client.py）—— 前两层都在 akshare
     生态内，akshare 整体不可用时财务会全断；baostock 是独立的库与服务端，
     兜住这最后一环。代价：无每股净资产，BP 因子降级时会缺失。

一个必须说清楚的限制：
    该接口只提供「每股指标」和「比率」，不提供净利润、总资产、所有者权益的
    绝对金额。这反而让估值因子的实现变简单了 ——

        EP（盈利收益率）= 净利润 / 总市值 = (净利润/股本) / (市值/股本) = EPS / 股价
        BP（账面市值比）= 净资产 / 总市值   = BPS / 股价

    分子分母的股本约掉了，所以算 EP/BP 不需要知道总股本，也不依赖任何
    市值接口。这比"先取净利润再取市值"的路子更稳（少两个数据源依赖）。

    代价：算不出真实的净利润绝对额。本项目不用绝对额因子，故无影响。

时点对齐：
    接口只给报告期，不给公告日。用 src.common.pit 的法定最迟披露日作为数据
    可用日（保守策略），宁可晚用不早用。

ROE 的 TTM 处理：
    ROE 是比率，不是累计额，不能套用累计口径的 TTM 公式。
    A 股各期 ROE 是「年初至今」口径，年化方式：ROE_ttm = ROE_累计 × 12 / 报告期月数
    （Q1 ×4，半年 ×2，Q3 ×4/3，年报 ×1）
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import List, Optional

import pandas as pd

from src.common.logger import logger
from src.common.pit import add_ann_date, compute_ttm, report_type_of
from src.common.utils import normalize_code, to_date
from src.layer1_data.fetcher.akshare_client import get_client

# 中文字段 -> 英文
_FIELD_MAP = {
    "日期": "report_date",
    "摊薄每股收益(元)": "eps",
    "加权每股收益(元)": "eps_weighted",
    "扣除非经常性损益后的每股收益(元)": "eps_deducted",
    "每股净资产_调整后(元)": "bps",
    "每股净资产_调整前(元)": "bps_raw",
    "每股资本公积金(元)": "capital_reserve_ps",
    "每股未分配利润(元)": "undistributed_ps",
    "每股经营性现金流(元)": "ocf_ps",
    "净资产收益率(%)": "roe",
    "加权净资产收益率(%)": "roe_weighted",
    "销售毛利率(%)": "gross_margin",
    "销售净利率(%)": "net_margin",
    "主营业务收入增长率(%)": "revenue_growth",
    "净利润增长率(%)": "net_profit_growth",
    "总资产增长率(%)": "asset_growth",
    "应收账款周转率(次)": "ar_turnover",
    "存货周转率(次)": "inv_turnover",
    "总资产周转率(次)": "asset_turnover",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "资产负债率(%)": "debt_ratio",
    "主营业务利润(元)": "main_profit",
}

# 报告期 -> 年化系数（把年初至今口径的年化）
_ANNUALIZE_MONTHS = {
    (3, 31): 3,
    (6, 30): 6,
    (9, 30): 9,
    (12, 31): 12,
}

# 备源：新浪财务摘要（stock_financial_abstract）。
# P1-4：同花顺接口是单点 —— 被封/改版 = 财务全停。新浪摘要字段集不同但
# 覆盖本项目需要的核心列（eps/bps/roe/gross_margin/debt_ratio），且自带
# 真实公告日（公告日期列），时点对齐比主源"法定最迟披露日"更精确。
_SINA_FIELD_MAP = {
    "报告期": "report_date",
    "公告日期": "ann_date",
    "基本每股收益": "eps",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
    "资产负债率": "debt_ratio",
}

# 备源接口名（传给 AKShareClient.call / call_batch 的 fallbacks）
_SINA_FALLBACK = ("stock_financial_abstract",)


def _parse_cn_date(s) -> Optional[date]:
    """解析 2015-03-31 / 2015年03月31日 两种格式 -> date。"""
    if s is None or pd.isna(s):
        return None
    s = str(s).strip()
    if not s:
        return None    # 空串：pd.to_datetime("") 返回 NaT 而非抛错，会漏过上面的判空
    m = re.match(r"(\d{4})[-年/]?(\d{1,2})[-月/]?(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        return pd.to_datetime(s).date()
    except Exception:                                       # noqa: BLE001
        return None


def _annualize_factor(report_date) -> float:
    """年化系数 = 12 / 报告期涵盖月数。"""
    d = to_date(report_date)
    if d is None:
        return 1.0
    months = _ANNUALIZE_MONTHS.get((d.month, d.day))
    if months is None:
        return 1.0
    return 12.0 / months


def _normalize_financial(raw: pd.DataFrame, code: str,
                         source: str = "ths") -> pd.DataFrame:
    """把主源（同花顺）/备源（新浪）的原始返回统一成入库格式。

    差异点：
      - 字段映射表不同（_FIELD_MAP vs _SINA_FIELD_MAP）
      - 时点对齐策略：主源无公告日 -> 保守（法定最迟披露日）；
        备源带真实公告日 -> precise（真实公告日，缺失才用法定日兜底）
      - 报告期/公告日格式：主源"2015-03-31"，备源"2015年03月31日"
    """
    field_map = _SINA_FIELD_MAP if source == "sina" else _FIELD_MAP
    df = raw.rename(columns={k: v for k, v in field_map.items() if k in raw.columns})
    df = df.loc[:, ~df.columns.duplicated()]

    if "report_date" not in df.columns:
        return pd.DataFrame()

    df["ts_code"] = code
    df["report_date"] = df["report_date"].map(_parse_cn_date)
    df = df.dropna(subset=["report_date"]).drop_duplicates(subset=["report_date"], keep="first")
    df = df.sort_values("report_date").reset_index(drop=True)

    # 数值化（日期列除外）
    for c in df.columns:
        if c not in ("ts_code", "report_date", "ann_date"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # 时点对齐
    strategy = "precise" if source == "sina" else "conservative"
    df = add_ann_date(df, report_col="report_date", strategy=strategy)

    # TTM
    if "eps" in df.columns:
        df["eps_ttm"] = compute_ttm(df, "eps", report_col="report_date")
    if "roe" in df.columns:
        factors = df["report_date"].map(_annualize_factor)
        df["roe_ttm"] = df["roe"] * factors

    for c in ("gross_margin", "debt_ratio"):
        if c not in df.columns:
            df[c] = None
    return df


def _source_of(raw: pd.DataFrame) -> str:
    """判断原始数据来自主源还是备源（AKShareClient 降级时标注 attrs._source）。"""
    return "sina" if raw.attrs.get("_source") == "stock_financial_abstract" else "ths"


_REPAIR_MARGIN = os.getenv("FIN_REPAIR_MARGIN", "1") not in ("0", "false", "False", "")


def _repair_gross_margin(df: pd.DataFrame, code: str,
                         min_missing_rate: float = 0.5) -> pd.DataFrame:
    """主源毛利率缺失时，用 baostock 的 gpMargin 按报告期回填。

    为什么需要（2026-09-07 实测，SQL 证据）：
        财务表 gross_margin 缺失率按报告期分布为 2015-2018 年 11-15%、
        2019 年 58%、**2020 年起 100%**；同期 bps/roe/eps/debt_ratio 缺失
        率只有 5-8%。排除"整体降级到 baostock"后定位到根因：同花顺
        stock_financial_analysis_indicator 的「销售毛利率(%)」自 2020 年起
        整列返回 NaN（上游停更，不是字段名变化——同批的「主营业务成本率(%)」
        等也一样空，无法用其他列反推）。

        危害是静默的：任何使用毛利率的因子在近 6 年回测里退化成常数，
        不报错、不告警，但因子排名完全失效。

    修复：baostock query_profit_data 提供 gpMargin（销售毛利率），
    已由 baostock_client._BS_FIELD_MAP 映射为 gross_margin 并统一为百分数口径，
    可直接按 report_date 对齐回填，只补缺失行、不覆盖主源已有值。

    门控：缺失率低于 min_missing_rate 不发起网络请求（零星缺失通常是该股
    本身无数据，补了也补不到）；.env 的 FIN_REPAIR_MARGIN=0 可整体关闭。
    """
    if not _REPAIR_MARGIN or df is None or df.empty:
        return df
    if "gross_margin" not in df.columns or "report_date" not in df.columns:
        return df

    missing = df["gross_margin"].isna()
    if not missing.any() or missing.mean() < min_missing_rate:
        return df

    years = [str(d)[:4] for d in df.loc[missing, "report_date"] if d is not None]
    years = [y for y in years if y.isdigit()]
    if not years:
        return df

    try:
        from src.layer1_data.fetcher.baostock_client import get_baostock
        raw = get_baostock().query_financial(code, start_year=min(years))
    except Exception as e:                                # noqa: BLE001
        logger.debug(f"{code} 毛利率补齐跳过（baostock 不可用）: {type(e).__name__}: {e}")
        return df

    if raw is None or raw.empty or "gross_margin" not in raw.columns:
        return df

    src = raw[["report_date", "gross_margin"]].copy()
    src["report_date"] = pd.to_datetime(src["report_date"], errors="coerce").dt.date
    src = src.dropna(subset=["report_date", "gross_margin"])
    if src.empty:
        return df
    src = src.drop_duplicates("report_date").set_index("report_date")["gross_margin"]

    tgt = pd.to_datetime(df["report_date"], errors="coerce").dt.date
    filled = tgt.map(src)
    take = missing & filled.notna()
    n = int(take.sum())
    if n:
        df.loc[take, "gross_margin"] = filled[take]
        logger.info(f"{code} 毛利率用 baostock 补齐 {n}/{int(missing.sum())} 个缺失报告期")
    return df


def _fetch_baostock_financial(code: str, start_year: str = "2014") -> pd.DataFrame:
    """跨生态降级：akshare（同花顺 + 新浪）都拿不到时，用 baostock 兜底。

    与 akshare 生态内两个源的差异（下游必须知道的）：
      · 自带真实公告日(pubDate) -> precise 时点对齐，比主源的法定最迟披露日更精确
      · eps 只有 TTM 口径(epsTTM)，没有单期 eps —— eps_ttm 直接用，eps 留空
      · 没有每股净资产，bps 为空 —— BP 因子在降级样本上会缺失，
        由因子覆盖率报告发现并告警，不会静默算错
      · roe 需年化（baostock 的 roeAvg 是年初至今累计口径）
    """
    try:
        from src.layer1_data.fetcher.baostock_client import get_baostock
        raw = get_baostock().query_financial(code, start_year=start_year)
    except Exception as e:                                # noqa: BLE001
        logger.error(f"{code} baostock 备源不可用: {type(e).__name__}: {e}")
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    try:
        df = add_ann_date(raw, report_col="report_date", strategy="precise")
    except Exception as e:                                # noqa: BLE001
        logger.error(f"{code} baostock 时点对齐失败: {type(e).__name__}: {e}")
        return pd.DataFrame()

    df["ts_code"] = code
    if "report_type" not in df.columns:
        df["report_type"] = df["report_date"].map(report_type_of)

    # ROE 年化（与主源口径一致：年初至今 -> 年化）
    if "roe" in df.columns:
        df["roe_ttm"] = df["roe"] * df["report_date"].map(_annualize_factor)

    # 结构对齐：缺失列显式留空，保证入库表结构稳定（不是 0，是"没有"）
    for c in ("eps", "bps", "roe", "roe_ttm", "eps_ttm", "gross_margin",
              "debt_ratio", "net_profit", "net_profit_ttm", "total_revenue",
              "total_revenue_ttm", "total_assets", "total_equity"):
        if c not in df.columns:
            df[c] = None

    logger.info(f"{code} 财务数据来自备源(baostock，独立于 akshare 生态)")
    keep = ["ts_code", "report_date", "report_type", "ann_date", "ann_date_source",
            "eps", "eps_ttm", "bps", "roe", "roe_ttm", "gross_margin", "debt_ratio",
            "net_profit", "net_profit_ttm", "total_revenue", "total_revenue_ttm",
            "total_assets", "total_equity"]
    return df[[c for c in keep if c in df.columns]].reset_index(drop=True)


def fetch_financial(
    ts_code: str,
    start_year: str = "2014",
) -> pd.DataFrame:
    """抓单只股票的财务指标，并补上 ann_date / TTM。

    P1-4 三级降级：主源（同花顺）-> 备源1（新浪，同为 akshare 生态）
    -> 备源2（baostock，脱离 akshare 生态）。

    返回列：ts_code / report_date / ann_date / report_type / eps / eps_ttm
            / bps / roe / roe_ttm / gross_margin / ...
    """
    code = normalize_code(ts_code)
    client = get_client()
    try:
        raw = client.call("stock_financial_analysis_indicator", symbol=code,
                          start_year=start_year, fallbacks=_SINA_FALLBACK)
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"{code} akshare 财务源（同花顺+新浪）均不可用: "
                       f"{type(e).__name__}: {e} —— 降级 baostock（跨生态）")
        return _fetch_baostock_financial(code, start_year)

    if raw is None or raw.empty:
        logger.warning(f"{code} akshare 财务源返回空 —— 降级 baostock（跨生态）")
        return _fetch_baostock_financial(code, start_year)

    df = _normalize_financial(raw, code, _source_of(raw))
    if df.empty:
        logger.warning(f"{code} 财务数据解析为空，实际字段: {list(raw.columns)[:5]}")
        return pd.DataFrame()
    if _source_of(raw) == "sina":
        logger.info(f"{code} 财务数据来自备源(新浪，含真实公告日)")

    # 主源 2020 年后毛利率整列 NaN（上游停更）-> 用 baostock 的 gpMargin 回填，
    # 只补缺失报告期，不覆盖主源已有值。见 _repair_gross_margin 文档。
    df = _repair_gross_margin(df, code)

    # 接口不给绝对额，这些列留空占位，保持表结构稳定
    for c in ("net_profit", "net_profit_ttm", "total_revenue", "total_revenue_ttm",
              "total_assets", "total_equity"):
        if c not in df.columns:
            df[c] = None

    keep = ["ts_code", "report_date", "report_type", "ann_date", "ann_date_source",
            "eps", "eps_ttm", "bps", "roe", "roe_ttm", "gross_margin", "debt_ratio",
            "net_profit", "net_profit_ttm", "total_revenue", "total_revenue_ttm",
            "total_assets", "total_equity"]
    keep = [c for c in keep if c in df.columns]
    return df[keep].reset_index(drop=True)


def fetch_financial_batch(
    ts_codes: List[str],
    start_year: str = "2014",
    max_workers: int = 8,
) -> tuple[pd.DataFrame, List[str]]:
    """批量抓取财务数据（主源失败自动降级备源）。

    返回 (合并后的 DataFrame, 失败代码列表)。
    """
    client = get_client()
    raw_map, failed = client.call_batch(
        "stock_financial_analysis_indicator",
        [normalize_code(c) for c in ts_codes],
        key="symbol",
        extra_kwargs={"start_year": start_year},
        max_workers=max_workers,
        fallbacks=_SINA_FALLBACK,
    )

    frames = []
    for code, raw in raw_map.items():
        try:
            df = _normalize_financial(raw, code, _source_of(raw))
            if df.empty:
                failed.append(code)
                continue
            frames.append(df)
        except Exception as e:                            # noqa: BLE001
            logger.error(f"{code} 财务解析失败: {type(e).__name__}: {e}")
            failed.append(code)

    # 第三层降级：akshare 生态内拿不到的，用 baostock 串行兜一遍。
    # 串行是刻意的 —— baostock 会话非线程安全，并发查询会串数据。
    if failed:
        rescued: List[str] = []
        for code in list(failed):
            df = _fetch_baostock_financial(code, start_year)
            if not df.empty:
                frames.append(df)
                rescued.append(code)
        for c in rescued:
            failed.remove(c)
        if rescued:
            logger.info(f"baostock 跨生态兜回 {len(rescued)} 只"
                        f"（示例 {rescued[:5]}）")

    if not frames:
        return pd.DataFrame(), failed

    merged = pd.concat(frames, ignore_index=True)
    # 只保留目标列（缺失的补 None，保证表结构稳定）
    target = ["ts_code", "report_date", "report_type", "ann_date", "ann_date_source",
              "eps", "eps_ttm", "bps", "roe", "roe_ttm", "gross_margin", "debt_ratio"]
    for c in target:
        if c not in merged.columns:
            merged[c] = None
    return merged[target], failed


if __name__ == "__main__":
    df = fetch_financial("600000", start_year="2021")
    print(df.to_string())
    print("\n列:", list(df.columns))
