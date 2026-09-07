"""
FastAPI 服务

鉴权说明:
  设置环境变量 QQMUSIC_API_KEY 后, 除白名单路径外全部要求
  X-API-Key 请求头。白名单用【精确集合匹配】, 不用 startswith,
  否则 "/" 会让判断恒真、中间件整体空转。
"""
import os
import time
from contextlib import contextmanager
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_

from ..spiders import CommentSpider, SearchSpider
from ..storage import get_session, init_db, Song, Comment, SongCrawlStatus
from ..utils import get_logger, now_cst, clean_content, is_trivial
from ..utils.constants import MAX_PAGE_SIZE

logger = get_logger("qqmusic.api")

# 白名单: 精确匹配, 不做前缀匹配
SKIP_AUTH_PATHS = frozenset({
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})


def _require_api_key(request: Request) -> None:
    api_key = os.getenv("QQMUSIC_API_KEY")
    if not api_key:
        return  # 未配置则不启用鉴权
    if request.headers.get("X-API-Key") == api_key:
        return
    raise HTTPException(status_code=401, detail="缺少或错误的 API Key")


app = FastAPI(
    title="QQ 音乐评论服务",
    description="QQ 音乐评论抓取 / 检索 / AI 情感分析",
    version="0.1.0",
)


@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("服务启动, DB 已初始化")


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if path in SKIP_AUTH_PATHS:
        return await call_next(request)
    try:
        _require_api_key(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    return await call_next(request)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """必须返回 Response 对象。返回 dict 会导致空 body 500, 错误信息全丢。"""
    logger.exception(f"未处理异常 {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "内部错误"})


@contextmanager
def session_scope():
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ==================== Schema ====================
class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "qqmusic"
    time: str


class CommentItem(BaseModel):
    comment_id: str
    song_id: int
    user_nickname: Optional[str] = None
    content: str
    liked_count: int = 0
    comment_time: int = 0
    is_hot: int = 0
    ai_emotion: Optional[str] = None


class SongItem(BaseModel):
    id: Optional[int]
    name: Optional[str]
    singer: Optional[list] = None
    comment_total: int = 0


# ==================== 健康检查 ====================
@app.get("/", response_model=HealthResponse, tags=["health"])
@app.get("/health", response_model=HealthResponse, tags=["health"])
def health():
    return HealthResponse(time=now_cst().strftime("%Y-%m-%d %H:%M:%S"))


# ==================== 搜索 ====================
@app.get("/api/v1/search", tags=["搜索"])
def search(keyword: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=30)):
    """按关键词搜歌, 返回 songid 供后续抓评论"""
    result = SearchSpider().safe_fetch(keyword, limit=limit)
    if result is None:
        raise HTTPException(status_code=502, detail="搜索接口调用失败")
    return result


# ==================== 评论抓取 ====================
@app.get("/api/v1/comment/{song_id}", tags=["评论"])
def get_comment(
    song_id: int,
    pagenum: int = Query(0, ge=0),
    page_size: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    hot: bool = Query(False, description="只返回热评"),
):
    """取单页评论 (不入库)"""
    spider = CommentSpider()
    result = spider.safe_fetch(song_id, pagenum=pagenum, page_size=page_size)
    if result is None:
        raise HTTPException(status_code=502, detail="评论接口调用失败")
    if hot:
        result["comments"] = []
    return result


def _save_comments(s, song_id: int, comments: List[dict]) -> int:
    """
    批量入库, 已存在则跳过

    用 ON CONFLICT DO NOTHING, 避免"先查后插"的竞态。
    """
    if not comments:
        return 0
    saved = 0
    for c in comments:
        exists = s.execute(
            select(Comment.id).where(
                and_(Comment.song_id == song_id, Comment.comment_id == c["comment_id"])
            )
        ).first()
        if exists:
            continue
        s.add(Comment(**{k: v for k, v in c.items() if hasattr(Comment, k)}))
        saved += 1
    s.flush()
    return saved


@app.post("/api/v1/crawl/comment/{song_id}", tags=["爬取"])
def crawl_comment(
    song_id: int,
    max_pages: Optional[int] = Query(None, ge=1, description="最多翻多少页, 不传=翻到底"),
    skip_trivial: bool = Query(True, description="跳过水评 (短于 6 字)"),
):
    """
    翻页抓完整首歌的评论并入库

    注意: 服务端对单歌可翻深度有约 2 万条的上限,
    热门歌抓不全, 入库时会记录 completed 标志。
    """
    started = time.time()
    spider = CommentSpider()
    total_saved = 0

    def on_batch(pagenum: int, batch: List[dict]):
        nonlocal total_saved
        items = batch
        if skip_trivial:
            items = [c for c in batch if not is_trivial(c["content"])]
        with session_scope() as s:
            total_saved += _save_comments(s, song_id, items)

    result = spider.fetch_all(song_id, max_pages=max_pages, on_batch=on_batch)

    # 热评随首个请求一起返回, 需单独入库
    # (可能与普通评论重复, 靠 (song_id, comment_id) 唯一约束去重)
    hot_items = result["hot_comments"]
    if skip_trivial:
        hot_items = [c for c in hot_items if not is_trivial(c["content"])]
    if hot_items:
        with session_scope() as s:
            total_saved += _save_comments(s, song_id, hot_items)

    with session_scope() as s:
        # 歌曲占位: 评论抓取手里只有 songid, 先拿 id 顶名字,
        # 后续走搜索接口时用真实歌名覆盖
        song = s.execute(select(Song).where(Song.id == song_id)).scalar_one_or_none()
        if song is None:
            s.add(Song(id=song_id, name=str(song_id)))

        status = s.execute(
            select(SongCrawlStatus).where(SongCrawlStatus.song_id == song_id)
        ).scalar_one_or_none()
        if status is None:
            status = SongCrawlStatus(song_id=song_id)
            s.add(status)
        status.last_crawled_at = now_cst()
        status.crawl_count = (status.crawl_count or 0) + 1
        status.last_pagenum = result["pages"]
        status.comment_total = result["total"]
        status.comments_fetched = (status.comments_fetched or 0) + total_saved
        status.comments_completed = result["completed"]
        if result["completed"]:
            status.comments_completed_at = now_cst()

    duration_ms = int((time.time() - started) * 1000)
    logger.info(
        f"抓取完成 song={song_id}: 标称 {result['total']}, "
        f"入库 {total_saved}, 翻 {result['pages']} 页, 耗时 {duration_ms}ms"
    )
    return {
        "song_id": song_id,
        "total": result["total"],
        "saved": total_saved,
        "pages": result["pages"],
        "completed": result["completed"],
        "duration_ms": duration_ms,
    }


# ==================== 评论检索 ====================
@app.get("/api/v1/comments/search", tags=["评论检索"])
def search_comments(
    keyword: Optional[str] = Query(None, description="正文关键词"),
    song_id: Optional[int] = Query(None),
    emotion: Optional[str] = Query(None, description="情感标签"),
    min_liked: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """已入库评论的检索"""
    with session_scope() as s:
        stmt = select(Comment)
        conds = []
        if keyword:
            # 转义 LIKE 通配符, 否则用户输入 % 会全表扫描
            safe = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append(Comment.content.like(f"%{safe}%", escape="\\"))
        if song_id:
            conds.append(Comment.song_id == song_id)
        if emotion:
            conds.append(Comment.ai_emotion == emotion)
        if min_liked:
            conds.append(Comment.liked_count >= min_liked)
        if conds:
            stmt = stmt.where(and_(*conds))

        total = s.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        rows = s.execute(
            stmt.order_by(Comment.liked_count.desc()).limit(limit).offset(offset)
        ).scalars().all()

        # 必须在 session 内完成序列化:
        # session 关闭后访问 ORM 属性会抛 DetachedInstanceError
        items = [
            CommentItem(
                comment_id=c.comment_id,
                song_id=c.song_id,
                user_nickname=c.user_nickname,
                content=c.content,
                liked_count=c.liked_count,
                comment_time=c.comment_time,
                is_hot=c.is_hot,
                ai_emotion=c.ai_emotion,
            )
            for c in rows
        ]

    return {
        "total": total,
        "items": items,
    }


@app.get("/api/v1/comments/emotions", tags=["评论检索"])
def emotion_stats(song_id: Optional[int] = Query(None)):
    """情感标签分布"""
    with session_scope() as s:
        stmt = select(Comment.ai_emotion, func.count()).where(
            Comment.ai_emotion.isnot(None), Comment.ai_emotion != ""
        )
        if song_id:
            stmt = stmt.where(Comment.song_id == song_id)
        rows = s.execute(stmt.group_by(Comment.ai_emotion)).all()
    return {"items": [{"emotion": r[0], "count": r[1]} for r in rows]}


# ==================== AI 分析 ====================
@app.post("/api/v1/admin/comments/analyze", tags=["AI"])
def analyze_comments(limit: int = Query(100, ge=1, le=500), min_liked: int = Query(0, ge=0)):
    """对未打标的评论跑 AI 分析 (会消耗 LLM token)"""
    from ..ai import get_analyzer

    analyzer = get_analyzer()
    return analyzer.analyze_pending(limit=limit, min_liked=min_liked)


# ==================== 统计 ====================
@app.get("/api/v1/stats", tags=["统计"])
def stats():
    with session_scope() as s:
        song_count = s.execute(select(func.count(Song.id))).scalar() or 0
        comment_count = s.execute(select(func.count(Comment.id))).scalar() or 0
        analyzed = s.execute(
            select(func.count(Comment.id)).where(Comment.ai_emotion.isnot(None))
        ).scalar() or 0
        hot = s.execute(
            select(func.count(Comment.id)).where(Comment.is_hot == 1)
        ).scalar() or 0
    return {
        "songs": song_count,
        "comments": comment_count,
        "analyzed": analyzed,
        "hot_comments": hot,
    }
