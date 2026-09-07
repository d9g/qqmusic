"""
东八区时间工具 - 项目统一使用东八区时间 (北京时间)

设计:
- 数据库写入/读取统一用东八区时间
- 全局禁止 CURRENT_TIMESTAMP / datetime.now() 写入 DB
- 所有时间相关代码用 now_cst() 获取东八区时间
"""
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    """返回东八区时间 (naive datetime, 跟 DB schema 一致)"""
    return datetime.now(CST).replace(tzinfo=None)


def today_cst() -> str:
    """返回东八区今天日期字符串 YYYY-MM-DD"""
    return now_cst().strftime("%Y-%m-%d")


def date_offset_cst(days: int = 0) -> str:
    """返回东八区 N 天前/后的日期字符串 YYYY-MM-DD"""
    return (now_cst() + timedelta(days=days)).strftime("%Y-%m-%d")
