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
from sqlalchemy import select, func, and_, desc

from ..spiders import CommentSpider, SearchSpider, QQBrowseSpider, QQAuthSpider
from ..storage import get_session, init_db, Song, Comment, SongCrawlStatus
from ..utils import (
    get_logger, now_cst, clean_content, is_trivial, analyze_quality,
)
from ..utils.constants import MAX_PAGE_SIZE, INCREMENTAL_STOP_PAGES, DATA_DIR
from ..scheduler import get_scheduler

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
    sched = get_scheduler()
    if sched.enabled:
        sched.start()
    else:
        logger.info("调度器已被环境变量禁用 (QQMUSIC_SCHEDULER=0)")


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

    # 先占位歌曲行: 崩溃/半途失败时 songs 表也有记录, 概览不至于空
    with session_scope() as s:
        _ensure_song(s, song_id)

    try:
        result = spider.fetch_all(
            song_id,
            max_pages=max_pages,
            on_batch=on_batch,
            start_pagenum=start_pagenum,
            stop_after_seen_pages=stop_after,
        )
    except Exception as e:
        # QQ 对下架/异常歌曲会直接 HTTP 500, 不能让它炸穿接口;
        # 记为 error 状态并正常返回, 调度器也不会因此中断
        logger.error(f"song {song_id}: 抓取异常: {e}")
        result = {
            "song_id": song_id, "total": 0, "fetched": 0,
            "pages": 0, "start_pagenum": start_pagenum,
            "next_pagenum": start_pagenum,  # 保留游标, 不回退
            "completed": 0, "stop_reason": "error", "hot_comments": [],
            "error": str(e),
        }

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
        "error": result.get("error"),
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


# ==================== 调度器 ====================
@app.get("/api/v1/admin/scheduler/status", tags=["调度"])
def scheduler_status():
    """后台增量抓取调度器状态"""
    return get_scheduler().status()


@app.post("/api/v1/admin/scheduler/start", tags=["调度"])
def scheduler_start():
    """启动调度器线程 (随服务自启, 此接口用于手动恢复)"""
    sched = get_scheduler()
    sched.enabled = True
    sched.start()
    return sched.status()


@app.post("/api/v1/admin/scheduler/stop", tags=["调度"])
def scheduler_stop():
    """停止调度器线程 (重启服务后会按配置自启)"""
    get_scheduler().stop()
    return get_scheduler().status()


@app.post("/api/v1/admin/scheduler/run-now", tags=["调度"])
def scheduler_run_now():
    """立即触发一轮增量抓取 (不等待下个周期; 进行中则拒绝)"""
    sched = get_scheduler()
    if not sched.status()["started"]:
        sched.start()
    ok = sched.run_now()
    if not ok:
        raise HTTPException(status_code=409, detail="一轮抓取正在进行中")
    return {"ok": True, "message": "已触发, 结果稍后可在 status 查看"}


# ==================== 登录 ====================
import json as _json

_LOGIN_PATH = os.path.join(DATA_DIR, ".qq_login.json")


class CookieBody(BaseModel):
    cookie: str = ""


def _save_login(info: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_LOGIN_PATH, "w", encoding="utf-8") as f:
        _json.dump(info, f, ensure_ascii=False)


