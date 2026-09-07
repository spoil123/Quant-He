# -*- coding: utf-8 -*-
"""
股票池构建（含退市股 —— 消除幸存者偏差的第一道关）。

为什么必须拉退市股：
    如果只用「当前还在市的股票」回测，那些跌到退市的样本被系统性剔除，
    策略表现会被系统性高估。典型偏差量级：A 股全样本下年化虚高 2~4 个点。
    所以 stock_basic 必须包含退市股，并保留其退市前的全部历史行情。

AKShare 相关接口（名字随版本会变，这里做多接口尝试 + 字段容错）：
    stock_info_a_code_name     当前全 A 代码+名称
    stock_info_sh_name_code    沪市（含上市日期）
    stock_info_sz_name_code    深市（含上市日期）
    stock_info_bj_name_code    北交所
    stock_info_sh_delist       沪市退市列表（含暂停/终止上市日期）
    stock_info_sz_delist       深市退市列表（含终止上市日期）
    stock_zh_a_st_em           当前 ST/*ST 名单

注意：旧版 akshare 的 stock_info_delist 在新版已移除，改按市场拆分，见 fetch_delisted()。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from src.common.logger import logger
from src.common.utils import board_of, code_exchange, normalize_code, to_date
from src.layer1_data.fetcher.akshare_client import get_client

# 各接口返回的中文字段名 -> 统一英文名
_FIELD_MAP: Dict[str, str] = {
    "证券代码": "ts_code",
    "A股代码": "ts_code",
    "代码": "ts_code",
    "股票代码": "ts_code",
    "公司代码": "ts_code",        # 沪市退市接口用这个列名
    "code": "ts_code",          # stock_info_a_code_name 用的是英文列名
    "证券简称": "name",
    "A股简称": "name",
    "名称": "name",
    "股票简称": "name",
    "公司简称": "name",          # 沪市退市接口用这个列名
    "上市日期": "list_date",
    "A股上市日期": "list_date",
    "上市时间": "list_date",
    "退市日期": "delist_date",
    "退市时间": "delist_date",
    "终止上市日期": "delist_date",
    "暂停上市日期": "delist_date",     # 沪市退市接口用这个列名
    "公司全称": "full_name",
}


def _rename(df: pd.DataFrame) -> pd.DataFrame:
    """把中文字段映射为统一英文名（只改存在的列）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={k: v for k, v in _FIELD_MAP.items() if k in df.columns})
    # 重命名可能产生重复列名（沪市接口"证券简称"和"公司简称"都会映射成 name），
    # 不去重的话后续 pd.concat 会抛 InvalidIndexError
    return df.loc[:, ~df.columns.duplicated()]


def _try_call(client, func_names: List[str], **kwargs) -> pd.DataFrame:
    """按顺序尝试多个接口，返回第一个成功且非空的 DataFrame。"""
    for fn in func_names:
        try:
            if not hasattr(__import__("akshare", fromlist=["x"]), fn):
                logger.debug(f"接口不存在，跳过: {fn}")
                continue
            df = client.call(fn, **kwargs)
            if df is not None and not df.empty:
                logger.info(f"接口 {fn} 返回 {len(df)} 行")
                return _rename(df)
            logger.debug(f"接口 {fn} 返回空")
        except Exception as e:                             # noqa: BLE001
            logger.warning(f"接口 {fn} 调用失败: {type(e).__name__}: {e}")
    return pd.DataFrame()


def fetch_current_list() -> pd.DataFrame:
    """获取当前在市 A 股列表（沪深京三市）。"""
    client = get_client()
    frames: List[pd.DataFrame] = []

    # 1) 全 A 兜底接口
    df_all = _try_call(client, ["stock_info_a_code_name"])
    if not df_all.empty:
        frames.append(df_all)

    # 2) 分市场接口（含上市日期，优先用它的 list_date）
    for market, funcs in (
        ("SH", ["stock_info_sh_name_code"]),
        ("SZ", ["stock_info_sz_name_code"]),
        ("BJ", ["stock_info_bj_name_code"]),
    ):
        df_m = _try_call(client, funcs)
        if not df_m.empty:
            df_m["exchange"] = market
            frames.append(df_m)

    if not frames:
        logger.error("所有股票列表接口均失败")
        return pd.DataFrame(columns=["ts_code", "name", "exchange", "list_date"])

    # 合并：优先保留带 list_date 的记录
    merged = pd.concat(frames, ignore_index=True)
    if "ts_code" not in merged.columns:
        logger.error(f"列表接口返回字段异常: {list(merged.columns)}")
        return pd.DataFrame(columns=["ts_code", "name", "exchange", "list_date"])

    merged["ts_code"] = merged["ts_code"].astype(str).map(normalize_code)

    # 同名列合并时取第一个非空值（优先保留带上市日期的记录）
    # 注意：不同接口的 list_date 类型不一致（str / date / Timestamp 混在一起），
    # 必须先统一成 datetime 才能排序，否则会抛 "str 和 date 无法比较"
    if "list_date" in merged.columns:
        merged["_list_dt"] = pd.to_datetime(merged["list_date"], errors="coerce")
        merged = merged.sort_values("_list_dt", na_position="last").drop(columns=["_list_dt"])
    merged = merged.drop_duplicates(subset=["ts_code"], keep="first")

    if "exchange" not in merged.columns:
        merged["exchange"] = merged["ts_code"].map(code_exchange)
    else:
        merged["exchange"] = merged["exchange"].fillna(merged["ts_code"].map(code_exchange))

    if "list_date" in merged.columns:
        merged["list_date"] = merged["list_date"].map(lambda x: to_date(x))
    else:
        merged["list_date"] = None

    cols = ["ts_code", "name", "exchange", "list_date"]
    for c in cols:
        if c not in merged.columns:
            merged[c] = None
    return merged[cols].reset_index(drop=True)


