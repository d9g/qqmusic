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


def _run_migrations():
    """轻量 ALTER TABLE 迁移 (不引 alembic), 老库补字段用"""
    from sqlalchemy import inspect, text
    engine = get_engine()
    with engine.connect() as conn:
        cols = {c["name"] for c in inspect(engine).get_columns("comments")}
        if "ai_emotion" not in cols:
            for col, ddl in [
                ("ai_emotion", "VARCHAR(30)"),
                ("ai_emotion_secondary", "VARCHAR(30)"),
                ("ai_emotion_intensity", "VARCHAR(10)"),
                ("ai_emotion_keywords", "VARCHAR(200)"),
            ]:
                conn.execute(text(f"ALTER TABLE comments ADD COLUMN {col} {ddl}"))
        conn.commit()
