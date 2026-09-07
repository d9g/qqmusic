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
from typing import Dict, List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_

from ..spiders import CommentSpider, SearchSpider
from ..storage import get_session, init_db, Song, Comment, SongCrawlStatus
from ..utils import (
    get_logger, now_cst, clean_content, is_trivial, analyze_quality,
)
from ..utils.constants import MAX_PAGE_SIZE, INCREMENTAL_STOP_PAGES

logger = get_logger("qqmusic.api")

# 抓取模式
#   auto        按上轮状态决定: 已抓全 -> incremental, 否则 -> full 续传 (默认)
#   incremental 从头翻, 撞到已抓过的内容就收工, 只捞新评论
#   full        从 last_pagenum 接着翻, 用于中断后补完
#   restart     无视进度从头翻到底
CRAWL_MODES = ("auto", "incremental", "full", "restart")

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
    is_trivial: int = 0
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


def _mark_quality(items: List[dict]) -> None:
    """给每条评论打水评标 (原地修改)"""
    for c in items:
        q = analyze_quality(c.get("content", ""), c.get("liked_count", 0))
        c["is_trivial"] = 1 if q.is_trivial else 0
        c["trivial_reason"] = q.reason


def _bulk_insert_new(
    s, song_id: int, items: List[dict], drop_trivial: bool = False
) -> tuple:
    """
    批量入库, 返回 (实际插入条数, 本页从未见过的条数)

    两点与旧实现不同:
    1. 用一条 IN 查询批量判重, 而不是每条一个 SELECT —— 25 条评论从
       25 次查询降到 1 次, 大批量抓取时这是主要开销
    2. "未见过的条数"按去重前的全量算, 不受 drop_trivial 影响。
       增量抓取靠它判断"是否已撞到旧内容", 若受过滤影响会提前收工
    """
    if not items:
        return 0, 0
    ids = [c["comment_id"] for c in items if c.get("comment_id")]
    if not ids:
        return 0, 0

    existing = set(
        s.execute(
            select(Comment.comment_id).where(
                Comment.song_id == song_id, Comment.comment_id.in_(ids)
            )
        ).scalars().all()
    )
    unseen = len(set(ids)) - len(existing)

    to_insert = [
        c for c in items
        if c.get("comment_id")
        and c["comment_id"] not in existing
        and not (drop_trivial and c.get("is_trivial"))
    ]
    for c in to_insert:
        s.add(Comment(**{k: v for k, v in c.items() if hasattr(Comment, k)}))
    if to_insert:
        s.flush()
    return len(to_insert), unseen


def _touch_status(s, song_id: int, **kw) -> SongCrawlStatus:
    """取(或建)歌曲爬取状态并更新字段"""
    status = s.execute(
        select(SongCrawlStatus).where(SongCrawlStatus.song_id == song_id)
    ).scalar_one_or_none()
    if status is None:
        status = SongCrawlStatus(song_id=song_id)
        s.add(status)
    for k, v in kw.items():
        setattr(status, k, v)
    return status


def _ensure_song(s, song_id: int) -> None:
    """歌曲占位: 只抓评论时手里只有 songid, 歌名等搜索接口补全时再覆盖"""
    if s.execute(select(Song.id).where(Song.id == song_id)).first() is None:
        s.add(Song(id=song_id, name=str(song_id)))


