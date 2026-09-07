# -*- coding: utf-8 -*-
"""
AKShare 统一客户端。

解决三个真实问题：
1. 限流：全量抓 5000+ 只股票，不控制频率会被数据源掐断。这里做全局令牌间隔 +
   分批长休息（每 N 只睡 M 秒），参数全在 .env 可调。
2. 重试：网络抖动、接口偶发返回空表、东财改字段名都会抛异常。指数退避重试，
   重试仍失败的记录进 failed 清单，落盘后支持断点续传，不因为一只票挂掉整轮。
3. 缓存：抓过的数据落 parquet，重跑时直接读，避免重复请求（调试阶段省时间）。

用法：
    client = AKShareClient()
    df = client.call("stock_zh_a_hist", symbol="600000", period="daily",
                     start_date="20230101", end_date="20231231", adjust="qfq")
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd

from src.common.config import PROJECT_ROOT, load_dotenv_once
from src.common.logger import logger
from src.common.utils import retry

load_dotenv_once()


class RateLimiter:
    """简单令牌间隔：保证任意两次请求间隔 >= min_interval 秒（线程安全）。"""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.perf_counter()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.perf_counter()


class AKShareClient:
    """AKShare 调用封装：限流 + 重试 + 缓存 + 失败留痕。"""

    def __init__(
        self,
        interval: float | None = None,
        max_retry: int | None = None,
        max_workers: int | None = None,
        batch_size: int | None = None,
        batch_sleep: float | None = None,
        cache_enabled: bool = True,
        cache_dir: str | Path | None = None,
    ):
        self.interval = float(interval if interval is not None else os.getenv("AK_REQUEST_INTERVAL", 0.25))
        self.max_retry = int(max_retry if max_retry is not None else os.getenv("AK_MAX_RETRY", 3))
        self.max_workers = int(max_workers if max_workers is not None else os.getenv("AK_MAX_WORKERS", 4))
        self.batch_size = int(batch_size if batch_size is not None else os.getenv("AK_BATCH_SIZE", 200))
        self.batch_sleep = float(batch_sleep if batch_sleep is not None else os.getenv("AK_BATCH_SLEEP", 5))

        self.limiter = RateLimiter(self.interval)
        self.cache_enabled = cache_enabled
        self.cache_dir = Path(cache_dir) if cache_dir else PROJECT_ROOT / "data" / "cache"
        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.failed_dir = PROJECT_ROOT / "data" / "raw"
        self.failed_dir.mkdir(parents=True, exist_ok=True)

        self._call_count = 0
        self._failed: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 缓存

    def _cache_key(self, func_name: str, kwargs: Dict[str, Any]) -> str:
        payload = json.dumps({"f": func_name, "k": kwargs}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()[:20]

    def _cache_path(self, func_name: str, kwargs: Dict[str, Any]) -> Path:
        return self.cache_dir / f"{func_name}_{self._cache_key(func_name, kwargs)}.parquet"

    def _cache_ttl_seconds(self, func_name: str) -> float:
        """按接口类型给缓存有效期。

        缓存必须带 TTL —— 无 TTL 的缓存会让"时间敏感接口"（日线/指数/日历）
        命中昨天的 parquet，增量更新从缓存生效次日起静默停更（2026-09-06 实测：
        同参数第 3 次请求 0.01s 命中旧缓存，改日期区间仍返回旧数据）。

        分档（.env 可调）：
          · 日线/分钟/指数/日历 —— 每个交易日更新，缓存只兜住同一天的重复请求
          · 财务指标 —— 财报期一天内可能有新公告，3 天是安全上限
          · 行业/股票列表/摘要 —— 低频变化，可长缓存
          · 其余 —— 默认 1 天
        """
        if any(k in func_name for k in (
                "stock_zh_a_daily", "stock_zh_a_hist_tx", "stock_zh_a_minute",
                "stock_zh_index_daily", "stock_zh_index_daily_tx",
                "trade_date_hist")):
            return float(os.getenv("AK_CACHE_TTL_DAILY_HOURS", 6)) * 3600
        if "financial" in func_name:
            return float(os.getenv("AK_CACHE_TTL_FIN_HOURS", 72)) * 3600
        if any(k in func_name for k in ("stock_board", "_abstract",
                                        "stock_basic", "stock_info")):
            return float(os.getenv("AK_CACHE_TTL_SLOW_HOURS", 168)) * 3600
        return float(os.getenv("AK_CACHE_TTL_HOURS", 24)) * 3600

    # ---------------------------------------------------------------- 单次调用

    @staticmethod
    def _fit_kwargs(func_name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """按目标接口的实际签名裁剪参数。

        为什么需要：主备源是不同作者写的不同接口，参数集合天然不齐。本项目
        的实锤案例（2026-09-07）：

            主源 stock_financial_analysis_indicator(symbol, start_year)
            备源 stock_financial_abstract(symbol)          # 不接受 start_year

        未裁剪前 kwargs 原样透传，备源 100% 抛 TypeError，被 call() 的
        except 吞掉后只留一条 debug 日志 —— 结果是"备源配了但从来没生效过"，
        财务三级降级实际只有两级，ann_date_source 全表为 0（真实公告日从未入库）。

        规则：
          · 接口带 **kwargs 时不裁剪（它本就能吞下任意参数）
          · 接口没有的位置参数名（如 start_year）直接剔除
          · 裁剪行为记 debug 日志，便于发现"备源又被悄悄跳过"
        """
        if not kwargs:
            return {}
        try:
            params = inspect.signature(getattr(ak, func_name)).parameters
        except (AttributeError, TypeError, ValueError):
            return dict(kwargs)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs)
        fitted = {k: v for k, v in kwargs.items() if k in params}
        dropped = sorted(set(kwargs) - set(fitted))
        if dropped:
            logger.debug(f"接口 {func_name} 不支持参数 {dropped}，已剔除后调用")
        return fitted

    def call(
        self,
        func_name: str,
        use_cache: bool = True,
        fallbacks: Sequence[str] = (),
        **kwargs: Any,
    ) -> pd.DataFrame:
        """调用一个 AKShare 接口，返回 DataFrame（失败抛异常）。

        P1-4 备源降级：akshare 接口改版/被封 = 单点故障，只做"打不死就重试"
        不够，要"换个源"。fallbacks 传备接口名序列，主接口抛异常或返回空表
        时自动依次尝试备接口，全部失败才抛最后一个异常。

        降级结果通过 df.attrs["_source"] 标注实际数据源（默认等于 func_name），
        调用方可用它留痕"这批数据是从备源来的"，避免把降级数据当主源数据。

        缓存：每个接口各自独立缓存（缓存键含 func_name），备源数据同样落盘。
        """
        funcs: List[str] = [func_name] + [f for f in (fallbacks or ()) if f and f != func_name]
        last_err: Exception | None = None

        for fn in funcs:
            try:
                # 备源参数裁剪：主备源签名不齐是常态（主源要 start_year，备源只收
                # symbol），原样透传会让备源 100% 抛 TypeError —— 备源从未生效过
                # 且只有 debug 日志（2026-09-07 实测：ann_date_source 全表 = 0）。
                df = self._call_one(fn, use_cache=use_cache,
                                    **self._fit_kwargs(fn, kwargs))
                if df is not None and not df.empty:
                    if fn != func_name:
                        df.attrs["_source"] = fn
                        logger.info(f"数据源降级: {func_name} -> {fn}（{len(df)} 行）")
                    return df
                last_err = ValueError(f"接口 {fn} 返回空表")
                logger.debug(f"接口 {fn} 返回空，尝试下一备源")
            except Exception as e:                        # noqa: BLE001
                last_err = e
                if fn != func_name:
                    logger.warning(f"备源 {fn} 也失败: {type(e).__name__}: {e}")
                else:
                    logger.debug(f"主源 {func_name} 失败: {type(e).__name__}: {e}")

        if last_err is None:
            last_err = RuntimeError(f"接口 {func_name} 无可用数据")
        raise last_err

    def _call_one(self, func_name: str, use_cache: bool, **kwargs: Any) -> pd.DataFrame:
        """单接口调用：接口检查 -> 缓存 -> 限流重试 -> 类型检查 -> 计数 -> 缓存写。"""
        if not hasattr(ak, func_name):
            raise AttributeError(f"AKShare 没有接口 {func_name}，请检查 akshare 版本或接口名")

        # 幂等：call() 已裁过一次，这里兜住直接调用 _call_one / call_batch 的路径。
        # 同时保证缓存键用的是"真正传给接口的参数"，避免主备源同数据不同键。
        kwargs = self._fit_kwargs(func_name, kwargs)
        cache_file = self._cache_path(func_name, kwargs)
        if self.cache_enabled and use_cache and cache_file.exists():
            # TTL 检查：超过有效期的缓存视为过期，重新请求（防增量更新命中旧数据）
            age = time.time() - cache_file.stat().st_mtime
            ttl = self._cache_ttl_seconds(func_name)
            if age <= ttl:
                try:
                    df = pd.read_parquet(cache_file)
                    logger.debug(f"[缓存命中] {func_name} {kwargs} -> {len(df)} 行")
                    return df
                except Exception as e:                       # noqa: BLE001
                    logger.warning(f"缓存读取失败，重新请求: {e}")
            else:
                logger.debug(f"[缓存过期] {func_name} 已存 {age/3600:.1f}h > "
                             f"TTL {ttl/3600:.0f}h，重新请求")

        fn = getattr(ak, func_name)

        @retry(times=self.max_retry, delay=1.0, backoff=2.0,
               on_retry=lambda e, n: logger.warning(f"{func_name} 第 {n} 次重试（{type(e).__name__}: {e}）"))
        def _do_call() -> pd.DataFrame:
            self.limiter.wait()
            return fn(**kwargs)

        df = _do_call()

        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"接口 {func_name} 返回了 {type(df)}，不是 DataFrame")

        with self._lock:
            self._call_count += 1

        if self.cache_enabled and not df.empty:
            try:
                df.to_parquet(cache_file, index=False)
                self._cache_warned = False               # 恢复后允许下次再告警
            except Exception as e:                       # noqa: BLE001
                # 同类失败只告警一次，防日志刷屏（pyarrow 缺失/磁盘满时每请求一条）
                if not getattr(self, "_cache_warned", False):
                    self._cache_warned = True
                    logger.warning(f"缓存写入失败（首次告警，后续同类静默，不影响主流程）: "
                                   f"{type(e).__name__}: {str(e)[:120]}")

        logger.debug(f"[请求] {func_name} {kwargs} -> {len(df)} 行")
        return df

    # ---------------------------------------------------------------- 批量调用

    def call_batch(
        self,
        func_name: str,
        symbols: List[str],
        key: str = "symbol",
        extra_kwargs: Dict[str, Any] | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        skip_empty: bool = True,
        max_workers: int | None = None,
        fallbacks: Sequence[str] = (),
    ) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
        """批量调用同一接口（线程池并发 + 全局限流 + 批间休息）。

        关于并发，有个容易误解的点：
            并发并没有提高「每秒请求数」—— 全局 RateLimiter 对所有线程生效，
            每秒最多仍是 1/interval 个请求。并发真正做的是**把网络往返的等待
            时间重叠掉**：
                串行   总耗时 ≈ N × (限流间隔 + 网络响应时间)
                并发4  总耗时 ≈ N × 限流间隔
            实测能把 5912 只全量抓取从 5 小时以上压到 2 小时以内，
            而请求速率没变，被封 IP 的风险不增加。

        批次之间仍长休息（每 batch_size 只睡 batch_sleep 秒），进一步降低限流概率。
        任何一只失败只进 failed 清单，不中断整批，事后可重跑。

        P1-4：fallbacks 传给底层 call()，单只主接口失败自动切备源。

        返回 (成功字典, 失败代码列表)。
        """
        from concurrent.futures import ThreadPoolExecutor

        workers = int(max_workers or self.max_workers)
        workers = max(1, workers)

        results: Dict[str, pd.DataFrame] = {}
        failed: List[str] = []
        extra_kwargs = extra_kwargs or {}
        total = len(symbols)
        done = [0]

        def _work(sym: str) -> None:
            try:
                df = self.call(func_name, **{key: sym}, **extra_kwargs,
                               fallbacks=fallbacks)
                if skip_empty and (df is None or df.empty):
                    logger.debug(f"{sym} 返回空，跳过")
                    with self._lock:
                        failed.append(sym)
                else:
                    with self._lock:
                        results[sym] = df
            except Exception as e:                        # noqa: BLE001
                logger.error(f"{func_name}({sym}) 失败: {type(e).__name__}: {e}")
                with self._lock:
                    failed.append(sym)
                    self._failed.append({"func": func_name, "symbol": sym, "error": str(e)})
            with self._lock:
                done[0] += 1
                if on_progress:
                    on_progress(done[0], total, sym)

        for start in range(0, total, self.batch_size):
            chunk = symbols[start:start + self.batch_size]
            if workers == 1:
                for s in chunk:
                    _work(s)
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    list(ex.map(_work, chunk))

            if start + self.batch_size < total:
                logger.info(
                    f"已处理 {min(start + self.batch_size, total)}/{total}，"
                    f"休息 {self.batch_sleep}s 避免限流"
                )
                time.sleep(self.batch_sleep)

        if failed:
            self._dump_failed(func_name, failed)

        logger.info(f"{func_name}: 成功 {len(results)}，失败 {len(failed)}")
        return results, failed

    # ---------------------------------------------------------------- 失败清单

    def _dump_failed(self, func_name: str, symbols: List[str]) -> None:
        """失败清单落盘，供断点续传。"""
        path = self.failed_dir / f"failed_{func_name}.json"
        try:
            existing = set()
            if path.exists():
                existing = set(json.loads(path.read_text(encoding="utf-8")))
            existing.update(symbols)
            path.write_text(json.dumps(sorted(existing), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.warning(f"失败清单已写入 {path}（{len(existing)} 只），可用 --retry-failed 重跑")
        except Exception as e:                            # noqa: BLE001
            logger.error(f"写入失败清单出错: {e}")

    def load_failed(self, func_name: str) -> List[str]:
        path = self.failed_dir / f"failed_{func_name}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def clear_failed(self, func_name: str) -> None:
        path = self.failed_dir / f"failed_{func_name}.json"
        if path.exists():
            path.unlink()

    # ---------------------------------------------------------------- 统计

    @property
    def call_count(self) -> int:
        return self._call_count

    def reset_stats(self) -> None:
        with self._lock:
            self._call_count = 0
            self._failed = []


# 全局默认客户端
_default_client: Optional[AKShareClient] = None


def get_client(**kwargs: Any) -> AKShareClient:
    """获取默认客户端（单例）。"""
    global _default_client
    if _default_client is None:
        _default_client = AKShareClient(**kwargs)
    return _default_client


if __name__ == "__main__":
    # 连通性自检：拉一只票的日线
    client = get_client()
    df = client.call(
        "stock_zh_a_hist",
        symbol="600000",
        period="daily",
        start_date="20230101",
        end_date="20230331",
        adjust="qfq",
    )
    print(df.head())
    print("列:", list(df.columns))
    print("行数:", len(df))
