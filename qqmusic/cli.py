"""
命令行入口

    python -m qqmusic.cli search 孤勇者
    python -m qqmusic.cli comments 331839675 --pages 20 --save
    python -m qqmusic.cli stats
    python -m qqmusic.cli serve --port 8000
"""
from typing import Optional

import typer

from .spiders import CommentSpider, SearchSpider
from .storage import get_session, init_db, Comment, Song, SongCrawlStatus
from .utils import get_logger, now_cst, is_trivial
from .utils.constants import MIN_REQUEST_INTERVAL

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
    pages: Optional[int] = typer.Option(None, "--pages", "-p", help="最多翻多少页"),
    sleep: float = typer.Option(MIN_REQUEST_INTERVAL, "--sleep", help="页间隔秒数"),
    save: bool = typer.Option(False, "--save", help="入库"),
    skip_trivial: bool = typer.Option(True, "--skip-trivial/--keep-trivial", help="跳过水评"),
    show: int = typer.Option(5, "--show", help="预览条数"),
):
    """抓取整首歌的评论"""
    init_db()
    spider = CommentSpider()
    collected = []

    def on_batch(pagenum: int, batch):
        items = [c for c in batch if not is_trivial(c["content"])] if skip_trivial else batch
        collected.extend(items)

    result = spider.fetch_all(song_id, max_pages=pages, sleep=sleep, on_batch=on_batch)

    typer.echo(
        f"标称评论数 {result['total']}, 实抓 {len(collected)} 条, "
        f"翻 {result['pages']} 页, 抓全={bool(result['completed'])}"
    )
    if not result["completed"]:
        typer.secho(
            "注意: 未抓全, 该歌评论数超过服务端可翻深度上限 (约 2 万条)",
            fg=typer.colors.YELLOW,
        )

    for c in collected[:show]:
        typer.echo(f"  [{c['liked_count']}赞] {c['content'][:60]}")

    if save:
        _save(song_id, collected)
        typer.secho(f"已入库 {len(collected)} 条", fg=typer.colors.GREEN)


def _save(song_id: int, items):
    from .storage.db import get_session as _gs

    s = _gs()
    try:
        for c in items:
            exists = s.query(Comment.id).filter(
                Comment.song_id == song_id, Comment.comment_id == c["comment_id"]
            ).first()
            if exists:
                continue
            s.add(Comment(**{k: v for k, v in c.items() if hasattr(Comment, k)}))
        status = s.query(SongCrawlStatus).filter(
            SongCrawlStatus.song_id == song_id
        ).first()
        if status is None:
            status = SongCrawlStatus(song_id=song_id)
            s.add(status)
        status.last_crawled_at = now_cst()
        status.comments_fetched = (status.comments_fetched or 0) + len(items)
        s.commit()
    finally:
        s.close()


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
    finally:
        s.close()
    typer.echo(f"评论 {total} 条, 覆盖 {songs} 首歌, 已分析 {analyzed} 条")


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
