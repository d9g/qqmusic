"""
SQLAlchemy 2.0 风格, 4 张表

与网易项目的关键差异 (均来自 2026-09 实测):
1. comment_id 是 String(200)  —— QQ 的 commentid 是 66~110 字符的字符串,
   形如 "1!CRHVjInz...", 不是整数
2. comment_time 单位是【秒】  —— 网易那边是毫秒, 两边不可混用
3. song_id 是 QQ songid       —— 与网易 song id 区间重叠, 两库不可直接合并
4. 不设 platform 字段         —— 本项目只服务单一平台, 跨平台留到以后统一设计
"""
from ..utils.cst_time import now_cst
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Index, JSON, UniqueConstraint,
)

from .db import Base


class Song(Base):
    __tablename__ = "songs"

    id = Column(Integer, primary_key=True)  # QQ songid
    mid = Column(String(40), index=True)  # songmid
    name = Column(String(500), nullable=False)
    singer = Column(JSON)  # [{id, name}]
    album_id = Column(Integer)
    album_name = Column(String(500))
    duration = Column(Integer, default=0)  # 秒
    comment_total = Column(Integer, default=0)  # 标称评论总数
    created_at = Column(DateTime, default=now_cst)
    updated_at = Column(DateTime, default=now_cst, onupdate=now_cst)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    comment_id = Column(String(200), index=True)  # QQ commentid (字符串, 非整数)
    song_id = Column(Integer, nullable=False, index=True)
    user_nickname = Column(String(200))
    content = Column(Text)  # 已清洗 (去掉 [em] 表情码)
    raw_content = Column(Text)  # 清洗前原文, 排障用
    liked_count = Column(Integer, default=0)  # praisenum
    comment_time = Column(Integer, default=0)  # 【秒级】时间戳 (注意: 非毫秒)
    is_hot = Column(Integer, default=0)  # 0=普通 1=热门
    crawled_at = Column(DateTime, default=now_cst)

    # 水评标记。水评是"打标保留"而非"丢弃":
    # 过滤规则会持续调优, 丢掉就没法按新规则重算了
    is_trivial = Column(Integer, default=0, index=True)  # 1=水评
    trivial_reason = Column(String(20))  # empty / short / system / url

    # AI 评分
    ai_score = Column(Integer, default=-1)  # 0-5 星 (-1=未评分)
    ai_label = Column(String(20))  # "口水" / "中等" / "高质量"
    ai_reason = Column(String(500))
    ai_analyzed_at = Column(DateTime)
    # 情感标签 (26 标签体系, 与网易项目保持一致便于横向对比)
    ai_emotion = Column(String(30), index=True)
    ai_emotion_secondary = Column(String(30))
    ai_emotion_intensity = Column(String(10))
    ai_emotion_keywords = Column(String(200))

    __table_args__ = (
        UniqueConstraint("song_id", "comment_id", name="uq_comments_song_comment"),
        Index("idx_comments_song_time", "song_id", "comment_time"),
        Index("idx_comments_ai_score", "ai_score", "liked_count"),
        Index("idx_comments_emotion", "ai_emotion", "liked_count"),
        Index("idx_comments_song_trivial", "song_id", "is_trivial"),
    )


class SongCrawlStatus(Base):
    """歌曲爬取状态 - 断点续传"""

    __tablename__ = "song_crawl_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    song_id = Column(Integer, nullable=False, unique=True, index=True)
    last_crawled_at = Column(DateTime, default=now_cst, index=True)
    crawl_count = Column(Integer, default=0)
    # QQ 用 pagenum 分页 (不是 offset), 断点记页号
    last_pagenum = Column(Integer, default=0)
    # 【续传游标】下一个要抓的页号, 即"已连续抓完的边界"。
    # 每抓完一页就写回, 中途崩溃不会丢进度 —— last_pagenum 只在整轮结束时
    # 更新, 扛不住中断, 所以另设此列。
    next_pagenum = Column(Integer, default=0)
    comment_total = Column(Integer, default=0)  # 标称总数
    comments_fetched = Column(Integer, default=0)  # 实际入库条数
    # 1=抓全 (标称总数未超过可翻深度), 0=被服务端深度上限截断
    comments_completed = Column(Integer, default=0)
    comments_completed_at = Column(DateTime)
    # 本轮为何停下, 便于判断要不要重试:
    # natural_end  接口返回空页, 真的翻到底了
    # depth_cap    撞上约 2 万条的服务端深度上限
    # max_pages    达到调用方指定的页数上限
    # incremental  增量模式下连续多页无新增, 提前收工
    # error        异常中断, 下次应从 next_pagenum 续传
    stop_reason = Column(String(20))
    # 库内该歌最新评论的时间戳 (秒), 用于判断是否需要增量
    newest_comment_time = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_scs_completed_time", "comments_completed", "last_crawled_at"),
    )


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    spider_name = Column(String(50), nullable=False, index=True)
    target_id = Column(Integer, index=True)
    success = Column(Integer, default=1)  # 1=成功 0=失败
    error_msg = Column(Text)
    duration_ms = Column(Integer, default=0)
    crawled_at = Column(DateTime, default=now_cst, index=True)
