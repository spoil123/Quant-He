# -*- coding: utf-8 -*-
"""
通用工具：日期、代码标准化、分块、重试、计时。

全部为纯函数或轻量装饰器，保证 Windows / Linux 行为一致。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Any, Callable, Iterable, Iterator, List, Sequence, TypeVar

import numpy as np
import pandas as pd

T = TypeVar("T")

# ---------------------------------------------------------------- 日期


def to_date(d: str | date | datetime | pd.Timestamp | None) -> date | None:
    """把各种输入统一成 datetime.date。

    刻意做得"宽容"：脏数据（NaT、NaN、"未知"、"-"）一律返回 None，不抛异常。
    全量抓取 5000+ 只股票时，任何一只的字段格式异常都不该中断整轮任务。
    """
    if d is None:
        return None
    # NaT / NaN 优先识别（pd.isna 对 str 返回 False，对数组会报错，故分开处理）
    try:
        if d is pd.NaT:
            return None
        if not isinstance(d, (str, bytes)) and pd.isna(d):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, pd.Timestamp):
        return d.date() if pd.notna(d) else None

    s = str(d).strip().replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_str(d: str | date | datetime | pd.Timestamp | None, fmt: str = "%Y-%m-%d") -> str | None:
    dt = to_date(d)
    return dt.strftime(fmt) if dt else None


def to_compact(d: str | date | datetime | None) -> str:
    """日期转 YYYYMMDD 紧凑格式（AKShare 多数接口要这种）。"""
    return to_str(d, "%Y%m%d") or ""


def today() -> date:
    return datetime.now().date()


def date_range_chunks(start: str, end: str, chunk_days: int = 365) -> List[tuple[str, str]]:
    """把长区间切成小段，避免单次请求过大。"""
    s, e = to_date(start), to_date(end)
    assert s and e
    out = []
    cur = s
    while cur < e:
        nxt = min(cur + timedelta(days=chunk_days), e)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt + timedelta(days=1)
    return out


# ---------------------------------------------------------------- 代码


def normalize_code(code: str | int) -> str:
    """股票代码统一为 6 位字符串（补前导零、去前后空格、去后缀）。"""
    s = str(code).strip()
    # 去掉 .SH / .SZ / .BJ 之类后缀
    for sep in (".", "_"):
        if sep in s:
            s = s.split(sep)[0]
    # 去掉字母（如 sh600000）
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6)


def code_exchange(code: str) -> str:
    """根据代码判断交易所：SH / SZ / BJ。"""
    c = normalize_code(code)
    if c.startswith(("60", "68", "9", "5")):
        return "SH"
    if c.startswith(("43", "83", "87", "92")):
        return "BJ"
    return "SZ"


def board_of(code: str) -> str:
    """板块：主板 / 创业板 / 科创板 / 北交所。"""
    c = normalize_code(code)
    if c.startswith("688"):
        return "科创板"
    if c.startswith("300") or c.startswith("301"):
        return "创业板"
    if c.startswith(("43", "83", "87", "92")):
        return "北交所"
    return "主板"


# ---------------------------------------------------------------- 分块


def chunked(seq: Sequence[T] | Iterable[T], size: int) -> Iterator[List[T]]:
    """把序列切成固定大小的块。"""
    buf: List[T] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# ---------------------------------------------------------------- 重试


def retry(
    times: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: Callable[[Exception, int], None] | None = None,
):
    """指数退避重试装饰器。

    times     最大尝试次数（含首次）
    delay     首次失败后的等待秒数
    backoff   每次等待时长倍数
    """

    def deco(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            last: Exception | None = None
            # 边界修复：times<=0 时按「只试一次」处理，避免 for 循环空转后
            # raise None（TypeError: exceptions must derive from BaseException）
            n_try = max(1, int(times))
            for attempt in range(1, n_try + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:      # noqa: BLE001
                    last = e
                    if attempt == n_try:
                        break
                    if on_retry:
                        on_retry(e, attempt)
                    time.sleep(wait)
                    wait *= backoff
            raise last  # type: ignore[misc]

        return wrapper

    return deco


# ---------------------------------------------------------------- 计时


def timeit(fn: Callable) -> Callable:
    """打印函数耗时。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        name = getattr(fn, "__name__", "func")
        print(f"[timeit] {name} 耗时 {elapsed:.2f}s")
        return result

    return wrapper


# ---------------------------------------------------------------- 数值


def safe_div(a: pd.Series | float, b: pd.Series | float) -> pd.Series | float:
    """除法：分母为 0 / NaN 时返回 NaN，而不是 inf 或报错。"""
    if isinstance(a, pd.Series) or isinstance(b, pd.Series):
        a = pd.Series(a) if not isinstance(a, pd.Series) else a
        b = pd.Series(b) if not isinstance(b, pd.Series) else b
        return a / b.replace(0, np.nan)
    return np.nan if b == 0 else a / b


def fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x * 100:.{digits}f}%"


def fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:,.{digits}f}"


if __name__ == "__main__":
    print(normalize_code(1), normalize_code("sh600000"), normalize_code("600000.SH"))
    print(code_exchange("600000"), code_exchange("000001"), code_exchange("830799"))
    print(board_of("688001"), board_of("300750"), board_of("000001"), board_of("830799"))
    print(list(chunked(range(7), 3)))
    print(date_range_chunks("2015-01-01", "2016-01-01", 180))
