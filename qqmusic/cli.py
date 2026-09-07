"""
命令行入口

    python -m qqmusic.cli search 孤勇者
    python -m qqmusic.cli comments 331839675 --pages 20 --save
    python -m qqmusic.cli comments 331839675 --mode incremental --save
    python -m qqmusic.cli status 331839675
    python -m qqmusic.cli recheck
    python -m qqmusic.cli stats
    python -m qqmusic.cli serve --port 8000
"""
import time as _time
from typing import List, Optional

import typer

from .spiders import CommentSpider, SearchSpider
from .storage import get_session, init_db, Comment, Song, SongCrawlStatus
from .utils import get_logger, now_cst, is_trivial, analyze_quality
from .utils.constants import MIN_REQUEST_INTERVAL, INCREMENTAL_STOP_PAGES

app = typer.Typer(help="QQ 音乐评论抓取工具")
logger = get_logger("qqmusic.cli")


@app.command()
def search(
    keyword: str = typer.Argument(..., help="歌名或歌手"),
    limit: int = typer.Option(10, "--limit", "-n", help="返回条数"),
):
    """搜索歌曲, 输出 songid"""
    result = SearchSpider().safe_fetch(keyword, limit=limit)
    if not result:
        typer.secho("搜索失败", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"共 {result['total']} 条结果:")
    for s in result["songs"]:
        singer = "、".join(x["name"] for x in (s["singer"] or []) if x.get("name"))
        typer.echo(f"  {s['id']:>12}  {s['name']} — {singer}")


@app.command()
def comments(
    song_id: int = typer.Argument(..., help="QQ songid"),
    mode: str = typer.Option(
        "auto", "--mode", "-m",
        help="auto=按上轮进度自动选 / incremental=只捞新评论 / full=从断点继续 / restart=重头来",
    ),
    pages: Optional[int] = typer.Option(None, "--pages", "-p", help="最多翻多少页"),
    sleep: float = typer.Option(MIN_REQUEST_INTERVAL, "--sleep", help="页间隔秒数"),
    save: bool = typer.Option(False, "--save", help="入库"),
    drop_trivial: bool = typer.Option(
        False, "--drop-trivial", help="丢弃水评 (默认入库但打标, 便于后续重算)"
    ),
    show: int = typer.Option(5, "--show", help="预览条数"),
):
    """
    抓取整首歌的评论

    断点续传: 每抓完一页就把游标写回 song_crawl_status.next_pagenum,
    中途 Ctrl+C 或崩了, 下次默认(mode=auto)从断点接着抓, 不会重来。
    已抓全的歌再跑会走增量: 从第一页开始, 连续 2 页无新增即收工,
    只捞新评论 —— 重复跑的代价通常是几个请求而不是几百个。
    """
    init_db()
    spider = CommentSpider()
    collected: List[dict] = []
    trivial_count = 0

    # 决定起始页号 / 增量早停
    s = get_session()
    try:
        st = s.query(SongCrawlStatus).filter(
            SongCrawlStatus.song_id == song_id
        ).first()
        prev_completed = bool(st and st.comments_completed)
        prev_next = (st.next_pagenum if st else 0) or 0
    finally:
        s.close()

    resolved = "incremental" if (mode == "auto" and prev_completed) else (
        "full" if mode == "auto" else mode
    )
    if resolved not in ("incremental", "full", "restart"):
        typer.secho(f"未知 mode: {mode}", fg=typer.colors.RED)
        raise typer.Exit(1)

    start_pagenum = prev_next if resolved == "full" else 0
    stop_after = INCREMENTAL_STOP_PAGES if resolved == "incremental" else None
    if start_pagenum:
        typer.secho(f"从第 {start_pagenum} 页续传", fg=typer.colors.CYAN)

    def on_batch(pagenum: int, batch) -> int:
        nonlocal trivial_count
        for c in batch:
            q = analyze_quality(c["content"], c.get("liked_count", 0))
            c["is_trivial"] = 1 if q.is_trivial else 0
            c["trivial_reason"] = q.reason
        trivial_count += sum(1 for c in batch if c["is_trivial"])
        if drop_trivial:
            batch = [c for c in batch if not c["is_trivial"]]
        collected.extend(batch)
        # 先数"没见过的", 再入库: 反过来的话刚插进去的都算已存在,
        # 增量模式会误判成"没有新评论"而立刻收工
        unseen = _count_unseen(song_id, [c["comment_id"] for c in batch])
        if save:
            # 每页落一次库 + 写回游标, 中断不丢进度
            _save(song_id, batch, next_pagenum=pagenum + 1)
        return unseen

    started = _time.time()
    result = spider.fetch_all(
        song_id,
        max_pages=pages,
        sleep=sleep,
        on_batch=on_batch,
        start_pagenum=start_pagenum,
        stop_after_seen_pages=stop_after,
    )

    typer.echo(
        f"模式 {resolved} | 标称 {result['total']}, 本次实抓 {len(collected)} 条 "
        f"(含水评 {trivial_count} 条), 翻 {result['pages']} 页, "
        f"停止原因 {result['stop_reason']}, 耗时 {_time.time() - started:.1f}s"
    )
    if result["stop_reason"] == "depth_cap":
        typer.secho(
            "注意: 未抓全, 该歌评论数超过服务端可翻深度上限 (约 2 万条)",
            fg=typer.colors.YELLOW,
        )

    for c in collected[:show]:
        tag = "水" if c.get("is_trivial") else "  "
        typer.echo(f"  [{tag}][{c['liked_count']}赞] {c['content'][:60]}")

    if save:
        _finalize(song_id, result)
        typer.secho(
            f"已入库, 库内该歌共 {_count_song(song_id)} 条", fg=typer.colors.GREEN
        )


