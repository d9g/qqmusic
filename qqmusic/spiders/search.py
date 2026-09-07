"""
QQ 音乐搜索爬虫

实测结论 (2026-09):
- 只有 search_for_qq_cp 还活着, 返回 songid / songmid
- client_search_cp 与 u.y.qq.com/cgi-bin/musicu.fcg 均已返回空, 勿用
- 无需签名, 直接 GET 即可
"""
from typing import Dict, Any, List

import requests

from .base import BaseSpider
from ..utils.constants import SEARCH_URL, DEFAULT_HEADERS

_BASE_PARAMS = {
    "format": "json",
    "g_tk": "5381",
    "loginUin": "0",
    "hostUin": "0",
    "inCharset": "utf8",
    "outCharset": "utf-8",
    "notice": "0",
    "platform": "yqq.json",
    "needNewCode": "0",
    "remoteplace": "txt.yqq.song",
}


class SearchSpider(BaseSpider):
    name = "search"

    def __init__(self, timeout: int = 15):
        super().__init__()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def fetch(self, keyword: str, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        """
        按关键词搜歌

        :param keyword: 歌名 / 歌手
        :param page: 页码 (从 1 开始)
        :param limit: 每页条数
        :return: {"keyword", "total", "songs": [...]}
        """
        params = {
            **_BASE_PARAMS,
            "w": keyword,
            "p": str(page),
            "n": str(limit),
        }
        resp = self.session.get(SEARCH_URL, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        resp.encoding = "utf-8"
        data = resp.json()

        song_block = (data.get("data") or {}).get("song") or {}
        raw_list = song_block.get("list") or []

        songs: List[Dict[str, Any]] = []
        for item in raw_list:
            singers = [
                {"id": s.get("id"), "name": s.get("name")}
                for s in (item.get("singer") or [])
            ]
            songs.append({
                "id": item.get("songid"),
                "mid": item.get("songmid"),
                "name": item.get("songname"),
                "singer": singers,
                "album_id": item.get("albumid"),
                "album_name": item.get("albumname"),
                "duration": item.get("interval") or 0,  # 秒
            })

        return {
            "keyword": keyword,
            "total": song_block.get("totalnum") or 0,
            "songs": songs,
        }
