# -*- coding: utf-8 -*-
"""
baostock 数据客户端 —— P1-4 备源，独立于 akshare 生态。

为什么需要它：
    日线数据已有新浪/腾讯双 HTTP 源（真正独立，不经过 akshare），但财务数据的
    备源 stock_financial_abstract 仍属 akshare 生态 —— akshare 整体不可用
    （接口改版 / 被封 / 依赖冲突）时财务链路会全断，单库单点只解决一半。
    baostock 是独立的库 + 独立服务端，用来兜住这最后一环。

能力边界（先说清楚，别到时候当黑盒用）：
    · profit 接口给 epsTTM / roeAvg / npMargin / gpMargin，能保住 EP、ROE 类因子
    · 没有「每股净资产」，BP 因子拿不到 —— 降级时该列缺失，
      因子覆盖率报告（factors/coverage.py）会发现并告警，不会静默出错
    · 自带 pubDate（真实公告日），时点对齐可用 precise 策略，比主源更精确
    · 北交所(bj.)支持有限，取不到时会返回空而不是报错

单位口径：baostock 的比率是小数（0.0516 = 5.16%），主源同花顺是百分数。
    本模块统一转成百分数，与 financial.py 的主源口径一致。
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

from src.common.logger import logger
from src.common.utils import normalize_code

# 查询间隔（baostock 是免费公共服务，别打太狠）
_MIN_INTERVAL = 0.05

# baostock 字段 -> 项目字段
_BS_FIELD_MAP = {
    "statDate": "report_date",
    "pubDate": "ann_date",
    "epsTTM": "eps_ttm",
    "roeAvg": "roe",
    "npMargin": "net_margin",
    "gpMargin": "gross_margin",
    "liabilityToAsset": "debt_ratio",
}

# 这些字段 baostock 是小数、主源是百分数 —— 统一到百分数
_PCT_FIELDS = ("roe", "net_margin", "gross_margin", "debt_ratio")


def _bs_code(ts_code: str) -> str:
    """600000 -> sh.600000；000001 -> sz.000001；8xxxxx -> bj.8xxxxx。"""
    code = normalize_code(ts_code)
    if code.startswith(("6", "9")):
        return f"sh.{code}"
    if code.startswith(("4", "8")):
        return f"bj.{code}"
    return f"sz.{code}"


def _current_quarter(now: Optional[datetime] = None) -> int:
    """当前所属报告期季度（1~4）。"""
    now = now or datetime.now()
    return (now.month - 1) // 3 + 1


class BaostockClient:
    """baostock 会话封装：懒登录、失败可重试、查询转 DataFrame。

    与 AKShareClient 的区别：不走 akshare 的限流/缓存机制，baostock 自己
    是 HTTP 长连接会话，这里只做最小必要的间隔控制与异常归一。
    """

    def __init__(self) -> None:
        self._logged_in = False
        self._last_call = 0.0

    # ---------------------------------------------------------------- 会话

    def login(self) -> bool:
        if self._logged_in:
            return True
        try:
            import baostock as bs                        # 延迟导入：未装也不影响主流程
        except ImportError as e:
            raise RuntimeError(
                "baostock 未安装，无法使用财务备源（pip install baostock）"
            ) from e

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        self._logged_in = True
        logger.debug("baostock 登录成功")
        return True

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            import baostock as bs
            bs.logout()
        except Exception as e:                           # noqa: BLE001
            logger.debug(f"baostock 登出异常（可忽略）: {e}")
        finally:
            self._logged_in = False

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        self._last_call = time.time()

    # ---------------------------------------------------------------- 查询

    def _query_one(self, fn_name: str, **kwargs) -> Optional[Dict[str, str]]:
        """执行一次 baostock 查询，返回首行 dict（无数据返回 None）。"""
        import baostock as bs

        self.login()
        self._throttle()
        rs = getattr(bs, fn_name)(**kwargs)
        if rs.error_code != "0":
            raise RuntimeError(f"{fn_name} 失败: {rs.error_msg}")
        if not rs.next():
            return None
        return dict(zip(rs.fields, rs.get_row_data()))

    def query_financial(self, ts_code: str,
                        start_year: int | str = 2014) -> pd.DataFrame:
        """逐季拉取财务数据，返回与 financial.py 主源同构的 DataFrame。

        返回列：report_date / ann_date / eps_ttm / roe / net_margin /
                gross_margin / debt_ratio（缺失列补 None 保持结构稳定）

        注意：没有 bps —— baostock 不提供每股净资产，BP 因子在降级时会缺失。
        """
        code = _bs_code(ts_code)
        now = datetime.now()
        end_year, end_quarter = now.year, _current_quarter(now)
        rows: List[Dict[str, str]] = []

        for year in range(int(start_year), end_year + 1):
            for q in (1, 2, 3, 4):
                if year == end_year and q > end_quarter:
                    continue                              # 未来报告期，跳过
                try:
                    rec = self._query_one("query_profit_data",
                                          code=code, year=year, quarter=q)
                    if rec is None:
                        continue                          # 该期未披露/未上市
                    bal = self._query_one("query_balance_data",
                                          code=code, year=year, quarter=q)
                except RuntimeError as e:
                    logger.warning(f"{code} {year}Q{q} baostock 查询失败: {e}")
                    continue
                if bal:
                    # 只要资产负债率，避免与 profit 的字段互相覆盖
                    if "liabilityToAsset" in bal:
                        rec["liabilityToAsset"] = bal["liabilityToAsset"]
                rows.append(rec)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.rename(columns={k: v for k, v in _BS_FIELD_MAP.items()
                                if k in df.columns})
        df = df.loc[:, ~df.columns.duplicated()]

        if "report_date" not in df.columns:
            return pd.DataFrame()

        # 同一报告期可能被多个季度查询命中，去重保留第一条
        df["report_date"] = pd.to_datetime(df["report_date"],
                                           errors="coerce").dt.date
        if "ann_date" in df.columns:
            df["ann_date"] = pd.to_datetime(df["ann_date"],
                                            errors="coerce").dt.date
        df = (df.dropna(subset=["report_date"])
                .drop_duplicates(subset=["report_date"], keep="first")
                .sort_values("report_date")
                .reset_index(drop=True))

        # 数值化 + 小数比率转百分数（与主源口径一致）
        for c in df.columns:
            if c in ("report_date", "ann_date"):
                continue
            df[c] = pd.to_numeric(df[c], errors="coerce")
            if c in _PCT_FIELDS:
                df[c] = df[c] * 100

        # baostock 的 liabilityToAsset 口径不稳定：某些报告期返回 0.0092 量级
        # 而非 0.92（实测 600000 的 2024Q2~2025Q2 均如此，差 100 倍）。
        # A 股资产负债率不可能低于 1.5%，据此识别并修正，同时告警 ——
        # 宁可多一次提示，也不能把差 100 倍的数静默喂给下游因子。
        if "debt_ratio" in df.columns:
            bad = df["debt_ratio"].abs() < 1.5
            if bad.any():
                logger.warning(
                    f"{code} baostock debt_ratio 有 {int(bad.sum())} 期口径异常"
                    f"（返回 0.0092 量级，应为 0.92 量级），已按 ×100 修正为百分数"
                )
                df.loc[bad, "debt_ratio"] = df.loc[bad, "debt_ratio"] * 100

        # bps 取不到：显式留空，别让下游误以为 0
        if "bps" not in df.columns:
            df["bps"] = pd.NA
        return df

    # ---------------------------------------------------------------- 日线行情

    _DAILY_FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,"
                     "turn,tradestatus,pctChg")

    def query_daily(self, ts_code: str, start_date: str = "2015-01-01",
                    end_date: str = "2050-01-01", adjust: str = "qfq"
                    ) -> pd.DataFrame:
        """拉取日线行情 —— 独立于 akshare 生态的第三层兜底。

        akshare 整体不可用时（东财已先挂过一次），sina/tx 两个源都在 akshare
        生态内，会一起断。baostock 是独立库 + 独立服务端，日线由此兜底。

        返回原始 baostock 列（date/code/open/high/low/close/preclose/volume/
        amount/turn/tradestatus/pctChg，均为字符串），口径归一在
        daily_price._normalize_baostock 做，与新浪/腾讯保持一致。

        复权映射：qfq->前复权(2)  hfq->后复权(1)  none/''->不复权(3)
        注意：baostock 无流通股本序列，float_share 由调用方置空。
        """
        code = _bs_code(ts_code)
        import baostock as bs

        self.login()
        self._throttle()
        adjustflag = {"qfq": "2", "hfq": "1", "none": "3", "": "3"}.get(adjust, "2")
        rs = bs.query_history_k_data_plus(
            code, self._DAILY_FIELDS,
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise RuntimeError(f"query_history_k_data_plus 失败: {rs.error_msg}")
        rows: List[List[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=rs.fields)


_client: Optional[BaostockClient] = None


def get_baostock() -> BaostockClient:
    """模块级单例（baostock 会话是进程级全局的）。"""
    global _client
    if _client is None:
        _client = BaostockClient()
    return _client
