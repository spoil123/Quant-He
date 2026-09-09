# -*- coding: utf-8 -*-
"""
第 1 层增量更新。

增量策略（针对新浪接口"一次返回全历史"的特点专门设计）：
    新浪 stock_zh_a_daily 不论你请求哪段区间，都返回该股全部历史。这反而让事情变简单：
        1. 拉一次拿到全历史；
        2. 拿库内已存区间与它做重叠比对，检测前复权基准是否漂移；
        3. 没漂移 -> 只 INSERT 新增交易日（幂等，重复跑无害）；
        4. 漂移了  -> 说明期间发生分红送股，历史价格被追溯修改，
                     删掉该股受影响区间后整段重写。
    所以「增量更新」省下的是写库量，不是请求量 —— 但写库量恰恰是千万级数据下
    最耗时的部分，收益依然显著。

为什么必须处理复权漂移：
    前复权以最新价为基准往前折算。2023 年分红后，2019 年的前复权价会跟着变。
    如果不重算，回测里 2019-2023 的收益率曲线会在分红日出现假跳空。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

from src.common.config import get_config
from src.common.db import execute, read_sql, transaction_scope
from src.common.logger import logger
from src.common.utils import chunked, normalize_code
from src.layer1_data.cleaner.price_cleaner import clean_price, detect_restatement
from src.layer1_data.fetcher import daily_price as dp
from src.layer1_data.fetcher import financial as fin
from src.layer1_data.fetcher import industry as ind
from src.layer1_data.fetcher import stock_list as sl
from src.layer1_data.storage import repository as repo


# ---------------------------------------------------------------- 股票主表

def update_stock_basic() -> int:
    """更新股票主表（含退市股）。"""
    t0 = datetime.now()
    logger.info("开始更新股票主表")
    try:
        uni = sl.build_universe()
        if uni.empty:
            repo.log_update("stock_basic", "failed", message="股票池构建为空", started_at=t0)
            return 0
        n = repo.save_stock_basic(uni)
        repo.log_update("stock_basic", "success", rows=n, started_at=t0)
        logger.info(f"股票主表更新完成：{n} 只（退市 {int(uni['is_delisted'].sum())} 只）")
        return n
    except Exception as e:                                # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        repo.log_update("stock_basic", "failed", message=msg, started_at=t0)
        logger.error(f"股票主表更新失败: {msg}")
        raise


# ---------------------------------------------------------------- 交易日历

def update_trade_calendar() -> int:
    t0 = datetime.now()
    df = dp.fetch_trade_calendar()
    if df.empty:
        repo.log_update("trade_calendar", "failed", message="未取到数据", started_at=t0)
        return 0
    n = repo.save_trade_calendar(df)
    repo.log_update("trade_calendar", "success", rows=n, started_at=t0)
    logger.info(f"交易日历更新完成：{n} 个交易日")
    return n


# ---------------------------------------------------------------- 指数

def update_index(index_code: str = "000300", start_date: str | None = None) -> int:
    t0 = datetime.now()
    cfg = get_config("universe")
    start = start_date or cfg["history"]["start_date"]
    df = dp.fetch_index_daily(index_code, start_date=start)
    if df.empty:
        repo.log_update("index_daily", "failed", ts_code=index_code,
                        message="未取到数据", started_at=t0)
        return 0
    n = repo.save_index_daily(df)
    repo.log_update("index_daily", "success", ts_code=index_code, rows=n,
                    start_date=start, started_at=t0)
    logger.info(f"指数 {index_code} 更新完成：{n} 行")
    return n


# ---------------------------------------------------------------- 行业

def update_industry() -> int:
    t0 = datetime.now()
    df = ind.fetch_sw_classification()
    if df.empty:
        repo.log_update("industry_classification", "failed",
                        message="申万分类下载失败", started_at=t0)
        return 0
    n = repo.save_industry(df)
    repo.log_update("industry_classification", "success", rows=n, started_at=t0)
    logger.info(f"行业分类更新完成：{n} 行")
    return n


# ---------------------------------------------------------------- 日线

def _is_covered(code: str, start_date: str | None) -> bool:
    """库内 qfq 是否已完整覆盖 [start_date, 最近交易日]。

    新浪接口每次返回全历史，增量逻辑省的是写库量不是请求量。
    对已抓到最近交易日的股票，连请求都省掉 —— 这是全量中断后续跑的
    关键：已完成的部分绝不重抓。
    """
    try:
        cal = read_sql(
            "SELECT MAX(trade_date) AS d FROM trade_calendar "
            "WHERE is_trading = 1 AND trade_date <= CURDATE()"
        )
        last_trade = cal.iloc[0, 0] if not cal.empty else None
        if last_trade is None:
            return False
        # 该股最早可得日：晚于 start_date 的按上市日算（次新股 2015 后上市，
        # 库内 s_first 必然晚于 2015-01-05，不能因此判定"历史缺失"）
        base = start_date or "2015-01-01"
        listed = read_sql(
            "SELECT list_date FROM stock_basic WHERE ts_code = :c", {"c": code}
        )
        if not listed.empty and listed.iloc[0, 0] is not None:
            ld = pd.Timestamp(listed.iloc[0, 0]).date()
            if ld > pd.Timestamp(base).date():
                base = str(ld)
        cal_first = read_sql(
            "SELECT MIN(trade_date) AS d FROM trade_calendar "
            "WHERE is_trading = 1 AND trade_date >= :s",
            {"s": base},
        )
        need_first = (cal_first.iloc[0, 0] if not cal_first.empty else None) \
            or pd.Timestamp(base).date()
        stored = repo.get_stored_prices(code, adj_type="qfq")
        if stored.empty:
            return False
        s_first = pd.to_datetime(stored["trade_date"]).min().date()
        s_last = pd.to_datetime(stored["trade_date"]).max().date()
        need_last = pd.Timestamp(last_trade).date()

        # L2 修复（2026-09-07）：停牌股和退市股的 s_last 永远追不上「最近交易日」，
        # 于是每次增量都被判成未覆盖 -> 每次全量重抓全历史。可停牌期间本来
        # 就没有新行情，重抓是纯浪费（请求省不掉，但解析和写库全白做）。
        # 放宽条件：停牌中 / 已退市的股票只校验历史起点，不要求追平最近交易日。
        if s_last < need_last and _is_dormant(code, need_last):
            return s_first <= pd.Timestamp(need_first).date()
        return s_first <= pd.Timestamp(need_first).date() and s_last >= need_last
    except Exception:                                         # noqa: BLE001
        return False


def _is_dormant(code: str, need_last) -> bool:
    """该股是否处于「不可能有新行情」的状态：停牌中或已退市。"""
    try:
        r = read_sql(
            "SELECT is_suspended FROM daily_price "
            "WHERE ts_code = :c AND adj_type = 'qfq' "
            "ORDER BY trade_date DESC LIMIT 1", {"c": code})
        if not r.empty and int(r.iloc[0, 0] or 0) == 1:
            return True
    except Exception:                                         # noqa: BLE001
        pass
    try:
        dl = read_sql("SELECT delist_date, is_delisted FROM stock_basic "
                      "WHERE ts_code = :c", {"c": code})
        if not dl.empty:
            isdl, dd = dl.iloc[0, 1], dl.iloc[0, 0]
            if isdl is not None and int(isdl or 0) == 1:
                return True
            if dd is not None and pd.Timestamp(dd).date() <= need_last:
                return True
    except Exception:                                         # noqa: BLE001
        pass
    return False


def update_daily_one(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    force_full: bool = False,
    name: str | None = None,
) -> Tuple[int, int]:
    """更新单只股票日线。返回 (price 行数, basic 行数)。

    流程：抓全历史 -> 清洗 -> 与库内比对检测复权漂移 -> 增量或重写。

    name: 股票名称（当前名），用于 ST 股 5% 涨跌停判定。
        仅「增量写入新交易日」时生效；历史区间（全量/整段重写）传入 None，
        不做 ST 猜测 —— 避免把"今天 ST"错标到历史（前视污染）。
    """
    code = normalize_code(ts_code)
    t0 = datetime.now()

    # ---------------- 已完整覆盖则跳过（增量模式专用） ----------------
    if not force_full and _is_covered(code, start_date):
        repo.log_update("daily_price", "skipped", ts_code=code,
                        message="已覆盖全区间，跳过", started_at=t0)
        return 0, 0

    price, basic = dp.fetch_stock_full(code, start_date or "2015-01-01", end_date or "2050-01-01")
    if price.empty:
        repo.log_update("daily_price", "skipped", ts_code=code,
                        message="未取到数据", started_at=t0)
        return 0, 0

    # 清洗全历史时【不】传 name —— 当前名称 ≠ 历史名称，把"今天 ST"标到
    # 2019 年是前视污染。增量新增行（近几日）会在下面单独按当前名补 ST 标记。
    cleaned, report = clean_price(price)
    if cleaned.empty:
        repo.log_update("daily_price", "skipped", ts_code=code,
                        message="清洗后为空", started_at=t0)
        return 0, 0

    # ---------------- 复权漂移检测 ----------------
    stored = repo.get_stored_prices(code, adj_type="qfq")
    restated = False
    if not force_full and not stored.empty:
        fresh_qfq = cleaned[cleaned["adj_type"] == "qfq"]
        restated = detect_restatement(stored, fresh_qfq, adj_type="qfq")

    last_date = repo.get_last_date("daily_price", code)

    if restated or force_full:
        # 两种情况都要整段重写，否则会漏历史：
        #   restated   —— 前复权基准漂移，历史价格已被追溯修改
        #   force_full —— 显式要求全量。典型场景：把起始年份从 2023 提前到 2015，
        #                 增量逻辑只会补「晚于 last_date」的新日期，不会回填
        #                 2015~2023 这段更早的历史，必须先删后写。
        reason = "复权基准漂移" if restated else "force_full 全量重写"
        logger.info(f"{code} 整段重写（{reason}）")
        # H2 修复：DELETE + 重写必须同事务 —— 分开提交时若 save 失败会留下
        # "删了没写"的空洞。且复权漂移只影响 qfq（hfq 以首日为锚不回溯、
        # none 是真实价），只删 qfq，别动其它 adj_type 与已入库 daily_basic。
        with transaction_scope() as conn:
            conn.execute(text("DELETE FROM `daily_price` "
                              "WHERE ts_code = :c AND adj_type = 'qfq'"),
                         {"c": code})
            to_write_price = cleaned[cleaned["adj_type"] == "qfq"]
            n_price = repo.save_daily_price(to_write_price, con=conn)
        # daily_basic 不复权、无回溯，不需重写；若此前为空则补写
        n_basic = 0
        if not basic.empty and repo.get_last_date("daily_basic", code) is None:
            n_basic = repo.save_daily_basic(basic)
        repo.log_update("daily_price", "success", ts_code=code, rows=n_price,
                        start_date=start_date, end_date=end_date,
                        message="restated" if restated else None, started_at=t0)
        return n_price, n_basic

    # ---------------- 增量分支（未复权漂移） ----------------
    if last_date:
        # 只写新增交易日
        to_write_price = cleaned[pd.to_datetime(cleaned["trade_date"])
                                 > pd.Timestamp(last_date)].copy()
        if not to_write_price.empty and name and _is_st_name(name):
            # ST 股涨跌停是 5% 不是板块 10%：对增量新增行按当前名补标。
            # 只动新增行（近几日名称与当前一致），历史行不回溯 —— 无前视。
            to_write_price = _mark_st_rows(to_write_price, name)
        if not basic.empty:
            to_write_basic = basic[pd.to_datetime(basic["trade_date"])
                                   > pd.Timestamp(last_date)].copy()
        else:
            to_write_basic = basic
    else:
        to_write_price = cleaned
        to_write_basic = basic

    n_price = repo.save_daily_price(to_write_price)
    n_basic = repo.save_daily_basic(to_write_basic) if not to_write_basic.empty else 0

    repo.log_update("daily_price", "success", ts_code=code, rows=n_price,
                    start_date=start_date, end_date=end_date,
                    message="restated" if restated else None, started_at=t0)
    return n_price, n_basic


# 跳过代码段（2026-08-30 修正：北交所 920 已确认新浪可取，不再跳过）：
#   200  深市 B 股（港币计价，A 股策略不含）
#   900  沪市 B 股（美元计价，A 股策略不含）
# B 股不是 A 股，跳过是策略口径问题而非数据源限制，已在监控报告说明。
# 北交所（43/83/87/92）新浪 stock_zh_a_daily 支持 bj 前缀，正常抓取。
SKIP_PREFIXES = ("200", "900")


def _is_st_name(name: str | None) -> bool:
    """当前名称是否 ST / 退市整理（用名称判定，与 price_cleaner 口径一致）。"""
    return bool(name) and ("ST" in str(name).upper() or "退" in str(name))


def _mark_st_rows(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """把增量新增行按 ST 5% 涨跌停重标（is_limit_up/down + status=2）。

    只用于「近几日新增行」（此时名称与当前一致，ST 判定无前视）。
    全历史行绝不调用 —— 当前名 ≠ 历史名。
    口径对齐 price_cleaner：change_pct 触及 ±(5%-容差) 判涨跌停。
    """
    out = df.copy()
    lim = 5.0
    tol = 1.2
    chg = pd.to_numeric(out.get("change_pct"), errors="coerce").fillna(0.0)
    susp = pd.to_numeric(out.get("is_suspended"), errors="coerce").fillna(0)
    out["is_limit_up"] = ((chg >= lim - tol) & (susp == 0)).astype(int)
    out["is_limit_down"] = ((chg <= -(lim - tol)) & (susp == 0)).astype(int)
    # status：ST 交易日=2，停牌行保持 0
    out["status"] = 2
    out.loc[susp == 1, "status"] = 0
    return out


def _should_skip(code: str) -> bool:
    """是否跳过抓取（已知无数据源，避免无谓的重试等待）。"""
    return str(code).startswith(SKIP_PREFIXES)


def update_daily_batch(
    ts_codes: List[str],
    start_date: str | None = None,
    end_date: str | None = None,
    force_full: bool = False,
    batch_size: int = 100,
    max_workers: int = 4,
) -> Dict[str, int]:
    """批量更新日线（股票级并发）。返回统计字典。

    并发安全性说明：
        - 多只股票并行抓取，但请求速率仍由 AKShareClient 的全局限流器控制，
          所以并发不会提高对数据源的压力，只是把网络往返等待重叠掉。
        - 单只股票内部是串行的（3 次复权请求 -> 清洗 -> 入库），不存在竞态。
        - 数据库写入走 SQLAlchemy 连接池，engine / session 都是线程安全的；
          每只股票独立 session，insert_ignore 保证幂等。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cfg = get_config("universe")
    start = start_date or cfg["history"]["start_date"]
    end = end_date or cfg["history"].get("end_date") or "2050-01-01"

    stats: Dict[str, int] = {"codes": len(ts_codes), "price_rows": 0, "basic_rows": 0,
                             "failed": 0, "skipped": 0}
    failed_codes: List[str] = []
    t0 = datetime.now()
    lock = threading.Lock()
    done = [0]
    total = len(ts_codes)

    # 批量取股票名（增量 ST 判定用）。一次查全量映射，避免 N+1 查询。
    name_map: Dict[str, str] = {}
    try:
        nm = read_sql("SELECT ts_code, name FROM stock_basic WHERE name IS NOT NULL")
        name_map = dict(zip(nm["ts_code"].astype(str), nm["name"].astype(str)))
    except Exception:                                     # noqa: BLE001
        name_map = {}

    def _one(code: str) -> None:
        # 已知无数据源的代码段直接跳过，别把时间浪费在注定失败的重试上
        if _should_skip(code):
            with lock:
                stats["skipped"] += 1
                done[0] += 1
            return

        # 已完整覆盖全区间的直接跳过（不抓取、不算失败）
        if not force_full and _is_covered(code, start):
            with lock:
                stats["skipped"] += 1
                done[0] += 1
            return

        try:
            n_price, n_basic = update_daily_one(code, start, end,
                                                force_full=force_full,
                                                name=name_map.get(code))
            ok = n_price > 0
        except Exception as e:                            # noqa: BLE001
            logger.error(f"{code} 日线更新异常: {type(e).__name__}: {e}")
            n_price, n_basic, ok = 0, 0, False

        with lock:
            stats["price_rows"] += n_price
            stats["basic_rows"] += n_basic
            done[0] += 1
            if not ok:
                stats["failed"] += 1
                failed_codes.append(code)
            if done[0] % batch_size == 0 or done[0] == total:
                logger.info(
                    f"日线进度 {done[0]}/{total}  已写 {stats['price_rows']:,} 行  "
                    f"失败 {stats['failed']}  "
                    f"耗时 {(datetime.now() - t0).total_seconds():.0f}s"
                )

    workers = max(1, int(max_workers))
    if workers == 1:
        for code in ts_codes:
            _one(code)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, ts_codes))

    stats["failed_codes"] = failed_codes  # type: ignore[assignment]
    logger.info(
        f"日线更新完成：{stats['codes']} 只，写入 {stats['price_rows']:,} 行，"
        f"失败 {stats['failed']}，耗时 {(datetime.now() - t0).total_seconds():.0f}s"
    )
    return stats


