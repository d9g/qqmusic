"""
QQ 音乐「浏览 / 发现」爬虫

实测结论 (2026-09, 服务器 du.d9g.com.cn 验证):
- 分类树 fcg_get_diss_tag_conf.fcg         -> 返回完整分类(语种/风格/主题/心情/场景...)
- 歌单列表 fcg_get_diss_by_tag.fcg         -> 关键: 参数名必须用 categoryId(驼峰),
                                              且【不能带 outCharset】, 否则 list 恒空
- 歌单详情 fcg_v8_playlist_cp.fcg?id=      -> cdlist[0].songlist 含完整歌曲清单
- 官方榜单 musicu.fcg GetDetail(topId)      -> 返回榜单歌曲
- 以上均无需登录 (loginUin=0)
- 歌手/专辑/电台的列表接口需要登录态 qqmusic_key, 匿名返回空, 本期不实现
"""
from typing import Dict, Any, List, Optional
import re
import html
import json
import time
import random

import requests

from .base import BaseSpider
from ..utils.constants import DEFAULT_HEADERS

_CAT_URL = "https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_tag_conf.fcg"
_LIST_URL = "https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg"
_DETAIL_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_playlist_cp.fcg"
_TOPLIST_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_MYDISS_URL = "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_create_diss"

# 匿名基础参数 (注意: 歌单列表不要带 outCharset)
_BASE = {
    "g_tk": "5381",
    "loginUin": "0",
    "hostUin": "0",
    "format": "json",
    "inCharset": "utf8",
    "notice": "0",
    "platform": "yqq.json",
    "needNewCode": "1",
}

# 官方榜单目录 (topId -> 名称), 均为 2026-09 实测可用
TOPLISTS = {
    26: "热歌榜", 27: "新歌榜", 28: "网络歌曲榜", 29: "影视金曲榜",
    36: "K歌金曲榜", 52: "腾讯音乐人原创榜", 62: "飙升榜", 63: "DJ舞曲榜",
    64: "综艺新歌榜", 65: "国风热歌榜", 66: "ACG新歌榜", 67: "听歌识曲榜",
    74: "Q音快手榜", 75: "有声榜", 76: "音乐巅峰榜", 108: "美国公告牌榜",
    113: "香港电台榜", 114: "香港商台榜",
}


def _unescape(s: Any) -> str:
    """QQ 返回的分类/歌单名常带 HTML 实体 (如 &#128171;), 还原成正常文本"""
    if not isinstance(s, str):
        return str(s)
    return html.unescape(s).strip()


def _gtk(p_skey: str) -> int:
    """QQ g_tk 算法: 由 p_skey cookie 计算"""
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return h & 2147483647