@app.post("/api/v1/crawl/comment/{song_id}", tags=["爬取"])
def crawl_comment(
    song_id: int,
    mode: str = Query("auto", description="auto / incremental / full / restart"),
    max_pages: Optional[int] = Query(None, ge=1, description="最多翻多少页, 不传=翻到底"),
    drop_trivial: bool = Query(False, description="True=丢弃水评; False=全部入库但打标(推荐)"),
):
    """
    抓整首歌的评论并入库, 支持断点续传与增量更新

    三种典型场景:
    - 首次抓 / 中断后补完: 从 next_pagenum 继续, 每抓完一页就写回游标,
      中途崩溃不会从头再来
    - 已抓全的歌再抓: 走增量, 从第一页开始, 连续 2 页无新增即收工
      (评论按时间倒序返回, 新评论只会插在前面, 所以撞到旧内容就能停)
    - 热门歌: 服务端有约 2 万条的深度上限, 抓不全会记 completed=0

    水评默认入库但打标 (is_trivial), 不丢弃 —— 过滤规则会持续调优,
    丢了就没法按新规则重算了。重算走 POST /api/v1/admin/comments/recheck。
    """
    if mode not in CRAWL_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode 必须是 {', '.join(CRAWL_MODES)}"
        )

    started = time.time()
    spider = CommentSpider()
    total_saved = 0

    # 决定起始页号与是否开启增量早停
    with session_scope() as s:
        status = s.execute(
            select(SongCrawlStatus).where(SongCrawlStatus.song_id == song_id)
        ).scalar_one_or_none()
        prev_completed = bool(status and status.comments_completed)
        prev_next = (status.next_pagenum if status else 0) or 0

    if mode == "auto":
        mode = "incremental" if prev_completed else "full"

    if mode == "incremental":
        start_pagenum, stop_after = 0, INCREMENTAL_STOP_PAGES
    elif mode == "full":
        start_pagenum, stop_after = prev_next, None
    else:  # restart
        start_pagenum, stop_after = 0, None

    if start_pagenum:
        logger.info(f"song {song_id}: 从第 {start_pagenum} 页续传 (mode={mode})")

    def on_batch(pagenum: int, batch: List[dict]) -> int:
        nonlocal total_saved
        _mark_quality(batch)
        with session_scope() as s:
            inserted, unseen = _bulk_insert_new(
                s, song_id, batch, drop_trivial=drop_trivial
            )
            # 每页写回游标, 这是断点续传的关键: 只在整轮结束时更新的话,
            # 中途挂掉就白抓了
            _touch_status(s, song_id, next_pagenum=pagenum + 1)
        total_saved += inserted
        return unseen

    result = spider.fetch_all(
        song_id,
        max_pages=max_pages,
        on_batch=on_batch,
        start_pagenum=start_pagenum,
        stop_after_seen_pages=stop_after,
    )

    # 热评随每次响应一起返回, 单独入库 (可能与普通评论重复, 靠唯一约束去重)
    hot_items = result["hot_comments"]
    _mark_quality(hot_items)
    if hot_items:
        with session_scope() as s:
            total_saved += _bulk_insert_new(
                s, song_id, hot_items, drop_trivial=drop_trivial
            )[0]

    stop_reason = result["stop_reason"]
    # 全量已翻到底 / 增量抓完 -> 游标归零, 下次从头走增量;
    # 中途被 max_pages 截断 -> 保留游标, 下次接着翻
    keep_cursor = stop_reason in ("max_pages", "error")
    next_pagenum = result["next_pagenum"] if keep_cursor else 0

    with session_scope() as s:
        _ensure_song(s, song_id)
        st = _touch_status(
            s, song_id,
            last_crawled_at=now_cst(),
            next_pagenum=next_pagenum,
            comment_total=result["total"],
            stop_reason=stop_reason,
        )
        st.crawl_count = (st.crawl_count or 0) + 1
        st.comments_fetched = (
            s.execute(
                select(func.count(Comment.id)).where(Comment.song_id == song_id)
            ).scalar() or 0
        )
        st.newest_comment_time = (
            s.execute(
                select(func.max(Comment.comment_time)).where(Comment.song_id == song_id)
            ).scalar() or 0
        )
        # 增量模式不改变"是否抓全"的结论: 它是建立在已抓全的前提上的,
        # 若用本轮的 natural_end 去覆盖, 反而会把状态改错
        if stop_reason != "incremental":
            st.comments_completed = result["completed"]
            if result["completed"]:
                st.comments_completed_at = now_cst()
        effective_completed = st.comments_completed

    duration_ms = int((time.time() - started) * 1000)
    logger.info(
        f"抓取完成 song={song_id}: 标称 {result['total']}, 新增 {total_saved}, "
        f"翻 {result['pages']} 页 (自 {start_pagenum}), 模式 {mode}, "
        f"停止原因 {stop_reason}, 耗时 {duration_ms}ms"
    )
    return {
        "song_id": song_id,
        "mode": mode,
        "total": result["total"],
        "saved": total_saved,
        "pages": result["pages"],
        "start_pagenum": start_pagenum,
        "next_pagenum": next_pagenum,
        # 增量模式下本轮的 completed 无意义, 回库里的结论
        "completed": effective_completed,
        "stop_reason": stop_reason,
        "duration_ms": duration_ms,
    }