def fetch_delisted() -> pd.DataFrame:
    """获取退市股票列表（含退市日期）。幸存者偏差的修正来源。"""
    client = get_client()

    # 新版 akshare 把退市接口按市场拆开了，两个都要取
    frames = []
    for fn in ("stock_info_sh_delist", "stock_info_sz_delist"):
        d = _try_call(client, [fn])
        if not d.empty and "ts_code" in d.columns:
            frames.append(d)

    if not frames:
        logger.warning("退市股票接口未取到数据 —— 这会导致幸存者偏差，请检查 akshare 版本")
        return pd.DataFrame(columns=["ts_code", "name", "exchange", "delist_date"])

    df = pd.concat(frames, ignore_index=True)

    if df.empty or "ts_code" not in df.columns:
        return pd.DataFrame(columns=["ts_code", "name", "exchange", "delist_date"])

    df["ts_code"] = df["ts_code"].astype(str).map(normalize_code)
    df["exchange"] = df["ts_code"].map(code_exchange)
    if "delist_date" in df.columns:
        df["delist_date"] = df["delist_date"].map(lambda x: to_date(x))
    else:
        df["delist_date"] = None

    df = df.drop_duplicates(subset=["ts_code"], keep="first")
    out = df[["ts_code", "name", "exchange", "delist_date"]].reset_index(drop=True)
    logger.info(f"退市股票 {len(out)} 只")
    return out


def fetch_st_list() -> pd.DataFrame:
    """获取当前 ST / *ST 名单（只作标记，不剔除）。"""
    client = get_client()
    df = _try_call(client, ["stock_zh_a_st_em"])
    if df.empty or "ts_code" not in df.columns:
        logger.warning("ST 名单未取到，is_st 将全部置 0（不影响数据入库，仅影响过滤）")
        return pd.DataFrame(columns=["ts_code"])
    df["ts_code"] = df["ts_code"].astype(str).map(normalize_code)
    return df[["ts_code"]].drop_duplicates()


def build_universe() -> pd.DataFrame:
    """合并当前在市 + 退市，生成 stock_basic 数据帧。

    返回字段：ts_code / name / exchange / board / list_date / delist_date
             / is_delisted / is_st / status
    status: 0=已退市 1=正常上市
    """
    cur = fetch_current_list()
    delisted = fetch_delisted()
    st = fetch_st_list()

    logger.info(f"在市 {len(cur)} 只，退市 {len(delisted)} 只")

    # 在市
    if not cur.empty:
        cur = cur.copy()
        cur["delist_date"] = None
        cur["is_delisted"] = 0
    else:
        cur = pd.DataFrame(columns=["ts_code", "name", "exchange", "list_date",
                                    "delist_date", "is_delisted"])

    # 退市
    if not delisted.empty:
        delisted = delisted.copy()
        delisted["is_delisted"] = 1
        if "list_date" not in delisted.columns:
            delisted["list_date"] = None
    else:
        delisted = pd.DataFrame(columns=["ts_code", "name", "exchange", "list_date",
                                         "delist_date", "is_delisted"])

    base_cols = ["ts_code", "name", "exchange", "list_date", "delist_date", "is_delisted"]
    uni = pd.concat([cur[base_cols], delisted[base_cols]], ignore_index=True)

    # 退市的优先（保留退市日期），在市的次之
    uni["_pri"] = uni["is_delisted"].fillna(0).astype(int)
    uni = uni.sort_values(["ts_code", "_pri"], ascending=[True, False])
    uni = uni.drop_duplicates(subset=["ts_code"], keep="first").drop(columns=["_pri"])

    uni["board"] = uni["ts_code"].map(board_of)
    uni["exchange"] = uni["exchange"].fillna(uni["ts_code"].map(code_exchange))

    # ST 判定：优先用接口名单；接口不可用时从名称推断（A 股 ST 股名称必含 "ST"）
    st_set = set(st["ts_code"]) if not st.empty else set()
    if st_set:
        uni["is_st"] = uni["ts_code"].isin(st_set).astype(int)
    else:
        uni["is_st"] = uni["name"].astype(str).str.upper().str.contains("ST", na=False).astype(int)
        if int(uni["is_st"].sum()):
            logger.info(f"ST 名单接口不可用，改由名称推断出 {int(uni['is_st'].sum())} 只 ST 股")
    # 已退市 status=0，在市=1
    uni["status"] = uni["is_delisted"].map(lambda x: 0 if int(x or 0) == 1 else 1)

    uni = uni.sort_values("ts_code").reset_index(drop=True)
    logger.info(
        f"股票池构建完成：共 {len(uni)} 只（在市 {int((uni.is_delisted == 0).sum())}，"
        f"退市 {int((uni.is_delisted == 1).sum())}，ST {int(uni.is_st.sum())}）"
    )
    return uni[["ts_code", "name", "exchange", "board", "list_date",
                "delist_date", "is_delisted", "is_st", "status"]]


def sample_universe(n: int = 50, seed: int = 42) -> pd.DataFrame:
    """随机抽样（跑通链路时用，避免全量抓取耗时）。"""
    uni = build_universe()
    return uni.sample(n=min(n, len(uni)), random_state=seed).reset_index(drop=True)


if __name__ == "__main__":
    df = build_universe()
    print(df.head(10).to_string())
    print("\n总数:", len(df))
    print("退市数:", int(df["is_delisted"].sum()))
    print("板块分布:\n", df["board"].value_counts())
