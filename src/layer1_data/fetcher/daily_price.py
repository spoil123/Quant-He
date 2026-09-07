# -*- coding: utf-8 -*-
"""
日线行情抓取。

数据源选择（本机实测，2026-08）：
    新浪 stock_zh_a_daily   —— 主力。一次返回全历史（6000+ 行），且带 outstanding_share
                               （流通股本历史序列），可以自己算流通市值，不依赖估值接口。
    腾讯 stock_zh_a_hist_tx —— 备选。按日期区间返回，字段较少。
    东财 stock_zh_a_hist    —— 不可用。其历史 K 线域名 push2his.eastmoney.com 在本机
                               网络下连接被重置（HTTP 000），实时域名 push2 正常。
                               若将来网络恢复，把它加回 SOURCES 即可，无需改别处。

新浪字段口径（实测确认，别搞错）：
    turnover          是「比率」不是百分数：0.002854 表示 0.2854%，入库时 ×100
    outstanding_share 是当日流通股本（股），是真实历史序列（浦发 2000 年 4 亿股 → 2023 年 293 亿股）
    amount            成交额（元）
    volume            成交量（股）

关于复权（这是回测最容易出错的地方）：
    qfq 前复权：以最新价为基准往前折算。发生新的分红送股时，整条历史序列会被追溯修改，
               所以增量更新不能只追加新行 —— 必须检测复权基准漂移，漂移了就重拉全历史。
    hfq 后复权：以上市首日为基准向后折算。历史一旦生成不再变化，收益计算一律用它，
               保证任何时点重跑结果一致。
    none 不复权：真实成交价，算市值、判断涨跌停、判断 1 元退市风险都用它。

    本项目三份都存，各司其职。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.common.logger import logger
from src.common.utils import board_of, code_exchange, normalize_code, to_compact
from src.layer1_data.fetcher.akshare_client import get_client

# 新浪需要的带前缀代码：sh600000 / sz000001 / bj430047
def sina_symbol(ts_code: str) -> str:
    return code_exchange(ts_code).lower() + normalize_code(ts_code)


# 各板块涨跌幅限制（%）。ST 股为 5%，清洗层按名称修正。
_LIMIT_PCT = {"主板": 10.0, "创业板": 20.0, "科创板": 20.0, "北交所": 30.0}

# 数据源优先级
#   sina / tx 都走 akshare 函数 —— akshare 整体不可用（接口改版/被封/依赖冲突）
#   时会一起断。baostock 是独立库 + 独立服务端，作第三层兜底（P1-4 跨生态）。
SOURCES = ["sina", "tx", "baostock"]


# ---------------------------------------------------------------- 单源抓取

def _fetch_sina(ts_code: str, adjust: str) -> pd.DataFrame:
    client = get_client()
    return client.call("stock_zh_a_daily", symbol=sina_symbol(ts_code), adjust=adjust)


def _fetch_tx(ts_code: str, adjust: str, start_date: str, end_date: str) -> pd.DataFrame:
    client = get_client()
    return client.call(
        "stock_zh_a_hist_tx",
        symbol=sina_symbol(ts_code),
        start_date=to_compact(start_date),
        end_date=to_compact(end_date),
        adjust=adjust,
    )


def _fetch_baostock(ts_code: str, adjust: str, start_date: str, end_date: str) -> pd.DataFrame:
    """baostock 独立生态兜底（不经过 akshare）。"""
    from src.layer1_data.fetcher.baostock_client import get_baostock
    return get_baostock().query_daily(
        ts_code, start_date=start_date, end_date=end_date, adjust=adjust,
    )


def _normalize_sina(df: pd.DataFrame, ts_code: str, adj_type: str) -> pd.DataFrame:
    """新浪返回 -> 入库格式。"""
    if df is None or df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["trade_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    out["ts_code"] = normalize_code(ts_code)
    out["adj_type"] = adj_type

    for src, dst in (("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("volume", "volume"), ("amount", "amount")):
        out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else None

    out["pre_close"] = None      # 由清洗层按复权口径补齐
    out["change"] = None
    out["change_pct"] = None
    out["amplitude"] = None

    # 换手率：新浪给的是比率，统一成百分数
    if "turnover" in df.columns:
        out["turnover_rate"] = pd.to_numeric(df["turnover"], errors="coerce") * 100
    else:
        out["turnover_rate"] = None

    # 流通股本（历史序列），供 daily_basic 算流通市值
    if "outstanding_share" in df.columns:
        out["float_share"] = pd.to_numeric(df["outstanding_share"], errors="coerce")
    else:
        out["float_share"] = None

    out = out.dropna(subset=["trade_date"])

    # 停牌：成交量为 0
    out["is_suspended"] = (out["volume"].fillna(0) <= 0).astype(int)

    # 涨跌停：用不复权口径判断（复权价的涨跌幅会被分红扭曲）
    limit = _LIMIT_PCT.get(board_of(ts_code), 10.0)

    return out


def fetch_daily(
    ts_code: str,
    start_date: str = "2015-01-01",
    end_date: str = "2050-01-01",
    adjust: str = "qfq",
    source: str = "sina",
) -> pd.DataFrame:
    """抓单只股票单种复权的日线（主数据源失败自动降级到备用源）。"""
    code = normalize_code(ts_code)
    adj = {"qfq": "qfq", "hfq": "hfq", "none": "", "": ""}.get(adjust, "qfq")

    # 自动降级：主源失败就挨个试备用源。
    # 实测新浪对部分老退市股返回空 JSON（JSONDecodeError），腾讯反而能取到；
    # 反过来腾讯不支持北交所 920 代码而新浪支持老代码段。这一步能把全量抓取
    # 的失败率实实在在降下来。
    sources = [source] + [s for s in SOURCES if s != source]
    df = pd.DataFrame()
    last_err: Exception | None = None

    for src in sources:
        try:
            if src == "sina":
                raw = _fetch_sina(code, adj)
                df = _normalize_sina(raw, code, adjust)
            elif src == "tx":
                raw = _fetch_tx(code, adj, start_date, end_date)
                df = _normalize_tx(raw, code, adjust)
            elif src == "baostock":
                raw = _fetch_baostock(code, adj, start_date, end_date)
                df = _normalize_baostock(raw, code, adjust)
            else:
                continue
            if not df.empty:
                if src != source:
                    logger.info(f"{code} {adjust} 主数据源 {source} 失败，已由 {src} 兜底")
                break
        except Exception as e:                            # noqa: BLE001
            last_err = e
            logger.debug(f"{code} {adjust} 数据源 {src} 失败: {type(e).__name__}: {e}")
            continue

    if df.empty:
        logger.warning(
            f"{code} {adjust} 所有数据源均失败"
            f"（{type(last_err).__name__ if last_err else '返回空'}）"
        )
        return pd.DataFrame()

    # 按日期区间裁剪（新浪返回全历史）
    s, e = pd.Timestamp(start_date), pd.Timestamp(end_date)
    mask = (pd.to_datetime(df["trade_date"]) >= s) & (pd.to_datetime(df["trade_date"]) <= e)
    df = df.loc[mask].copy()

    # 涨跌幅：复权口径下用自身序列计算（前复权/后复权序列内部自洽）
    df = df.sort_values("trade_date").reset_index(drop=True)
    if adjust in ("qfq", "hfq") and "close" in df.columns:
        prev = df["close"].shift(1)
        df["pre_close"] = prev
        df["change"] = (df["close"] - prev).round(4)
        df["change_pct"] = ((df["close"] / prev - 1.0) * 100).round(4)
        df["amplitude"] = ((df["high"] - df["low"]) / prev * 100).round(4)

    lim = _LIMIT_PCT.get(board_of(code), 10.0)
    chg = df["change_pct"].fillna(0) if "change_pct" in df.columns else pd.Series(0, index=df.index)
    df["is_limit_up"] = (chg >= lim - 0.6).astype(int)
    df["is_limit_down"] = (chg <= -(lim - 0.6)).astype(int)
    df["status"] = 1
    df.loc[df["is_suspended"] == 1, "status"] = 0

    return df


def _normalize_baostock(df: pd.DataFrame, ts_code: str, adj_type: str) -> pd.DataFrame:
    """baostock 返回 -> 入库格式。

    baostock 字段是字符串，date 为 YYYY-MM-DD。与新浪/腾讯的差异：
      · turnover 缺省（baostock 的 turn 是成交额占比近似，不用）
      · 无流通股本 outstanding_share，float_share 留空（下游算市值会缺，
        由覆盖率/质量闸门发现，不会静默出错）
      · pctChg 自带，可直接用
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["trade_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    out["ts_code"] = normalize_code(ts_code)
    out["adj_type"] = adj_type

    for src, dst in (("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("volume", "volume"), ("amount", "amount")):
        out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else None

    out["pre_close"] = pd.to_numeric(df["preclose"], errors="coerce") if "preclose" in df.columns else None
    out["change"] = None
    out["change_pct"] = None
    out["amplitude"] = None
    out["turnover_rate"] = None
    out["float_share"] = None
    out = out.dropna(subset=["trade_date"])
    out["is_suspended"] = (out["volume"].fillna(0) <= 0).astype(int)

    # baostock 停牌标记（tradestatus=0 表示停牌，与 volume=0 等价校验）
    if "tradestatus" in df.columns:
        ts_flag = pd.to_numeric(df["tradestatus"], errors="coerce")
        suspended = ts_flag.fillna(1) <= 0
        out["is_suspended"] = (out["is_suspended"] | suspended).astype(int)
    return out


def _normalize_tx(df: pd.DataFrame, ts_code: str, adj_type: str) -> pd.DataFrame:
    """腾讯返回 -> 入库格式。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["trade_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    out["ts_code"] = normalize_code(ts_code)
    out["adj_type"] = adj_type
    for src, dst in (("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("volume", "volume"), ("amount", "amount")):
        out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else None
    out["pre_close"] = None
    out["change"] = None
    out["change_pct"] = None
    out["amplitude"] = None
    out["turnover_rate"] = pd.to_numeric(df["turnover"], errors="coerce") * 100 if "turnover" in df.columns else None
    out["float_share"] = None
    out = out.dropna(subset=["trade_date"])
    out["is_suspended"] = (out["volume"].fillna(0) <= 0).astype(int)
    return out


# ---------------------------------------------------------------- 一次抓全

def fetch_stock_full(
    ts_code: str,
    start_date: str = "2015-01-01",
    end_date: str = "2050-01-01",
    adjusts: Tuple[str, ...] = ("qfq", "none"),
    source: str = "sina",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """一次抓齐某只股票所需复权，并派生 daily_basic（市值）。

    默认只抓 qfq + none：
      qfq  回测/因子用（前复权）
      none 用于派生流通市值（float_mv = 不复权收盘价 × 流通股本）
    不抓 hfq：当前策略面板只用 qfq、basic 用 none，hfq 无人消费；
    全量阶段省 1/3 请求与入库。将来需要时 adjusts=("hfq",) 单补即可。

    返回 (price_df, basic_df)
      price_df: daily_price 表格式（复权纵向拼接）
      basic_df: daily_basic 表格式（流通市值、总股本代理、换手率）
    """
    code = normalize_code(ts_code)
    frames: List[pd.DataFrame] = []
    raw_close: Optional[pd.DataFrame] = None

    for adj in adjusts:
        df = fetch_daily(code, start_date, end_date, adj, source=source)
        if df.empty:
            continue
        if adj == "none":
            raw_close = df[["trade_date", "close", "float_share", "turnover_rate"]].copy()
            raw_close = raw_close.rename(columns={"close": "close_raw"})
        frames.append(df)

    if not frames:
        return pd.DataFrame(), pd.DataFrame()

    price = pd.concat(frames, ignore_index=True)

    # ---- 派生 daily_basic：流通市值 = 不复权收盘价 × 流通股本 ----
    if raw_close is not None and not raw_close.empty and raw_close["float_share"].notna().any():
        b = raw_close.copy()
        b["ts_code"] = code
        b["float_mv"] = b["close_raw"] * b["float_share"]
        # 总市值：新浪只给流通股本，总市值留空（策略加权用流通市值即可）
        b["total_mv"] = None
        b["total_share"] = None
        b["close"] = b["close_raw"]
        b["pe_ttm"] = None
        b["pb"] = None
        b["ps_ttm"] = None
        b["dv_ttm"] = None
        basic = b[["trade_date", "ts_code", "close", "total_mv", "float_mv",
                   "total_share", "float_share", "pe_ttm", "pb", "ps_ttm",
                   "dv_ttm", "turnover_rate"]].copy()
        basic = basic.dropna(subset=["float_mv"])
    else:
        basic = pd.DataFrame()

    price = price.drop(columns=["float_share"], errors="ignore")
    return price, basic


def fetch_index_daily(
    index_code: str = "000300",
    start_date: str = "2015-01-01",
    end_date: str = "2050-01-01",
) -> pd.DataFrame:
    """指数日线（新浪，主源失败自动降级腾讯）。

    备源 stock_zh_index_daily_tx 字段兼容（date/open/high/low/close/volume）。
    """
    client = get_client()
    prefix = "sh" if index_code.startswith(("000", "999", "000")) else "sh"
    sym = prefix + index_code
    try:
        raw = client.call("stock_zh_index_daily", symbol=sym,
                          fallbacks=("stock_zh_index_daily_tx",))
    except Exception as e:                                # noqa: BLE001
        logger.error(f"指数 {index_code} 抓取失败: {type(e).__name__}: {e}")
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    df = pd.DataFrame()
    df["trade_date"] = pd.to_datetime(raw["date"], errors="coerce").dt.date
    df["index_code"] = index_code
    for src, dst in (("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("volume", "volume")):
        df[dst] = pd.to_numeric(raw[src], errors="coerce") if src in raw.columns else None
    df["amount"] = None
    df["change_pct"] = ((df["close"] / df["close"].shift(1) - 1.0) * 100).round(4)
    df = df.dropna(subset=["trade_date"])

    s, e = pd.Timestamp(start_date), pd.Timestamp(end_date)
    mask = (pd.to_datetime(df["trade_date"]) >= s) & (pd.to_datetime(df["trade_date"]) <= e)
    return df.loc[mask].sort_values("trade_date").reset_index(drop=True)


def fetch_trade_calendar() -> pd.DataFrame:
    """交易日历（新浪）。"""
    client = get_client()
    try:
        raw = client.call("tool_trade_date_hist_sina")
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"交易日历抓取失败: {e}")
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    col = raw.columns[0]
    df = pd.DataFrame({"trade_date": pd.to_datetime(raw[col], errors="coerce").dt.date})
    df = df.dropna().drop_duplicates().sort_values("trade_date").reset_index(drop=True)
    df["is_trading"] = 1
    df["prev_date"] = df["trade_date"].shift(1)
    df["next_date"] = df["trade_date"].shift(-1)

    ym = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m")
    df["is_month_end"] = (df.groupby(ym)["trade_date"].transform("max") == df["trade_date"]).astype(int)
    return df.drop(columns=[])


if __name__ == "__main__":
    p, b = fetch_stock_full("600000", "2023-01-01", "2023-03-31")
    print("=== price ===")
    print(p.head(6).to_string())
    print("\n复权类型:", p["adj_type"].unique().tolist(), " 行数:", len(p))
    print("\n=== basic（流通市值） ===")
    print(b.head(4).to_string())
    print("\n=== 指数 ===")
    print(fetch_index_daily("000300", "2023-01-01", "2023-01-10").to_string())