class QQBrowseSpider(BaseSpider):
    name = "browse"

    def __init__(self, timeout: int = 15):
        super().__init__()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._cat_cache: Optional[Dict] = None
        self._cat_cache_at: float = 0

    # -------------------- 分类树 --------------------
    def get_category_tree(self) -> Dict[str, Any]:
        """返回 {updated, groups:[{group, items:[{id,name}]}]}"""
        now = time.time()
        if self._cat_cache and now - self._cat_cache_at < 3600:
            return self._cat_cache
        params = {**_BASE}
        resp = self.session.get(_CAT_URL, params=params, timeout=self.timeout)
        resp.encoding = "utf-8"
        data = resp.json()
        groups = []
        for grp in (data.get("data", {}).get("categories") or []):
            gname = _unescape(grp.get("categoryGroupName", ""))
            items = []
            for it in grp.get("items", []):
                cid = it.get("categoryId")
                name = _unescape(it.get("categoryName", ""))
                if cid is None or not name:
                    continue
                # 过滤掉 "全部" 这种占位分类 (categoryId=10000000 是组根)
                items.append({"id": int(cid), "name": name})
            if items:
                groups.append({"group": gname, "items": items})
        result = {"updated": int(now), "groups": groups}
        self._cat_cache = result
        self._cat_cache_at = now
        return result

    # -------------------- 歌单列表 --------------------
    def get_playlists(
        self, category_id: int, page: int = 0, sort: int = 5, size: int = 30
    ) -> Dict[str, Any]:
        """按分类列歌单。sort: 5=最热(默认) 其它取值见分类树 allsorts"""
        sin = page * size
        ein = sin + size - 1
        params = {
            **_BASE,
            "picmid": "1",
            "rnd": str(round(random.random(), 16)),
            "categoryId": str(category_id),
            "sortId": str(sort),
            "sin": str(sin),
            "ein": str(ein),
            "sum": str(size),
        }
        resp = self.session.get(_LIST_URL, params=params, timeout=self.timeout)
        resp.encoding = "utf-8"
        data = resp.json().get("data", {})
        out = []
        for p in data.get("list", []) or []:
            creator = p.get("creator") or {}
            cover = p.get("imgurl") or p.get("logo") or ""
            cover = re.sub(r"/\d+$", "/300", cover) if cover else cover
            out.append({
                "dissid": str(p.get("dissid")),
                "name": _unescape(p.get("dissname", "")),
                "cover": cover,
                "listen_num": int(p.get("listennum") or 0),
                "creator": _unescape(creator.get("name", "") if isinstance(creator, dict) else ""),
                "tags": _unescape(p.get("tags", "")),
            })
        return {
            "category_id": category_id,
            "page": page,
            "size": size,
            "total": len(out),
            "playlists": out,
        }

    # -------------------- 歌单详情(歌曲清单) --------------------
    def get_playlist_detail(self, dissid: str, song_num: int = 100) -> Dict[str, Any]:
        params = {
            **_BASE,
            "type": "1", "json": "1", "utf8": "1", "onlysong": "0",
            "new_format": "1", "id": str(dissid),
        }
        resp = self.session.get(_DETAIL_URL, params=params, timeout=self.timeout)
        resp.encoding = "utf-8"
        cd = (resp.json().get("data", {}).get("cdlist") or [{}])[0]
        songs = []
        for s in (cd.get("songlist") or [])[:song_num]:
            singers = [
                {"id": sg.get("id"), "name": _unescape(sg.get("name"))}
                for sg in (s.get("singer") or [])
            ]
            songs.append({
                "songid": int(s.get("songid") or 0),
                "songmid": s.get("songmid") or "",
                "name": _unescape(s.get("songname", "")),
                "album": _unescape(s.get("albumname", "")),
                "singer": singers,
                "duration": int(s.get("interval") or 0),
            })
        return {
            "dissid": str(dissid),
            "name": _unescape(cd.get("dissname", "")),
            "desc": _unescape(cd.get("desc", "")),
            "song_count": len(songs),
            "songs": songs,
        }

    # -------------------- 官方榜单 --------------------
    def get_toplist(self, top_id: int, num: int = 50) -> Dict[str, Any]:
        if top_id not in TOPLISTS:
            raise ValueError(f"未知榜单 topId={top_id}")
        data = {
            "detail": {
                "module": "musicToplist.ToplistInfoServer",
                "method": "GetDetail",
                "param": {
                    "topId": top_id,
                    "period": time.strftime("%Y-%m-%d"),
                    "offset": 0,
                    "num": num,
                },
            }
        }
        params = {
            "-": "getU", **_BASE,
            "data": json.dumps(data, separators=(",", ":")),
        }
        resp = self.session.get(_TOPLIST_URL, params=params, timeout=self.timeout)
        resp.encoding = "utf-8"
        dd = resp.json().get("detail", {}).get("data", {}).get("data", {})
        songs = []
        for i, s in enumerate(dd.get("song", []) or []):
            songs.append({
                "rank": s.get("rank", i + 1),
                "songid": int(s.get("songId") or 0),
                "songmid": s.get("mid") or s.get("singerMid") or "",
                "name": _unescape(s.get("title", "")),
                "singer": _unescape(s.get("singerName", "")),
                "cover": s.get("cover", ""),
            })
        return {
            "top_id": top_id,
            "title": _unescape(dd.get("title", TOPLISTS.get(top_id, ""))),
            "period": dd.get("period", ""),
            "songs": songs,
        }

    # -------------------- 登录用户歌单 (需 cookie) --------------------
    def get_my_playlists(self, cookie: str) -> Dict[str, Any]:
        """用用户 QQ 音乐 cookie 取「我的歌单」。cookie 失效则返回友好错误。"""
        # 从 cookie 提取 uin 与 p_skey
        uin = ""
        p_skey = ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("uin="):
                uin = part[4:].lstrip("oO")  # uin 可能带 o 前缀
            if part.startswith("p_skey="):
                p_skey = part[7:]
        if not uin or not p_skey:
            raise PermissionError("cookie 缺少 uin / p_skey, 请粘贴完整 QQ 音乐网页 cookie")
        gtk = _gtk(p_skey)
        params = {
            "g_tk": str(gtk),
            "loginUin": uin, "hostUin": uin,
            "format": "json", "inCharset": "utf8", "outCharset": "utf-8",
            "notice": "0", "platform": "yqq.json", "needNewCode": "1",
            "sin": "0", "ein": "29", "order": "1",
        }
        resp = self.session.get(
            _MYDISS_URL, params=params, timeout=self.timeout,
            headers={"Cookie": cookie},
        )
        resp.encoding = "utf-8"
        data = resp.json()
        if data.get("code") != 0:
            raise PermissionError(f"QQ 返回 code={data.get('code')}, cookie 可能已过期")
        out = []
        for d in data.get("data", {}).get("disslist", []) or []:
            out.append({
                "dissid": str(d.get("dissid")),
                "name": _unescape(d.get("dissname", "")),
                "cover": d.get("imgurl") or d.get("logo") or "",
                "song_count": int(d.get("song_num") or 0),
            })
        return {"uin": uin, "count": len(out), "playlists": out}

    # -------------------- safe 包装 (对齐 BaseSpider.safe_fetch 约定) --------------------
    def safe_fetch_category_tree(self):
        try:
            return self.get_category_tree()
        except Exception as e:
            self.logger.error(f"分类树抓取失败: {e}", exc_info=True)
            return None

    def safe_fetch_playlists(self, category_id, page=0, sort=5, size=30):
        try:
            return self.get_playlists(category_id, page=page, sort=sort, size=size)
        except Exception as e:
            self.logger.error(f"歌单列表抓取失败: {e}", exc_info=True)
            return None

    def safe_fetch_playlist_detail(self, dissid, song_num=100):
        try:
            return self.get_playlist_detail(dissid, song_num=song_num)
        except Exception as e:
            self.logger.error(f"歌单详情抓取失败: {e}", exc_info=True)
            return None

    def safe_fetch_toplist(self, top_id, num=50):
        try:
            return self.get_toplist(top_id, num=num)
        except Exception as e:
            self.logger.error(f"榜单抓取失败: {e}", exc_info=True)
            return None