# ---------------------------------------------------------------- 财务

def _financial_skip_codes() -> set:
    """返回「可以跳过」的 ts_code：已入库 **且** 已覆盖到市场最新报告期。

    为什么不是单纯「已入库就跳过」：那样在财报披露季会永远拉不到新数据 ——
    库里躺着的是上一期报告，每次都被判为已入库，新财报永远进不来。
    加上「含最新报告期」这个条件后，只有跟上市场进度的股票才跳过。
    """
    try:
        have = read_sql(
            "SELECT ts_code, MAX(report_date) AS rd "
            "FROM financial_indicator GROUP BY ts_code"
        )
        if have is None or have.empty:
            return set()
        rd = have["rd"].astype(str)
        latest = rd.max()
        return set(have.loc[rd >= latest, "ts_code"].astype(str))
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"查询已入库财务失败（本次不跳过）: {type(e).__name__}: {e}")
        return set()


def update_financial(ts_codes: List[str], start_year: str = "2014",
                     batch_size: int = 500, max_workers: int = 8,
                     skip_existing: bool = False) -> Dict[str, int]:
    """批量更新财务数据（分批抓取+分批入库）。

    为什么分批：同花顺接口高频调用会被限流（响应挂起不报错），
    一次性全抓完再入库的风险是——中途卡死，前面抓的全部丢失。
    每 batch_size 只抓完立即入库，卡死最多丢当前批次。

    skip_existing（M5 修复，2026-09-07）：同花顺一次返回全历史，已入库即完整。
    此前 update_all（夜跑主路径）从不跳过，5912 只每次全量重抓：浪费配额、
    易触发限流，还把日常更新拖到小时级；而 `--only financial` 路径早就有跳过
    逻辑 —— 两条路径行为不一致。现在统一由该参数控制，两处共用同一实现。

    注意：需要强制刷新时（如修复上游字段缺失后回填历史）显式传
    skip_existing=False 全量重抓，不要用增量路径去补历史数据。
    """
    t0 = datetime.now()
    skipped = 0
    if skip_existing and ts_codes:
        have = _financial_skip_codes()
        before = len(ts_codes)
        ts_codes = [c for c in ts_codes if normalize_code(c) not in have]
        skipped = before - len(ts_codes)
        logger.info(f"财务增量：{len(ts_codes)} 只待抓（全量 {before}，"
                    f"已入库且含最新报告期跳过 {skipped}）")
        if not ts_codes:
            return {"rows": 0, "failed": 0, "failed_codes": [], "skipped": skipped}

    total_n, total_failed = 0, 0
    all_failed: List[str] = []
    batches = list(chunked(ts_codes, batch_size))
    for i, chunk in enumerate(batches, 1):
        df, failed = fin.fetch_financial_batch(chunk, start_year=start_year,
                                               max_workers=max_workers)
        n = repo.save_financial(df) if not df.empty else 0
        total_n += n
        total_failed += len(failed)
        all_failed += failed
        logger.info(f"财务批次 {i}/{len(batches)}: {len(chunk)} 只 -> 入库 {n} 行, "
                    f"失败 {len(failed)}，累计 {total_n} 行")
    repo.log_update("financial_indicator", "success", rows=total_n,
                    message=f"failed={total_failed}", started_at=t0)
    logger.info(f"财务数据更新完成：{total_n} 行，失败 {total_failed} 只，"
                f"跳过 {skipped} 只，耗时 {(datetime.now() - t0).total_seconds():.0f}s")
    return {"rows": total_n, "failed": total_failed,
            "failed_codes": all_failed, "skipped": skipped}