@app.get("/api/v1/crawl/status/{song_id}", tags=["爬取"])
def crawl_status(song_id: int):
    """查看某首歌的抓取进度 / 续传游标"""
    with session_scope() as s:
        st = s.execute(
            select(SongCrawlStatus).where(SongCrawlStatus.song_id == song_id)
        ).scalar_one_or_none()
        if st is None:
            return {"song_id": song_id, "crawled": False}
        trivial = s.execute(
            select(func.count(Comment.id)).where(
                Comment.song_id == song_id, Comment.is_trivial == 1
            )
        ).scalar() or 0
        return {
            "song_id": song_id,
            "crawled": True,
            "last_crawled_at": str(st.last_crawled_at),
            "crawl_count": st.crawl_count,
            "next_pagenum": st.next_pagenum,
            "comment_total": st.comment_total,
            "comments_fetched": st.comments_fetched,
            "trivial_fetched": trivial,
            "comments_completed": st.comments_completed,
            "stop_reason": st.stop_reason,
            "newest_comment_time": st.newest_comment_time,
            # 下次用 auto 模式会走哪条路
            "next_mode": "incremental" if st.comments_completed else "full",
        }


# ==================== 评论检索 ====================
@app.get("/api/v1/comments/search", tags=["评论检索"])
def search_comments(
    keyword: Optional[str] = Query(None, description="正文关键词"),
    song_id: Optional[int] = Query(None),
    emotion: Optional[str] = Query(None, description="情感标签"),
    min_liked: int = Query(0, ge=0),
    exclude_trivial: bool = Query(True, description="排除水评"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """已入库评论的检索 (默认排除水评)"""
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
        if exclude_trivial:
            conds.append(Comment.is_trivial == 0)
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
                is_trivial=c.is_trivial or 0,
                ai_emotion=c.ai_emotion,
            )
            for c in rows
        ]

    return {
        "total": total,
        "items": items,
    }


@app.get("/api/v1/comments/emotions", tags=["评论检索"])
def emotion_stats(
    song_id: Optional[int] = Query(None),
    exclude_trivial: bool = Query(True, description="排除水评"),
):
    """情感标签分布"""
    with session_scope() as s:
        stmt = select(Comment.ai_emotion, func.count()).where(
            Comment.ai_emotion.isnot(None), Comment.ai_emotion != ""
        )
        if song_id:
            stmt = stmt.where(Comment.song_id == song_id)
        if exclude_trivial:
            stmt = stmt.where(Comment.is_trivial == 0)
        rows = s.execute(stmt.group_by(Comment.ai_emotion)).all()
    return {"items": [{"emotion": r[0], "count": r[1]} for r in rows]}


@app.post("/api/v1/admin/comments/recheck", tags=["AI"])
def recheck_quality(limit: int = Query(100000, ge=1, description="最多重算多少条")):
    """
    按当前规则重算全库水评标

    水评是打标保留的, 所以调完阈值 (TRIVIAL_MIN_LEN 等) 跑一次这个就行,
    不用重新抓取。返回各原因的条数分布。
    """
    started = time.time()
    reasons: Dict[str, int] = {}
    changed = 0
    with session_scope() as s:
        rows = s.execute(
            select(Comment).where(Comment.content.isnot(None)).limit(limit)
        ).scalars().all()
        for c in rows:
            q = analyze_quality(c.content or "", c.liked_count or 0)
            flag = 1 if q.is_trivial else 0
            reasons[q.reason] = reasons.get(q.reason, 0) + 1
            if (c.is_trivial or 0) != flag:
                c.is_trivial = flag
                changed += 1
            c.trivial_reason = q.reason
    duration_ms = int((time.time() - started) * 1000)
    logger.info(f"水评重算完成: {sum(reasons.values())} 条, 变更 {changed} 条")
    return {
        "checked": sum(reasons.values()),
        "changed": changed,
        "reasons": reasons,
        "duration_ms": duration_ms,
    }


# ==================== AI 分析 ====================
@app.post("/api/v1/admin/comments/analyze", tags=["AI"])
def analyze_comments(
    limit: int = Query(100, ge=1, le=500),
    min_liked: int = Query(0, ge=0),
    skip_trivial: bool = Query(True, description="跳过水评, 省 LLM token"),
):
    """对未打标的评论跑 AI 分析 (会消耗 LLM token)"""
    from ..ai import get_analyzer

    analyzer = get_analyzer()
    return analyzer.analyze_pending(
        limit=limit, min_liked=min_liked, skip_trivial=skip_trivial
    )


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
        trivial = s.execute(
            select(func.count(Comment.id)).where(Comment.is_trivial == 1)
        ).scalar() or 0
    return {
        "songs": song_count,
        "comments": comment_count,
        "valid_comments": comment_count - trivial,
        "trivial_comments": trivial,
        "analyzed": analyzed,
        "hot_comments": hot,
    }
