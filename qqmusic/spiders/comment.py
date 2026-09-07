"""
QQ 音乐评论爬虫

实测结论 (2026-09):
- 无需登录, 匿名直抓即可, 登录态不影响返回 (真/假/无 Cookie 三组对照结果一致)
- pagesize 硬上限 25, 传更大的值会静默退化成只返回 10 条
- 单歌可翻页深度约 2 万条, 超过后返回空列表且 code=0 (不是报错, 也不是风控)
- 热评固定 15 条, 与普通评论在同一响应里返回
- 评论 ID 是字符串 (66~110 字符), 时间是秒级时间戳
"""
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
        on_batch: Optional[Callable[[int, List[Dict]], None]] = None,
    ) -> Dict[str, Any]:
        """
        翻页抓完整首歌的评论

        :param song_id: QQ songid
        :param max_pages: 最多翻多少页 (None=不限, 由接口自然返回空为止)
        :param sleep: 页间隔秒数
        :param on_batch: 每页回调 (pagenum, comments), 便于边抓边入库
        :return: {
            "song_id", "total", "fetched", "pages", "completed",
            "hot_comments"
        }
        """
        hot_comments: List[Dict] = []
        fetched = 0
        pagenum = 0
        total = 0
        completed = False

        while True:
            if max_pages is not None and pagenum >= max_pages:
                self.logger.info(f"song {song_id}: 达到 max_pages={max_pages}, 停止")
                break

            result = self.fetch(song_id, pagenum=pagenum)
            total = result["total"]
            if pagenum == 0:
                hot_comments = result["hot_comments"]

            batch = result["comments"]
            if not batch:
                # 空页 = 已翻到底。注意: 也可能是被服务端深度上限截断,
                # 两者无法从响应区分, 只能靠 fetched 与 total 的差距判断
                completed = fetched >= (total or 0)
                break

            fetched += len(batch)
            if on_batch:
                on_batch(pagenum, batch)

            pagenum += 1

            if fetched >= PRACTICAL_DEPTH_CAP:
                self.logger.warning(
                    f"song {song_id}: 已达可翻深度上限 {PRACTICAL_DEPTH_CAP} 条, "
                    f"标称总数 {total}, 本次未抓全"
                )
                break

            if sleep:
                time.sleep(sleep)

        return {
            "song_id": song_id,
            "total": total,
            "fetched": fetched,
            "pages": pagenum,
            "completed": 1 if completed else 0,
            "hot_comments": hot_comments,
        }