# ---------------------------------------------------------------- 一键全量/增量

def init_all(codes: Optional[List[str]] = None, start_date: str | None = None,
             max_workers: int = 4) -> Dict:
    """首次建库：主表 -> 日历 -> 指数 -> 行业 -> 日线 -> 财务。"""
    logger.info("=" * 70)
    logger.info("第 1 层：全量初始化")
    logger.info("=" * 70)

    result: Dict[str, object] = {}
    result["stock_basic"] = update_stock_basic()
    result["trade_calendar"] = update_trade_calendar()
    result["index_daily"] = update_index(start_date=start_date)
    result["industry"] = update_industry()

    if codes is None:
        codes = repo.get_stock_codes()

    logger.info(f"开始抓取 {len(codes)} 只股票日线（全量，耗时较长）")
    result["daily"] = update_daily_batch(codes, start_date=start_date, force_full=True,
                                         max_workers=max_workers)

    logger.info("开始抓取财务数据")
    result["financial"] = update_financial(codes)

    return result


def reconcile_daily_basic(lookback_days: int = 30) -> Dict[str, int]:
    """对账补抓 daily_basic 缺口（2026-09-09，第三轮审计 M5）。

    daily_basic 增量只写 > last_date 的行，源接口偶发漏返回时该行永久
    缺失且无自愈路径 —— SQL 实测每日 12-20 只「有价无 basic」（含 300760
    等正常交易大盘股），估值因子（pe/pb/换手）随之静默缺口。

    做法：找近 lookback_days 个自然日内「有价无 basic」的股票，全量重抓
    其 basic（fetch_stock_full 派生），save 走 insert_ignore —— 已有行不动、
    缺失行补齐，天然幂等。
    """
    since = f"(NOW() - INTERVAL {int(lookback_days)} DAY)"
    df = read_sql(
        "SELECT DISTINCT p.ts_code FROM daily_price p "
        "LEFT JOIN daily_basic b ON b.ts_code = p.ts_code "
        "AND b.trade_date = p.trade_date "
        f"WHERE p.trade_date >= {since} AND b.ts_code IS NULL"
    )
    codes = sorted(df["ts_code"].astype(str).tolist()) if not df.empty else []
    if not codes:
        logger.info("daily_basic 对账：无缺口")
        return {"codes": 0, "rows": 0}

    from src.layer1_data.fetcher.daily_price import fetch_stock_full
    total = 0
    for code in codes:
        try:
            _, basic = fetch_stock_full(code)
            if basic is None or basic.empty:
                continue
            n = repo.save_daily_basic(basic)
            total += n
            if n:
                logger.info(f"daily_basic 对账补抓 {code}: 补 {n} 行")
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"daily_basic 对账补抓 {code} 失败: "
                           f"{type(e).__name__}: {e}")
    logger.info(f"daily_basic 对账完成：{len(codes)} 只涉事，补 {total} 行")
    return {"codes": len(codes), "rows": total}


def update_all(codes: Optional[List[str]] = None, max_workers: int = 4) -> Dict:
    """日常增量更新。"""
    logger.info("=" * 70)
    logger.info("第 1 层：增量更新")
    logger.info("=" * 70)

    result: Dict[str, object] = {}
    result["trade_calendar"] = update_trade_calendar()
    result["index_daily"] = update_index()
    result["stock_basic"] = update_stock_basic()

    if codes is None:
        codes = repo.get_stock_codes()

    result["daily"] = update_daily_batch(codes, force_full=False, max_workers=max_workers)
    # M5：basic 缺口对账补抓（源接口偶发漏返回的行由此自愈）
    result["basic_reconcile"] = reconcile_daily_basic()
    # M5：财务走增量（已入库且含最新报告期的跳过），避免夜跑每次全量重抓
    result["financial"] = update_financial(codes, skip_existing=True)
    return result


if __name__ == "__main__":
    print("该模块由 scripts/init_db.py 与 scripts/update_data.py 调用")
