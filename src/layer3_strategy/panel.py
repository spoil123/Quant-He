# -*- coding: utf-8 -*-
"""
因子面板组装（第 1 层 -> 第 3 层的桥梁）。

把分散在四张表里的行情、市值、财务、行业拼成一个大宽表，并且在拼接过程中
完成两件关键的时点对齐：

1. 财务对齐：用 ann_date（数据可用日）而不是 report_date（报告期）。
   实现是 merge_asof(direction="backward") —— 对每个交易日，只取该日之前
   已公布的最近一期财报。2023 年 4 月 28 日绝不可能看到 2023 年一季报。
2. 行业对齐：申万分类带「计入日期」，同样按生效日取当期的行业归属，
   而不是用最新行业去解释十年前的公司。

这两步做错，回测结果一定是虚高的 —— 因为模型"提前"看到了未来数据。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.common.db import read_sql
from src.common.logger import logger
from src.common.pit import build_pit_panel, shift_to_trade_date
from src.layer1_data.fetcher.industry import build_industry_snapshot

# 覆盖度对账：面板股票数 vs 当年全市场上市家数的偏差阈值（低于此覆盖率告警）
COVERAGE_WARN_THRESHOLD = 0.90

# 财务表里要带入面板的字段
FIN_COLS = ["eps", "eps_ttm", "bps", "roe", "roe_ttm", "gross_margin", "debt_ratio"]


def load_price_panel(
    start: str,
    end: str,
    codes: Optional[Sequence[str]] = None,
    adj_type: str = "qfq",
) -> pd.DataFrame:
    """行情 + 市值 基础面板。"""
    where = "p.adj_type = :adj AND p.trade_date BETWEEN :s AND :e"
    params: dict = {"adj": adj_type, "s": start, "e": end}

    if codes:
        codes = list(codes)
        # 代码量大时改用 IN 分批，这里用简单占位（MySQL 参数上限足够日常使用）
        ph = ", ".join(f":c{i}" for i in range(len(codes)))
        where += f" AND p.ts_code IN ({ph})"
        params.update({f"c{i}": c for i, c in enumerate(codes)})

    sql = f"""
        SELECT p.trade_date, p.ts_code,
               p.open, p.close, p.amount, p.turnover_rate,
               p.status, p.is_limit_up, p.is_limit_down, p.is_suspended,
               b.float_mv, b.float_share,
               s.is_st
        FROM daily_price p
        LEFT JOIN daily_basic b
               ON p.trade_date = b.trade_date AND p.ts_code = b.ts_code
        LEFT JOIN stock_basic s
               ON p.ts_code = s.ts_code
        WHERE {where}
        ORDER BY p.trade_date, p.ts_code
    """
    df = read_sql(sql, params)
    if df.empty:
        logger.warning(f"行情面板为空: {start} ~ {end}")
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["ts_code"] = df["ts_code"].astype(str)
    # 数值列转 float32：600 万行面板内存减半（价格/金额精度足够）
    for c in ("open", "close", "amount", "turnover_rate", "float_mv", "float_share"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    logger.info(f"行情面板: {len(df):,} 行，{df['ts_code'].nunique()} 只股票，"
                f"{df['trade_date'].nunique()} 个交易日")
    return df


def load_financial_pit(
    trade_dates: Sequence,
    codes: Optional[Sequence[str]] = None,
    lag_days: int = 1,
) -> pd.DataFrame:
    """财务数据的 PIT 面板（按数据可用日对齐）。"""
    where = "ann_date IS NOT NULL"
    params: dict = {}
    if codes:
        codes = list(codes)
        ph = ", ".join(f":c{i}" for i in range(len(codes)))
        where += f" AND ts_code IN ({ph})"
        params.update({f"c{i}": c for i, c in enumerate(codes)})

    cols = ["ts_code", "report_date", "ann_date"] + FIN_COLS
    sql = f"SELECT {', '.join(cols)} FROM financial_indicator WHERE {where} ORDER BY ts_code, ann_date"
    fin = read_sql(sql, params)
    if fin.empty:
        logger.warning("财务表为空或尚未入库，基本面因子将全部为 NaN")
        return pd.DataFrame()

    fin["ann_date"] = pd.to_datetime(fin["ann_date"]).dt.date
    fin["report_date"] = pd.to_datetime(fin["report_date"]).dt.date

    # 自然日 -> 可交易日（公告日当天盘后才公开，最快 T+1 能用）
    fin = shift_to_trade_date(fin, trade_dates, date_col="ann_date",
                              out_col="usable_date", lag_days=lag_days)

    panel = build_pit_panel(
        fin, trade_dates, ts_codes=list(codes) if codes else None,
        date_col="usable_date", value_cols=FIN_COLS,
    )
    logger.info(f"财务 PIT 面板: {len(panel):,} 行")
    return panel


def load_industry_pit(
    trade_dates: Sequence,
    codes: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """行业分类的 PIT 面板。"""
    try:
        ind = build_industry_snapshot(trade_dates=trade_dates, ts_codes=codes)
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"行业面板构建失败（将跳过行业中性化）: {e}")
        return pd.DataFrame()
    if ind.empty:
        logger.warning("行业面板为空，行业中性化将退化为仅市值中性化")
    return ind


def build_factor_panel(
    start: str,
    end: str,
    codes: Optional[Sequence[str]] = None,
    with_financial: bool = True,
    with_industry: bool = True,
    adj_type: str = "qfq",
) -> pd.DataFrame:
    """组装完整因子面板。

    列包含：行情 + 市值 + 财务(PIT) + 行业(PIT)
    """
    panel = load_price_panel(start, end, codes, adj_type=adj_type)
    if panel.empty:
        return panel

    trade_dates = sorted(panel["trade_date"].unique())
    all_codes = sorted(panel["ts_code"].unique())

    if with_financial:
        fin_p = load_financial_pit(trade_dates, all_codes)
        if not fin_p.empty:
            panel = panel.merge(fin_p, on=["trade_date", "ts_code"], how="left")

    if with_industry:
        ind_p = load_industry_pit(trade_dates, all_codes)
        if not ind_p.empty:
            panel = panel.merge(ind_p[["trade_date", "ts_code", "industry"]],
                                on=["trade_date", "ts_code"], how="left")
        if "industry" not in panel.columns:
            panel["industry"] = "UNKNOWN"
        panel["industry"] = panel["industry"].fillna("UNKNOWN")
    elif "industry" not in panel.columns:
        # with_industry=False 且无行业列时补一列占位，
        # 保证下游（中性化/回测）不因缺失 industry 而 KeyError
        panel["industry"] = "UNKNOWN"

    logger.info(f"因子面板组装完成: {len(panel):,} 行 × {len(panel.columns)} 列")
    return panel


def attach_raw_close(panel: pd.DataFrame) -> pd.DataFrame:
    """给面板补一列「不复权收盘价」raw_close —— 估值因子专用。

    M4 修复（2026-09-07）：账面市值比 BM = BPS / 价格，分子分母必须同口径。
    面板的 close 是前复权（qfq：历史价按除权因子向下折算到当前基准），而 bps
    取自 financial_indicator 的每股净资产，是**披露当期的原始值**、未经任何
    复权处理。两者直接相除，凡是发生过送转/分红的股票，分母被系统性折小，
    历史 BM 被放大，价值因子的截面排名随之错位 —— 而且不报错、不告警。

    正确配法是 BPS 配「当日实际成交价」（不复权）：

        BP = 净资产 / 市值 = (BPS × 股本) / (实际价 × 股本) = BPS / 实际价

    股本在分子分母约掉的前提是两者同属一个时点：不复权价满足，前复权价不满足。

    实现：adj_factor 表当前为空（0 行），无法用因子反推不复权价，故直接取
    daily_price 里 adj_type='none' 的收盘价。取不到时退回前复权价并告警 ——
    宁可沿用旧口径，也不能让因子整列变 NaN 静默失效。
    """
    if panel is None or panel.empty or "close" not in panel.columns:
        return panel

    raw = pd.DataFrame()
    try:
        start = str(pd.Timestamp(panel["trade_date"].min()))[:10]
        end = str(pd.Timestamp(panel["trade_date"].max()))[:10]
        codes = sorted(panel["ts_code"].dropna().unique().tolist())
        raw = load_price_panel(start, end, codes, adj_type="none")
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"读取不复权价格失败（BM 退回前复权口径）: "
                       f"{type(e).__name__}: {e}")

    if raw.empty or "close" not in raw.columns:
        panel["raw_close"] = panel["close"]
        logger.warning("未取到不复权价格，raw_close 退回前复权 close —— "
                       "估值因子仍是旧口径，建议补抓 adj_type='none' 数据")
        return panel

    m = (raw[["trade_date", "ts_code", "close"]]
         .drop_duplicates(subset=["trade_date", "ts_code"])
         .rename(columns={"close": "raw_close"}))
    out = panel.merge(m, on=["trade_date", "ts_code"], how="left")
    n_miss = int(out["raw_close"].isna().sum())
    if n_miss:
        # 混基准防护（2026-09-09，第三轮审计 L2）：同一只股票的 raw_close
        # 部分缺失时若用前复权价回填，BM=BPS/raw_close 在送转股上会出现
        # 两套价格基准的跳变，截面排名错位。改为按股处理：
        #   整只股票完全没有不复权价 → 整股退回前复权（股内基准一致）；
        #   部分日期缺失 → 留 NaN（该格 BM 缺失由因子计算 dropna 兜底），
        #   宁缺勿混。
        has_raw = out.groupby("ts_code")["raw_close"].transform(
            lambda s: s.notna().any())
        fallback_mask = out["raw_close"].isna() & ~has_raw
        out["raw_close"] = out["raw_close"].fillna(
            out["close"].where(fallback_mask))
        n_part = n_miss - int(fallback_mask.sum())
        if n_part:
            logger.warning(f"raw_close 有 {n_part} 行属「部分缺失」股票，"
                           f"留 NaN 防止 BM 混用两套复权基准")
        n_fb = int(fallback_mask.sum())
        if n_fb:
            logger.warning(f"{out.loc[fallback_mask, 'ts_code'].nunique()} 只股票"
                           f"完全无不复权价，整股退回前复权口径（{n_fb} 行）")
    return out


def add_forward_returns(
    panel: pd.DataFrame,
    periods: Sequence[int] = (1, 5, 20),
    price_col: str = "close",
) -> pd.DataFrame:
    """附加未来 N 日收益（仅用于因子 IC 检验，绝不能用于生成信号）。

    这一步是评估环节专用的。生成交易信号时严禁调用，否则等于用未来数据下单。
    """
    out = panel.sort_values(["ts_code", "trade_date"]).copy()
    c = out.groupby("ts_code", sort=False)[price_col]
    for n in periods:
        out[f"fwd_ret_{n}d"] = (c.shift(-n) / out[price_col] - 1.0)
    return out


# ---------------------------------------------------------------- 幸存者偏差对账

def load_market_listing_history() -> pd.DataFrame:
    """从 stock_basic 读取全市场（含退市）上市/退市日期，用于逐年覆盖度对账。

    关键：必须含退市股（is_delisted=1），否则对账本身就有幸存者偏差。
    若 stock_basic 里退市股比例过低（<2%），说明取数源头就有问题，直接告警。
    """
    df = read_sql(
        """SELECT ts_code, list_date, delist_date, is_delisted
           FROM stock_basic"""
    )
    if df.empty:
        logger.warning("stock_basic 为空，无法做幸存者偏差对账")
        return df
    df["ts_code"] = df["ts_code"].astype(str)
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce").dt.date
    df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce").dt.date
    df["is_delisted"] = pd.to_numeric(df["is_delisted"], errors="coerce").fillna(0).astype(int)

    n_delist = int((df["is_delisted"] == 1).sum())
    ratio = n_delist / len(df) if len(df) else 0.0
    logger.info(f"stock_basic 共 {len(df)} 只（退市 {n_delist}，占比 {ratio:.1%}）")
    if ratio < 0.02:
        logger.warning(
            f"退市股占比仅 {ratio:.1%}（<2%）—— 退市列表可能缺失，"
            "幸存者偏差对账结果不可信，请检查 stock_info_*_delist 接口"
        )
    return df


def market_listed_count_by_year(
    listing: pd.DataFrame,
    years: Sequence[int],
) -> pd.Series:
    """每年「全市场应在市家数」= 当年 12-31 时已上市且未退市的股票数。

    用当年年末作为对账基准日：list_date <= 年末 且
    (delist_date 为空 或 delist_date > 年末)。
    """
    if listing is None or listing.empty or "list_date" not in listing.columns:
        # 空上市表：无数据可对账，各年记 0（调用方据此跳过或告警）
        return pd.Series(0, index=list(years), dtype=int)
    out: Dict[int, int] = {}
    for y in years:
        end = pd.Timestamp(year=y, month=12, day=31).date()
        alive = (
            (listing["list_date"].notna())
            & (listing["list_date"] <= end)
            & (listing["delist_date"].isna() | (listing["delist_date"] > end))
        )
        out[y] = int(alive.sum())
    return pd.Series(out, dtype=int)


def coverage_report(
    panel: pd.DataFrame,
    start: str | None = None,
    end: str | None = None,
    listing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """逐年对账：面板实际股票数 vs 当年全市场上市家数。

    验收口径：面板股票数逐年跟得上历史上市总数（覆盖率 > 90%），
    无「退市股集体消失」。返回逐年对账表（同时打日志）。
    """
    if panel is None or panel.empty:
        logger.warning("面板为空，跳过覆盖度对账")
        return pd.DataFrame()

    if listing is None:
        listing = load_market_listing_history()
    if listing.empty:
        return pd.DataFrame()

    p = panel.copy()
    p["_year"] = pd.to_datetime(p["trade_date"]).dt.year
    # 当年「出现过任何行情记录」的股票数（含退市股，退市前的历史必须还在）
    panel_n = p.groupby("_year")["ts_code"].nunique()

    all_years = sorted(panel_n.index.tolist())
    market_n = market_listed_count_by_year(listing, all_years)

    rows = []
    for y in all_years:
        pn = int(panel_n.loc[y])
        mn = int(market_n.loc[y])
        cov = pn / mn if mn else float("nan")
        flag = "OK" if (mn == 0 or cov >= COVERAGE_WARN_THRESHOLD) else "LOW"
        rows.append({"year": y, "panel_stocks": pn, "market_listed": mn,
                     "coverage": round(cov, 4), "flag": flag})

    rep = pd.DataFrame(rows).set_index("year")
    if rep.empty:
        return rep

    # 日志输出
    lines = ["\n===== 幸存者偏差对账（面板 vs 当年全市场） ====="]
    for y, r in rep.iterrows():
        lines.append(
            f"  {y}: 面板 {r['panel_stocks']:>5d} / 当年上市 {r['market_listed']:>5d} "
            f"= 覆盖率 {r['coverage']:.1%}  {r['flag']}"
        )
    low = rep[rep["flag"] == "LOW"]
    if len(low):
        lines.append(f"  ⚠ 覆盖率偏低年份 {list(low.index)} —— 退市股行情可能缺失")
    else:
        lines.append("  ✔ 各年覆盖率均达标，未发现退市股集体消失")
    lines.append("=" * 52)
    logger.info("\n".join(lines))
    return rep
