"""
后台增量抓取调度器

目标: 已入库歌曲的评论自动保持最新, 不需要人工触发。

风控安全设计 (所有节奏参数都是为了"礼貌抓取", 实测 QQ 匿名接口无频率限制):
- 页间隔: 0.35s (CommentSpider 内置 MIN_REQUEST_INTERVAL)
- 歌曲间隔: 3~5s 随机抖动
- 单轮预算: 最多 30 首歌 / 500 页, 超出留到下一轮
- 默认每 12 小时跑一轮, 每次只走 auto 模式:
  已抓全的歌 -> incremental (通常 2~3 页即收工, 极便宜)
  未抓全的歌 -> full 续传 (受单轮页数预算约束)

环境变量:
- QQMUSIC_SCHEDULER=0                 关闭 (默认开启)
- QQMUSIC_SCHEDULER_INTERVAL_HOURS=12  轮询间隔
- QQMUSIC_SCHEDULER_MAX_SONGS=30       单轮最多歌曲数
- QQMUSIC_SCHEDULER_MAX_PAGES=500      单轮总页数预算
- QQMUSIC_SCHEDULER_SONG_GAP=3         歌曲间隔秒数
"""
import os
import random
import threading
import time
from typing import Optional, Dict, Any

from .utils import get_logger, now_cst

logger = get_logger("qqmusic.scheduler")

_WAKE_POLL = 5  # 秒, 响应 stop/run-now 的最大延迟


class CrawlScheduler:
    def __init__(self):
        self.enabled = (os.getenv("QQMUSIC_SCHEDULER", "1") != "0")
        self.interval_hours = _float_env("QQMUSIC_SCHEDULER_INTERVAL_HOURS", 12.0)
        self.max_songs = _int_env("QQMUSIC_SCHEDULER_MAX_SONGS", 30)
        self.max_pages = _int_env("QQMUSIC_SCHEDULER_MAX_PAGES", 500)
        self.song_gap = _float_env("QQMUSIC_SCHEDULER_SONG_GAP", 3.0)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # 状态快照 (给 /admin/scheduler/status 用)
        self._state: Dict[str, Any] = {
            "running": False,       # 本轮抓取进行中
            "started": False,       # 调度器线程已启动
            "last_run_at": None,
            "last_summary": None,
            "next_run_at": None,
        }

    # -------------------- 生命周期 --------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="crawl-scheduler", daemon=True
        )
        self._thread.start()
        with self._lock:
            self._state["started"] = True
        logger.info(
            f"调度器已启动: 间隔 {self.interval_hours}h, 单轮上限 "
            f"{self.max_songs} 首 / {self.max_pages} 页"
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._state["started"] = False
        logger.info("调度器已停止")

    def run_now(self) -> bool:
        """立即触发一轮 (不打断进行中的一轮)"""
        with self._lock:
            if self._state["running"]:
                return False
        self._wake.set()
        return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._state)
        return {
            "enabled": self.enabled,
            "started": snap["started"],
            "running": snap["running"],
            "last_run_at": snap["last_run_at"],
            "last_summary": snap["last_summary"],
            "next_run_at": snap["next_run_at"],
            "config": {
                "interval_hours": self.interval_hours,
                "max_songs": self.max_songs,
                "max_pages": self.max_pages,
                "song_gap": self.song_gap,
            },
        }

    # -------------------- 内部 --------------------
    def _loop(self) -> None:
        # 启动后先等一个短间隔再跑第一轮, 避免和启动初始化抢资源
        first_delay = min(self.interval_hours * 3600 / 12, 600)
        deadline = time.time() + first_delay
        with self._lock:
            self._state["next_run_at"] = deadline
        while not self._stop.is_set():
            while time.time() < deadline and not self._stop.is_set():
                if self._wake.wait(_WAKE_POLL):
                    self._wake.clear()
                    if self._stop.is_set():
                        return
                    break  # run_now 触发, 提前开跑
                # 定期刷新 next_run_at (给 status 看)
                with self._lock:
                    self._state["next_run_at"] = deadline
            if self._stop.is_set():
                return
            try:
                self._execute()
            except Exception as e:
                logger.error(f"调度轮执行异常: {e}", exc_info=True)
            deadline = time.time() + self.interval_hours * 3600
            with self._lock:
                self._state["next_run_at"] = deadline

    def _pick_songs(self):
        """按最久未更新优先, 取本轮要处理的歌"""
        from sqlalchemy import select
        from .storage import get_session
        from .storage.models import SongCrawlStatus

        s = get_session()
        try:
            return s.execute(
                select(SongCrawlStatus.song_id)
                .order_by(SongCrawlStatus.last_crawled_at.asc())
                .limit(self.max_songs)
            ).scalars().all()
        finally:
            s.close()

    def _execute(self) -> None:
        from .api.app import crawl_comment  # 延迟导入避免循环依赖

        with self._lock:
            self._state["running"] = True

        started = time.time()
        songs = self._pick_songs()
        results = []
        pages_used = 0
        saved_total = 0
        try:
            for song_id in songs:
                if self._stop.is_set():
                    break
                if pages_used >= self.max_pages:
                    logger.info(f"单轮页数预算用尽 ({pages_used} 页), 剩余歌曲留到下一轮")
                    break
                try:
                    # crawl_comment 的 mode/max_pages 是普通关键字参数, 直接传即可
                    r = crawl_comment(
                        song_id,
                        mode="auto",
                        max_pages=100,  # 单歌单轮上限, 防止一首歌吃光预算
                    )
                    pages_used += r.get("pages") or 0
                    saved_total += r.get("saved") or 0
                    results.append({
                        "song_id": song_id,
                        "saved": r.get("saved"),
                        "pages": r.get("pages"),
                        "stop_reason": r.get("stop_reason"),
                    })
                except Exception as e:
                    logger.error(f"调度抓取 song={song_id} 失败: {e}", exc_info=True)
                    results.append({"song_id": song_id, "error": str(e)})
                # 歌曲间隔 + 抖动
                time.sleep(self.song_gap + random.random() * 2)
        finally:
            summary = {
                "songs": len(results),
                "saved_total": saved_total,
                "pages_used": pages_used,
                "duration_s": round(time.time() - started, 1),
                "results": results,
            }
            with self._lock:
                self._state["running"] = False
                self._state["last_run_at"] = now_cst().isoformat()
                self._state["last_summary"] = summary
            logger.info(f"调度轮完成: {summary['songs']} 首, 新增 {saved_total}, "
                        f"{pages_used} 页, {summary['duration_s']}s")


_instance: Optional[CrawlScheduler] = None


def get_scheduler() -> CrawlScheduler:
    global _instance
    if _instance is None:
        _instance = CrawlScheduler()
    return _instance


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or default)
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key) or default)
    except ValueError:
        return default