def _load_login() -> dict:
    try:
        with open(_LOGIN_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _read_saved_cookie() -> str:
    try:
        with open(_COOKIE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


@app.get("/api/v1/auth/status", tags=["登录"])
def auth_status():
    """当前登录态 (是否已保存有效 cookie)"""
    info = _load_login()
    cookie = _read_saved_cookie()
    logged_in = bool(cookie and info.get("uin"))
    return {
        "logged_in": logged_in,
        "uin": info.get("uin"),
        "nickname": info.get("nickname"),
        "method": info.get("method"),  # qr / cookie
        "login_at": info.get("login_at"),
        "has_cookie": bool(cookie),
    }


@app.post("/api/v1/auth/qr/new", tags=["登录"])
def auth_qr_new():
    """生成登录二维码 (base64 PNG), 前端轮询 /auth/qr/poll"""
    try:
        return QQAuthSpider().create_qr()
    except Exception as e:
        logger.error(f"二维码生成失败: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"二维码生成失败: {e}")


@app.get("/api/v1/auth/qr/poll", tags=["登录"])
def auth_qr_poll(token: str = Query(..., min_length=8)):
    """轮询扫码状态。扫码确认成功后自动保存登录态"""
    r = QQAuthSpider().poll(token)
    if r.get("status") == "success":
        cookie = r.pop("cookie")
        try:
            with open(_COOKIE_PATH, "w", encoding="utf-8") as f:
                f.write(cookie)
        except Exception as e:
            logger.error(f"cookie 落盘失败: {e}")
            raise HTTPException(status_code=500, detail="cookie 保存失败")
        _save_login({
            "uin": r["uin"],
            "nickname": r["nickname"],
            "method": "qr",
            "login_at": now_cst().isoformat(),
        })
        logger.info(f"扫码登录成功: uin={r['uin']}, nickname={r['nickname']}")
    return r


@app.post("/api/v1/auth/cookie", tags=["登录"])
def auth_cookie_login(payload: CookieBody):
    """粘贴 QQ 音乐网页 cookie 登录 (扫码不可用时的兜底方式)"""
    cookie = (payload.cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="请粘贴 cookie")
    try:
        # 用「我的歌单」接口验证 cookie 有效性 (需要 uin + p_skey)
        r = QQBrowseSpider().get_my_playlists(cookie)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail={"message": str(e), "login_required": True})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cookie 校验失败: {e}")
    try:
        with open(_COOKIE_PATH, "w", encoding="utf-8") as f:
            f.write(cookie)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cookie 保存失败: {e}")
    _save_login({
        "uin": r["uin"],
        "nickname": f"QQ用户{r['uin']}",
        "method": "cookie",
        "login_at": now_cst().isoformat(),
    })
    logger.info(f"cookie 登录成功: uin={r['uin']}, 歌单 {r['count']} 个")
    return {"uin": r["uin"], "playlist_count": r["count"]}


@app.post("/api/v1/auth/logout", tags=["登录"])
def auth_logout():
    """退出登录: 清除服务器上保存的 cookie 与登录信息"""
    for p in (_COOKIE_PATH, _LOGIN_PATH):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return {"ok": True}


# ==================== 浏览 / 发现 ====================
_COOKIE_PATH = os.path.join(DATA_DIR, ".qq_cookie")


@app.get("/api/v1/browse/categories", tags=["浏览"])
def browse_categories():
    """QQ 音乐分类树 (语种/风格/主题/心情/场景)"""
    tree = QQBrowseSpider().safe_fetch_category_tree()
    if tree is None:
        raise HTTPException(status_code=502, detail="分类接口调用失败")
    return tree


@app.get("/api/v1/browse/playlists", tags=["浏览"])
def browse_playlists(
    category_id: int = Query(..., description="分类 id (来自 /browse/categories)"),
    page: int = Query(0, ge=0),
    sort: int = Query(5, description="5=最热 其它见分类 allsorts"),
    size: int = Query(30, ge=1, le=50),
):
    """按分类列出歌单"""
    r = QQBrowseSpider().safe_fetch_playlists(category_id, page=page, sort=sort, size=size)
    if r is None:
        raise HTTPException(status_code=502, detail="歌单列表接口调用失败")
    return r


@app.get("/api/v1/browse/playlist/{dissid}", tags=["浏览"])
def browse_playlist(dissid: str, song_num: int = Query(100, ge=1, le=300)):
    """歌单详情: 返回歌曲清单 (每行带 songid 供抓评论)"""
    r = QQBrowseSpider().safe_fetch_playlist_detail(dissid, song_num=song_num)
    if r is None:
        raise HTTPException(status_code=502, detail="歌单详情接口调用失败")
    return r


@app.get("/api/v1/browse/toplists", tags=["浏览"])
def browse_toplists():
    """官方榜单目录 (topId -> 名称, 均为实测可用)"""
    from ..spiders.browse import TOPLISTS
    return {"toplists": [{"id": k, "name": v} for k, v in TOPLISTS.items()]}


@app.get("/api/v1/browse/toplist/{top_id}", tags=["浏览"])
def browse_toplist(top_id: int, num: int = Query(50, ge=1, le=100)):
    """官方榜单歌曲 (topId 见 /browse/toplists)"""
    try:
        r = QQBrowseSpider().safe_fetch_toplist(top_id, num=num)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if r is None:
        raise HTTPException(status_code=502, detail="榜单接口调用失败")
    return r


@app.post("/api/v1/browse/my-playlists", tags=["浏览"])
def browse_my_playlists(payload: CookieBody):
    """
    读取登录用户的「我的歌单」。

    未登录 (无保存的 cookie) 返回 401 + login_required, 前端应跳登录页。
    安全说明: cookie 仅保存在服务器 data/.qq_cookie, 不进代码仓库。
    """
    cookie = (payload.cookie or "").strip()
    if cookie:
        try:
            with open(_COOKIE_PATH, "w", encoding="utf-8") as f:
                f.write(cookie)
        except Exception:
            pass
    else:
        cookie = _read_saved_cookie()
    if not cookie:
        raise HTTPException(status_code=401, detail={
            "message": "未登录, 请先在登录页扫码或粘贴 cookie",
            "login_required": True,
        })
    try:
        r = QQBrowseSpider().get_my_playlists(cookie)
    except PermissionError as e:
        # cookie 过期: 清掉失效登录态, 让前端回登录页
        auth_logout()
        raise HTTPException(status_code=401, detail={
            "message": str(e),
            "login_required": True,
        })
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取失败: {e}")
    # 顺手补全登录信息 (粘贴 cookie 走 my-playlists 的旧路径)
    if not _load_login().get("uin"):
        _save_login({
            "uin": r["uin"],
            "nickname": f"QQ用户{r['uin']}",
            "method": "cookie",
            "login_at": now_cst().isoformat(),
        })
    return r


# ==================== 我们的排行中心 (基于已入库数据) ====================
@app.get("/api/v1/rankings/hot-songs", tags=["排行"])
def ranking_hot_songs(limit: int = Query(20, ge=1, le=100)):
    """热门歌曲排行: 按入库评论数排序"""
    with session_scope() as s:
        stmt = (
            select(
                Song.id, Song.name, Song.singer, Song.album_name, Song.mid,
                func.count(Comment.id).label("real_comment_count"),
                func.coalesce(func.sum(Comment.liked_count), 0).label("liked_total"),
            )
            .outerjoin(Comment, Comment.song_id == Song.id)
            .group_by(Song.id, Song.name, Song.singer, Song.album_name, Song.mid)
            .having(func.count(Comment.id) > 0)
            .order_by(desc(func.count(Comment.id)))
            .limit(limit)
        )
        rows = s.execute(stmt).all()
        songs = [
            {
                "rank": i + 1,
                "id": r[0],
                "name": r[1],
                "singer": r[2] or [],
                "album_name": r[3],
                "mid": r[4],
                "comment_total": int(r[5] or 0),
                "liked_total": int(r[6] or 0),
            }
            for i, r in enumerate(rows)
        ]
    return {"count": len(songs), "songs": songs}


@app.get("/api/v1/rankings/hot-comments", tags=["排行"])
def ranking_hot_comments(limit: int = Query(20, ge=1, le=100)):
    """神评论排行: 按点赞数排序"""
    with session_scope() as s:
        stmt = (
            select(Comment, Song.name)
            .join(Song, Song.id == Comment.song_id)
            .where(Comment.liked_count > 0)
            .order_by(desc(Comment.liked_count))
            .limit(limit)
        )
        rows = s.execute(stmt).all()
        comments = [
            {
                "rank": i + 1,
                "comment_id": c.comment_id,
                "song_id": c.song_id,
                "song_name": sn,
                "user_nickname": c.user_nickname,
                "content": c.content or "",
                "liked_count": c.liked_count or 0,
            }
            for i, (c, sn) in enumerate(rows)
        ]
    return {"count": len(comments), "comments": comments}


@app.get("/api/v1/rankings/high-quality", tags=["排行"])
def ranking_high_quality(
    min_score: int = Query(4, ge=0, le=5),
    limit: int = Query(20, ge=1, le=100),
):
    """高质量评论排行: 按 AI 评分 (默认 >=4 星)"""
    with session_scope() as s:
        stmt = (
            select(Comment, Song.name)
            .join(Song, Song.id == Comment.song_id)
            .where(Comment.ai_score >= min_score)
            .order_by(desc(Comment.ai_score), desc(Comment.liked_count))
            .limit(limit)
        )
        rows = s.execute(stmt).all()
        comments = [
            {
                "rank": i + 1,
                "comment_id": c.comment_id,
                "song_id": c.song_id,
                "song_name": sn,
                "user_nickname": c.user_nickname,
                "content": c.content or "",
                "liked_count": c.liked_count or 0,
                "ai_score": c.ai_score,
                "ai_emotion": c.ai_emotion,
            }
            for i, (c, sn) in enumerate(rows)
        ]
    return {"count": len(comments), "comments": comments}


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
