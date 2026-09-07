# -*- coding: utf-8 -*-
"""
数据库连接与常用操作封装（SQLAlchemy 2.0）。

职责：
- engine 单例（带连接池）
- 建库 / 执行 .sql 脚本
- session 上下文管理器，自动提交 / 回滚
- 批量写入（分块 executemany）
- 快速读取为 DataFrame

用法：
    from src.common.db import get_engine, session_scope, read_sql, execute_sql_file
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.common.config import PROJECT_ROOT, get_config, get_db_url
from src.common.logger import logger

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine(create_db_if_missing: bool = False) -> Engine:
    """获取全局 engine（单例）。"""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    cfg = get_config("database")
    mysql = cfg["mysql"]
    pool = cfg.get("pool", {})

    if create_db_if_missing:
        create_database()

    url = get_db_url()
    _engine = create_engine(
        url,
        pool_size=int(pool.get("pool_size", 10)),
        max_overflow=int(pool.get("max_overflow", 20)),
        pool_recycle=int(pool.get("pool_recycle", 3600)),
        pool_pre_ping=bool(pool.get("pool_pre_ping", True)),
        echo=bool(pool.get("echo", False)),
        future=True,
    )
    _session_factory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    logger.debug(f"数据库引擎已创建: {mysql['host']}:{mysql['port']}/{mysql['database']}")
    return _engine


def get_server_engine() -> Engine:
    """不指定 database 的 engine，用于创建数据库。"""
    mysql = get_config("database")["mysql"]
    user = quote_plus(str(mysql["user"]))
    pwd = quote_plus(str(mysql["password"] or ""))
    url = (
        f"mysql+pymysql://{user}:{pwd}"
        f"@{mysql['host']}:{mysql['port']}/"
        f"?charset={mysql.get('charset', 'utf8mb4')}"
    )
    return create_engine(url, future=True, pool_pre_ping=True)


def create_database() -> None:
    """创建数据库（若不存在）。"""
    mysql = get_config("database")["mysql"]
    db_name = mysql["database"]
    eng = get_server_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    logger.info(f"数据库 {db_name} 已就绪")
    eng.dispose()


def check_connection() -> bool:
    """测试连接是否可用，返回 True/False。"""
    try:
        eng = get_engine()
        with eng.connect() as conn:
            row = conn.execute(text("SELECT VERSION()")).scalar()
        logger.info(f"MySQL 连接成功，版本: {row}")
        return True
    except Exception as e:                      # noqa: BLE001
        logger.error(f"MySQL 连接失败: {type(e).__name__}: {e}")
        return False


@contextmanager
def transaction_scope() -> Iterator[Engine]:
    """SQLAlchemy 连接级事务：正常提交，异常回滚（供 DELETE+写入原子操作）。"""
    eng = get_engine()
    with eng.begin() as conn:
        yield conn


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性 session：正常则提交，异常则回滚并抛出。"""
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    session: Session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def execute_sql_file(path: str | Path, split_on: str = ";") -> None:
    """执行 .sql 脚本（按分号切分语句，跳过空语句与整行注释）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"SQL 文件不存在: {path}")

    sql_text = path.read_text(encoding="utf-8")
    statements: List[str] = []
    for stmt in sql_text.split(split_on):
        # 去掉整行 -- 注释后判断是否还有实质内容
        stripped = "\n".join(
            line for line in stmt.splitlines() if not line.strip().startswith("--")
        ).strip()
        if stripped:
            statements.append(stmt.strip())

    eng = get_engine()
    with eng.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    logger.info(f"已执行 {path.name}，共 {len(statements)} 条语句")


def read_sql(sql: str, params: Dict[str, Any] | None = None) -> pd.DataFrame:
    """读 SQL 到 DataFrame。"""
    eng = get_engine()
    with eng.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def write_df(
    df: pd.DataFrame,
    table: str,
    method: str | None = None,
    chunk_size: int | None = None,
    con=None,  # noqa: ANN001
) -> int:
    """把 DataFrame 写入表。返回写入行数。

    con: 可选 SQLAlchemy 连接。传入时在该连接（含其外层事务）内写入，
        与同一事务里的 DELETE 等操作原子化 —— 复权重写等"先删后写"
        场景必须用事务化写入，否则删除与写入跨事务，失败留空洞。
    """
    if df.empty:
        return 0
    cfg = get_config("database")["write"]
    method = method or cfg.get("bulk_method", "insert_ignore")
    chunk_size = int(chunk_size or cfg.get("chunk_size", 20000))

    # pandas 的 method 取值有限，映射一下
    if method == "insert_ignore":
        if_exists, pd_method = "append", _insert_ignore
    elif method == "upsert":
        if_exists, pd_method = "append", _upsert
    elif method == "replace":
        if_exists, pd_method = "replace", None
    else:
        if_exists, pd_method = "append", None

    if con is not None:
        df.to_sql(
            name=table,
            con=con,
            if_exists=if_exists,
            index=False,
            chunksize=chunk_size,
            method=pd_method,
        )
    else:
        eng = get_engine()
        df.to_sql(
            name=table,
            con=eng,
            if_exists=if_exists,
            index=False,
            chunksize=chunk_size,
            method=pd_method,
        )
    logger.debug(f"写入 {table}: {len(df)} 行")
    return len(df)


# ---------------- pandas to_sql 的自定义 method ----------------

def _insert_ignore(table, conn, keys, data_iter):  # noqa: ANN001
    """INSERT IGNORE：遇到主键/唯一键冲突则跳过（增量更新主用）。"""
    from sqlalchemy.dialects.mysql import insert

    stmt = insert(table.table).values([dict(zip(keys, row)) for row in data_iter])
    stmt = stmt.prefix_with("IGNORE")
    conn.execute(stmt)


def _upsert(table, conn, keys, data_iter):  # noqa: ANN001
    """INSERT ... ON DUPLICATE KEY UPDATE：冲突则更新。

    财务主备源互清防护：可空列只在「新值为非空」时覆盖旧值。
    财务主源（同花顺）缺 gross_margin/debt_ratio → 对齐时补 None；备源
    （新浪/baostock）有真实值。若用 None 全列覆盖，主备源不同日重跑会互相
    清空，ann_date 也会在真实公告日↔法定日间抖动（污染 PIT 对齐）。
    规则：新值 IS NULL → 保留旧值（COALESCE(新值, 旧值)）。
    语义影响：有意「置空」的操作会失效 —— 本项目无此场景，可接受。
    """
    from sqlalchemy import func
    from sqlalchemy.dialects.mysql import insert

    # pandas to_sql 的 method 回调：table 是 SQLTable，.table 才是 SQLAlchemy Table
    tbl = getattr(table, "table", table)
    rows = [dict(zip(keys, row)) for row in data_iter]
    if not rows:
        return
    stmt = insert(tbl).values(rows)
    updatable = {k for k in keys if k not in ("id",)}
    assign = {
        col: func.coalesce(stmt.inserted[col], tbl.c[col])
        for col in updatable
    }
    stmt = stmt.on_duplicate_key_update(**assign)
    conn.execute(stmt)


def table_exists(table: str) -> bool:
    eng = get_engine()
    from sqlalchemy import inspect

    return inspect(eng).has_table(table)


def row_count(table: str, where: str | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM `{table}`"
    if where:
        sql += f" WHERE {where}"
    return int(read_sql(sql).iloc[0, 0])


def execute(sql: str, params: Sequence[Any] | Dict[str, Any] | None = None,
            need_lastrowid: bool = False):
    """执行非查询语句，返回影响行数；need_lastrowid=True 时返回 (rowcount, lastrowid)。

    为什么需要 lastrowid：trade_store 保存委托/成交后要取回自增主键。
    旧实现用「SELECT MAX(id)」在另一连接查 —— 并发（QMT 回调线程 + 主线程）
    下会取到别人刚插入的行，导致 order_id 串线错配。lastrowid 由驱动在
    同一连接内返回，原子且准确（MySQL 支持）。
    """
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(text(sql), params or {})
        last_id = result.lastrowid if need_lastrowid else None
        rc = result.rowcount or 0
    return (rc, last_id) if need_lastrowid else rc


if __name__ == "__main__":
    ok = check_connection()
    print("连接状态:", "OK" if ok else "FAILED")
    if ok:
        print("SQL 目录:", PROJECT_ROOT / "sql")
