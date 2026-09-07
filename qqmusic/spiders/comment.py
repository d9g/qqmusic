"""
QQ 音乐评论爬虫

实测结论 (2026-09):
- 无需登录, 匿名直抓即可, 登录态不影响返回 (真/假/无 Cookie 三组对照结果一致)
- pagesize 硬上限 25, 传更大的值会静默退化成只返回 10 条
- 单歌可翻页深度约 2 万条, 超过后返回空列表且 code=0 (不是报错, 也不是风控)
- 热评固定 15 条, 与普通评论在同一响应里返回
- 评论 ID 是字符串 (66~110 字符), 时间是秒级时间戳
"""
import os
import time
from typing import Dict, Any, List, Optional, Callable

import requests

from .base import BaseSpider
from ..utils.constants import (
    COMMENT_URL, DEFAULT_HEADERS, MAX_PAGE_SIZE,
    PRACTICAL_DEPTH_CAP, MIN_REQUEST_INTERVAL,
)
from ..utils.helper import clean_content

# 请求公共参数 (QQ 服务端校验较松, 这套参数实测稳定)
_BASE_PARAMS = {
    "g_tk": "5381",
    "loginUin": "0",
    "hostUin": "0",
    "format": "json",
    "inCharset": "utf8",
    "outCharset": "GB2312",
    "notice": "0",
    "platform": "yqq.json",
    "needNewCode": "0",
    "cid": "205360772",
    "reqtype": "2",
    "biztype": "1",
    "needmusiccrit": "0",
    "lasthotcommentid": "",
    "domain": "qq.com",
    "ct": "24",
    "cv": "10101010",
}

CMD_LATEST = "8"  # 最新评论
CMD_HOT = "6"     # 热门评论