def _count_unseen(song_id: int, ids) -> int:
    """这一批里有多少条是库里没有的"""
    if not ids:
        return 0
    from sqlalchemy import select as _sel

    s = get_session()
    try:
        existing = set(
            s.execute(
                _sel(Comment.comment_id).where(
                    Comment.song_id == song_id, Comment.comment_id.in_(list(ids))
                )
            ).scalars().all()
        )
    finally:
        s.close()
    return len(set(ids)) - len(existing)


def _count_song(song_id: int) -> int:
    from sqlalchemy import func, select as _sel

    s = get_session()
    try:
        return s.execute(
            _sel(func.count(Comment.id)).where(Comment.song_id == song_id)
        ).scalar() or 0
    finally:
        s.close()


def _finalize(song_id: int, result: dict) -> None:
    """抓完后收尾: 更新状态表"""
    from sqlalchemy import func, select as _sel

    s = get_session()
    try:
        if s.query(Song.id).filter(Song.id == song_id).first() is None:
            s.add(Song(id=song_id, name=str(song_id)))
            s.flush()

        st = s.query(SongCrawlStatus).filter(
            SongCrawlStatus.song_id == song_id
        ).first()
        if st is None:
            st = SongCrawlStatus(song_id=song_id)
            s.add(st)
        keep_cursor = result["stop_reason"] in ("max_pages", "error")
        st.last_crawled_at = now_cst()
        st.next_pagenum = result["next_pagenum"] if keep_cursor else 0
        st.comment_total = result["total"]
        st.stop_reason = result["stop_reason"]
        st.crawl_count = (st.crawl_count or 0) + 1
        st.comments_fetched = _count_song(song_id)
        st.newest_comment_time = s.execute(
            _sel(func.max(Comment.comment_time)).where(Comment.song_id == song_id)
        ).scalar() or 0
        # 增量模式不动"是否抓全"的结论
        if result["stop_reason"] != "incremental":
            st.comments_completed = result["completed"]
            if result["completed"]:
                st.comments_completed_at = now_cst()
        s.commit()
    finally:
        s.close()


