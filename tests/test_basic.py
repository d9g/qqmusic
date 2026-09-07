"""基础测试 (不依赖网络)"""
import os
import tempfile

import pytest

from qqmusic.utils.helper import clean_content, is_trivial, analyze_quality
from qqmusic.utils.constants import MAX_PAGE_SIZE, INCREMENTAL_STOP_PAGES


def test_clean_content_removes_emoji():
    raw = "好听[em]e400867[/em][em]e400420[/em]"
    assert clean_content(raw) == "好听"


def test_clean_content_keeps_plain_text():
    assert clean_content("这是一条正常评论") == "这是一条正常评论"


def test_clean_content_collapses_space():
    assert clean_content("a\n\n  b") == "a b"


def test_clean_content_drop_topic():
    assert clean_content("#话题# 内容", drop_topic=True).strip() == "内容"
    assert "#话题#" in clean_content("#话题# 内容", drop_topic=False)


def test_clean_content_removes_media_placeholder():
    # 媒体占位符若不去掉, "[图片]" 会被算成 4 个有效字符干扰长度判定
    assert clean_content("看看[图片]") == "看看"
    assert clean_content("[表情][视频][音频][动图]") == ""


def test_is_trivial():
    assert is_trivial("脸")
    assert is_trivial("")
    assert not is_trivial("这首歌让我想起了很多")


def test_analyze_quality_reasons():
    from qqmusic.utils.helper import analyze_quality

    assert analyze_quality("").reason == "empty"
    assert analyze_quality("评论审核中").reason == "system"
    assert analyze_quality("加我vx www.example.com 看片").reason == "url"
    assert analyze_quality("好听").reason == "short"
    assert analyze_quality("这首歌让我想起了很多").reason == "ok"


def test_analyze_quality_high_like_exempt():
    # 高赞豁免: 短评但点赞数达到阈值, 应保留
    from qqmusic.utils.helper import analyze_quality, TRIVIAL_MIN_LIKED_KEEP

    r = analyze_quality("神评", liked_count=TRIVIAL_MIN_LIKED_KEEP)
    assert not r.is_trivial and r.reason == "ok"
    # 点赞不够, 依然是水评
    assert analyze_quality("神评", liked_count=TRIVIAL_MIN_LIKED_KEEP - 1).reason == "short"


def test_incremental_stop_pages():
    # 增量模式连续 N 页无新增即停, 至少容忍 1 次误判
    assert INCREMENTAL_STOP_PAGES >= 2


def test_trivial_model_columns():
    # 水评是打标保留 (is_trivial + trivial_reason), 断点续传有游标列
    from qqmusic.storage.models import Comment, SongCrawlStatus

    cols = {c.name for c in Comment.__table__.columns}
    assert {"raw_content", "is_trivial", "trivial_reason"} <= cols
    scs_cols = {c.name for c in SongCrawlStatus.__table__.columns}
    assert {"next_pagenum", "stop_reason", "newest_comment_time"} <= scs_cols


def test_page_size_cap():
    """pagesize 必须 clamp 在 25, 否则会被服务端静默退化成 10 条"""
    assert MAX_PAGE_SIZE == 25


def test_models_importable():
    from qqmusic.storage.models import Song, Comment, SongCrawlStatus, CrawlLog

    assert Comment.__tablename__ == "comments"
    # 评论 ID 必须是字符串类型 (QQ 的 commentid 是 66~110 字符)
    assert str(Comment.comment_id.type).startswith("VARCHAR")


def test_init_db_creates_tables():
    from qqmusic.storage import init_db, get_session, Comment

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db}"
        try:
            import qqmusic.storage.db as dbmod

            dbmod._engine = None
            dbmod._SessionLocal = None
            init_db()
            s = get_session()
            try:
                n = s.query(Comment).count()
            finally:
                s.close()
            assert n == 0
        finally:
            os.environ.pop("DATABASE_URL", None)
            import qqmusic.storage.db as dbmod

            # 必须先 dispose 再置空: 否则连接句柄未释放,
            # Windows 上清理临时目录会 PermissionError
            if dbmod._engine:
                dbmod._engine.dispose()
            dbmod._engine = None
            dbmod._SessionLocal = None