class CommentSpider(BaseSpider):
    name = "comment"

    def __init__(self, timeout: int = 15):
        super().__init__()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        # 评论接口对机房 IP 有定向风控 (2026-09-07 实测: 服务器直连 500, 本机 200)。
        # 被风控时可设 QQMUSIC_PROXY=http://user:pass@host:port 走代理。
        proxy = os.getenv("QQMUSIC_PROXY")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.get(
            COMMENT_URL, params=params, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        # 接口声明 outCharset=GB2312, 实际返回 UTF-8
        resp.encoding = "utf-8"
        return resp.json()

    def fetch(
        self,
        song_id: int,
        pagenum: int = 0,
        page_size: int = MAX_PAGE_SIZE,
        cmd: str = CMD_LATEST,
    ) -> Dict[str, Any]:
        """
        取单页评论

        :param song_id: QQ songid
        :param pagenum: 页码 (从 0 开始)
        :param page_size: 每页条数, 会被强制 clamp 到 25
        :param cmd: "8"=最新, "6"=热门
        :return: {
            "song_id", "total", "hot_comments", "comments", "has_more"
        }
        """
        # 超过 25 会退化成只返回 10 条, 这里直接 clamp
        page_size = min(int(page_size), MAX_PAGE_SIZE)

        params = {
            **_BASE_PARAMS,
            "topid": str(song_id),
            "cmd": cmd,
            "pagenum": str(pagenum),
            "pagesize": str(page_size),
        }

        data = self._request(params)
        comment_block = data.get("comment") or {}
        hot_block = data.get("hot_comment") or {}

        raw_comments = comment_block.get("commentlist") or []
        raw_hot = hot_block.get("commentlist") or []

        comments = [self._parse(item, song_id) for item in raw_comments]
        hot_comments = [self._parse(item, song_id, is_hot=1) for item in raw_hot]

        total = comment_block.get("commenttotal") or 0

        return {
            "song_id": song_id,
            "total": total,
            "hot_comments": hot_comments,
            "comments": comments,
            # 返回条数达到请求条数, 说明后面可能还有
            "has_more": len(raw_comments) >= page_size,
        }

    def _parse(self, item: Dict[str, Any], song_id: int, is_hot: int = 0) -> Dict[str, Any]:
        """把接口原始字段映射成入库结构"""
        raw = item.get("rootcommentcontent") or ""
        return {
            "comment_id": item.get("commentid") or "",
            "song_id": song_id,
            "user_nickname": item.get("nick") or "匿名",
            "raw_content": raw,
            "content": clean_content(raw),
            "liked_count": item.get("praisenum") or 0,
            # 【秒级】时间戳, 与网易项目的毫秒不同, 不可混用
            "comment_time": item.get("time") or 0,
            # 只认"来自哪个 block", 不信 item 自带的 is_hot:
            # 实测普通评论列表里的 is_hot 也大量为 1, 照抄会把全表标成热评
            "is_hot": 1 if is_hot else 0,
        }

    def fetch_all(
        self,
        song_id: int,
        max_pages: Optional[int] = None,
        sleep: float = MIN_REQUEST_INTERVAL,
        on_batch: Optional[Callable[[int, List[Dict]], Optional[int]]] = None,
        start_pagenum: int = 0,
        stop_after_seen_pages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        翻页抓评论

        :param song_id: QQ songid
        :param max_pages: 本次最多翻多少页 (None=不限, 由接口自然返回空为止)
        :param sleep: 页间隔秒数
        :param on_batch: 每页回调 (pagenum, comments) -> 本页新增条数。
               返回值用于增量判断: 连续返回 0 达到 stop_after_seen_pages 时提前收工。
               不需要增量判断时返回 None 即可。
        :param start_pagenum: 起始页号, 断点续传时传上次的 next_pagenum
        :param stop_after_seen_pages: 连续多少页无新增就停。
               评论按时间倒序返回 (已实测页内与跨页均严格递减),
               新评论只会插在前面, 所以撞到已抓过的内容即可收工。
               传 None 表示不做增量判断, 一路翻到底。
        :return: {
            "song_id", "total", "fetched", "pages", "start_pagenum",
            "next_pagenum", "completed", "stop_reason", "hot_comments"
        }
        """
        hot_comments: List[Dict] = []
        fetched = 0
        pagenum = start_pagenum
        total = 0
        stop_reason = "max_pages"
        seen_streak = 0

        while True:
            if max_pages is not None and (pagenum - start_pagenum) >= max_pages:
                stop_reason = "max_pages"
                self.logger.info(f"song {song_id}: 达到 max_pages={max_pages}, 停止")
                break

            result = self.fetch(song_id, pagenum=pagenum)
            total = result["total"]
            # 热评每页都会带回来且内容相同, 取第一次拿到的即可;
            # 续传时起始页不是 0, 所以不能只认 pagenum==0
            if not hot_comments and result["hot_comments"]:
                hot_comments = result["hot_comments"]

            batch = result["comments"]
            if not batch:
                # 空页 = 翻到底。注意: 也可能是被服务端深度上限截断,
                # 两者从响应上无法区分, 只能靠 total 与深度上限的关系判断
                stop_reason = "natural_end"
                break

            fetched += len(batch)

            new_count = None
            if on_batch:
                new_count = on_batch(pagenum, batch)

            pagenum += 1

            # 增量提前收工: 连续 N 页全是已抓过的
            if stop_after_seen_pages is not None and new_count is not None:
                if new_count == 0:
                    seen_streak += 1
                    if seen_streak >= stop_after_seen_pages:
                        stop_reason = "incremental"
                        break
                else:
                    seen_streak = 0

            # 深度上限按"绝对页深度"算, 续传时不能只数本轮条数
            if (start_pagenum + fetched) >= PRACTICAL_DEPTH_CAP:
                self.logger.warning(
                    f"song {song_id}: 已达可翻深度上限 {PRACTICAL_DEPTH_CAP} 条, "
                    f"标称总数 {total}, 本次未抓全"
                )
                stop_reason = "depth_cap"
                break

            if sleep:
                time.sleep(sleep)

        # 只有"自然翻到底且标称总数在可翻深度内"才算抓全。
        # 超过深度上限的歌, 即便翻到空页也必然没抓全。
        completed = 1 if (
            stop_reason == "natural_end" and total <= PRACTICAL_DEPTH_CAP
        ) else 0

        return {
            "song_id": song_id,
            "total": total,
            "fetched": fetched,
            "pages": pagenum - start_pagenum,
            "start_pagenum": start_pagenum,
            "next_pagenum": pagenum,
            "completed": completed,
            "stop_reason": stop_reason,
            "hot_comments": hot_comments,
        }