def _save(song_id: int, items, next_pagenum: Optional[int] = None):
    from .storage.db import get_session as _gs

    s = _gs()
    try:
        ids = [c["comment_id"] for c in items if c.get("comment_id")]
        existing = set()
        if ids:
            from sqlalchemy import select as _sel

            existing = set(
                s.execute(
                    _sel(Comment.comment_id).where(
                        Comment.song_id == song_id, Comment.comment_id.in_(ids)
                    )
                ).scalars().all()
            )
        for c in items:
            if not c.get("comment_id") or c["comment_id"] in existing:
                continue
            s.add(Comment(**{k: v for k, v in c.items() if hasattr(Comment, k)}))
        st = s.query(SongCrawlStatus).filter(
            SongCrawlStatus.song_id == song_id
        ).first()
        if st is None:
            st = SongCrawlStatus(song_id=song_id)
            s.add(st)
        st.last_crawled_at = now_cst()
        if next_pagenum is not None:
            st.next_pagenum = next_pagenum
        s.commit()
    finally:
        s.close()


@app.command()
def status(song_id: int = typer.Argument(..., help="QQ songid")):
    """查看某首歌的抓取进度"""
    init_db()
    s = get_session()
    try:
        st = s.query(SongCrawlStatus).filter(
            SongCrawlStatus.song_id == song_id
        ).first()
        if st is None:
            typer.secho("这首歌还没抓过", fg=typer.colors.YELLOW)
            raise typer.Exit()
        typer.echo(f"歌曲 {song_id}")
        typer.echo(f"  上次抓取     {st.last_crawled_at}  (第 {st.crawl_count} 次)")
        typer.echo(f"  标称评论数   {st.comment_total}")
        typer.echo(f"  已入库       {st.comments_fetched}")
        typer.echo(f"  是否抓全     {'是' if st.comments_completed else '否'}")
        typer.echo(f"  停止原因     {st.stop_reason}")
        typer.echo(f"  续传游标     next_pagenum={st.next_pagenum}")
        typer.echo(
            f"  下次 auto 会走 {'incremental (只捞新评论)' if st.comments_completed else 'full (从断点续传)'}"
        )
    finally:
        s.close()


@app.command()
def recheck(limit: int = typer.Option(100000, "--limit", help="最多重算多少条")):
    """
    按当前规则重算全库水评标

    水评是打标保留的 (不是丢弃), 所以改完阈值跑一次这个就行, 不用重爬。
    """
    init_db()
    from sqlalchemy import select as _sel

    reasons = {}
    changed = 0
    s = get_session()
    try:
        rows = s.execute(
            _sel(Comment).where(Comment.content.isnot(None)).limit(limit)
        ).scalars().all()
        for c in rows:
            q = analyze_quality(c.content or "", c.liked_count or 0)
            flag = 1 if q.is_trivial else 0
            reasons[q.reason] = reasons.get(q.reason, 0) + 1
            if (c.is_trivial or 0) != flag:
                c.is_trivial = flag
                changed += 1
            c.trivial_reason = q.reason
        s.commit()
    finally:
        s.close()

    typer.echo(f"重算 {sum(reasons.values())} 条, 变更 {changed} 条")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        typer.echo(f"  {k:<8} {v:>6} 条")


@app.command()
def stats():
    """库内统计"""
    init_db()
    from sqlalchemy import func, select as _select

    s = get_session()
    try:
        total = s.execute(_select(func.count(Comment.id))).scalar() or 0
        songs = s.execute(_select(func.count(_select(Comment.song_id).distinct().subquery().c.song_id))).scalar() or 0
        analyzed = s.execute(
            _select(func.count(Comment.id)).where(Comment.ai_emotion.isnot(None))
        ).scalar() or 0
        trivial = s.execute(
            _select(func.count(Comment.id)).where(Comment.is_trivial == 1)
        ).scalar() or 0
    finally:
        s.close()
    pct = (trivial * 100 // total) if total else 0
    typer.echo(
        f"评论 {total} 条 (有效 {total - trivial}, 水评 {trivial} / {pct}%), "
        f"覆盖 {songs} 首歌, 已分析 {analyzed} 条"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
):
    """启动 API 服务"""
    import uvicorn

    uvicorn.run("qqmusic.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
