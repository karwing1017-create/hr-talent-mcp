"""
数据库连接与查询执行模块。
负责 PostgreSQL 连接管理、参数化查询、结果集转换。
"""

import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from typing import Any

from config import get_connection_params, logger


@contextmanager
def get_cursor():
    """获取数据库游标的上下文管理器。每次查询自动提交，异常自动回滚。"""
    conn = None
    try:
        conn = psycopg2.connect(**get_connection_params())
        conn.set_session(autocommit=True)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cursor
        cursor.close()
    except psycopg2.Error as e:
        logger.error(f"数据库错误: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def execute_query(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    """
    执行参数化查询，返回结果列表（每行为一个字典）。

    Args:
        sql: SQL 语句，使用 %(param_name)s 风格的参数占位符
        params: 参数字典

    Returns:
        结果列表，每个元素是一行数据的字典
    """
    try:
        with get_cursor() as cursor:
            cursor.execute(sql, params or {})
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except psycopg2.Error as e:
        logger.error(f"查询执行失败: {e}\nSQL: {sql[:200]}\nParams: {params}")
        raise


def execute_scalar(sql: str, params: dict | None = None) -> Any:
    """执行查询，返回单个标量值（如 COUNT 结果）。"""
    try:
        with get_cursor() as cursor:
            cursor.execute(sql, params or {})
            row = cursor.fetchone()
            if row:
                return list(row.values())[0]
            return None
    except psycopg2.Error as e:
        logger.error(f"标量查询失败: {e}\nSQL: {sql[:200]}")
        raise


def check_connection() -> bool:
    """快速检测数据库连接是否可用（用于健康检查端点）。"""
    try:
        with get_cursor() as cursor:
            cursor.execute("SELECT 1")
            return True
    except Exception:
        return False


def get_latest_stat_month(target_date: str, table: str) -> str:
    """
    获取目标月份当月或之前最近的统计月份（DM 层汇总表用）。

    DM 层表按 stat_month 存储月度汇总，此函数确保取到有效数据。

    Args:
        target_date: 目标日期，格式 YYYY-MM-DD
        table: 表名（含 schema）

    Returns:
        实际可用的统计月份字符串
    """
    target_month = target_date[:7]  # YYYY-MM
    sql = f"""
        SELECT MAX(stat_month) AS latest_month
        FROM {table}
        WHERE stat_month::text <= %(target_month)s
    """
    result = execute_query(sql, {"target_month": target_month})
    if result and result[0]["latest_month"]:
        return str(result[0]["latest_month"])
    sql_fallback = f"SELECT MIN(stat_month) AS earliest FROM {table}"
    result = execute_query(sql_fallback)
    if result and result[0]["earliest"]:
        return str(result[0]["earliest"])
    raise ValueError(f"表 {table} 中没有任何统计数据")


def get_latest_snapshot_date(target_date: str, table: str) -> str:
    """
    获取目标日期当天或之前最近的快照日期。
    DWS 表按 stat_date 存储每日快照，此函数确保取到有效数据。

    Args:
        target_date: 目标日期，格式 YYYY-MM-DD
        table: 表名（含 schema）

    Returns:
        实际可用的快照日期字符串
    """
    sql = f"""
        SELECT MAX(stat_date) AS latest_date
        FROM {table}
        WHERE stat_date <= %(target_date)s::date
    """
    result = execute_query(sql, {"target_date": target_date})
    if result and result[0]["latest_date"]:
        return str(result[0]["latest_date"])
    # 如果目标日期之前没有数据，取最早的快照
    sql_fallback = f"SELECT MIN(stat_date) AS earliest FROM {table}"
    result = execute_query(sql_fallback)
    if result and result[0]["earliest"]:
        return str(result[0]["earliest"])
    raise ValueError(f"表 {table} 中没有任何快照数据")
