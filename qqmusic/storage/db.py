"""
数据层
- 默认 SQLite, 通过 DATABASE_URL 切换 MySQL
- 自动建表 (init_db)
- SQLite 开 WAL, 支持读写并发
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

_engine = None
_SessionLocal = None


def get_db_url() -> str:
    """
    获取 DB URL

    默认走绝对路径, 避免相对路径受启动目录影响
    (相对路径在不同 CWD 下会生成多个 db 文件)
    """
    from ..utils.constants import DEFAULT_DB_URL
    return os.getenv("DATABASE_URL") or DEFAULT_DB_URL


def get_engine():
    global _engine
    if _engine is None:
        url = get_db_url()
        connect_args = {}
        if url.startswith("sqlite"):
            db_path = url.replace("sqlite:///", "")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            connect_args = {
                "check_same_thread": False,
                "timeout": 30,
            }
        _engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            echo=False,
            future=True,
        )
        if url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

    return _engine


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False)
    return _SessionLocal()


def init_db():
    """初始化所有表"""
    from .models import Song, Comment, CrawlLog, SongCrawlStatus  # noqa: F401
    Base.metadata.create_all(get_engine())
    _run_migrations()


# 老库补列用: 表名 -> [(列名, DDL)]
_MIGRATIONS = {
    "comments": [
        ("ai_emotion", "VARCHAR(30)"),
        ("ai_emotion_secondary", "VARCHAR(30)"),
        ("ai_emotion_intensity", "VARCHAR(10)"),
        ("ai_emotion_keywords", "VARCHAR(200)"),
        ("is_trivial", "INTEGER DEFAULT 0"),
        ("trivial_reason", "VARCHAR(20)"),
    ],
    "song_crawl_status": [
        ("next_pagenum", "INTEGER DEFAULT 0"),
        ("stop_reason", "VARCHAR(20)"),
        ("newest_comment_time", "INTEGER DEFAULT 0"),
    ],
}


def _run_migrations():
    """
    轻量 ALTER TABLE 迁移 (不引 alembic)

    只做"补列", 不做删改: create_all() 负责建新表, 这里负责让
    升级前就存在的老库跟上新字段, 否则查询会报 no such column。
    """
    from sqlalchemy import inspect, text

    engine = get_engine()
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.connect() as conn:
        for table, columns in _MIGRATIONS.items():
            if table not in existing_tables:
                continue  # 新库, create_all 已建好
            cols = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in columns:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        conn.commit()
