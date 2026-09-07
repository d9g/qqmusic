"""
QQ 音乐登录爬虫 (ptlogin 扫码 + cookie 粘贴)

流程 (2026-09 实测设计, 参照 y.qq.com 网页登录):
1. GET ssl.ptlogin2.qq.com/ptqrshow   -> 返回二维码 PNG + Set-Cookie: qrsig
2. ptqrtoken = hash33(qrsig)          -> 轮询凭证
3. GET ssl.ptlogin2.qq.com/ptqrlogin  -> ptuiCB('<code>',...,'<check_sig_url>','<code2>','<昵称>','')
   code: 0=成功  65=未扫码  66=已扫码待确认  67=已过期
4. 成功后 GET check_sig_url           -> 会话 jar 里落下 uin / p_skey 等 cookie,
   拼成 cookie 串后与「粘贴 cookie」登录完全等价 (get_my_playlists 只认 uin+p_skey)

会话说明: qrsig 绑定在 requests Session 上, /qr/new 与 /qr/poll 是两次 HTTP 请求,
所以 Session 存在类级字典里, 以 token 关联, 3 分钟自动过期清理。
"""
import base64
import random
import re
import threading
import time
import uuid
from typing import Dict, Any

import requests

from .base import BaseSpider
from ..utils.constants import DEFAULT_HEADERS

_QR_SHOW = "https://ssl.ptlogin2.qq.com/ptqrshow"
_QR_POLL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_APPID = "716027609"  # QQ 音乐 web 端 appid
_DAID = "384"         # QQ 音乐 web 端 daid (决定 p_skey 下发)
_S_URL = "https://y.qq.com/"

_PT_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Referer": (
        "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
        "?appid=716027609&daid=384&style=12"
        "&s_url=https%3A%2F%2Fy.qq.com%2F"
    ),
}

# ptqrlogin 响应形如: ptuiCB('0','0','https://...check_sig...','0','昵称','')
_POLL_RE = re.compile(r"ptuiCB\('(\d+)','([^']*)','([^']*)','([^']*)','([^']*)'")


def _ptqrtoken(qrsig: str) -> int:
    """ptlogin 的 hash33 算法 (注意初值是 0, 与 g_tk 的 5381 不同)"""
    h = 0
    for ch in qrsig:
        h += (h << 5) + ord(ch)
        h &= 2147483647
    return h


def _decode_nick(s: str) -> str:
    """ptqrlogin 里的昵称是 \\uXXXX 转义, 还原成正常文本"""
    if "\\u" not in s:
        return s
    try:
        return s.encode("utf-8").decode("unicode_escape")
    except Exception:
        return s


class QQAuthSpider(BaseSpider):
    name = "auth"

    # token -> {"session", "qrsig", "created", "done"}
    _sessions: Dict[str, dict] = {}
    _lock = threading.Lock()
    QR_TTL = 180  # 二维码有效期约 2~3 分钟

    def _cleanup(self) -> None:
        now = time.time()
        for k in [k for k, v in self._sessions.items() if now - v["created"] > self.QR_TTL]:
            self._sessions.pop(k, None)

    # -------------------- 生成二维码 --------------------
    def create_qr(self) -> Dict[str, Any]:
        t = str(int(time.time() * 1000)) + str(random.randint(10000, 99999))
        params = {
            "appid": _APPID, "daid": _DAID,
            "e": "2", "l": "M", "s": "3", "d": "72", "v": "4", "t": t,
        }
        sess = requests.Session()
        sess.headers.update(_PT_HEADERS)
        resp = sess.get(_QR_SHOW, params=params, timeout=self.timeout)
        resp.raise_for_status()
        qrsig = resp.cookies.get("qrsig")
        if not qrsig:
            raise RuntimeError("二维码生成失败: 响应未带 qrsig")

        token = uuid.uuid4().hex
        with self._lock:
            self._cleanup()
            self._sessions[token] = {
                "session": sess, "qrsig": qrsig,
                "created": time.time(), "done": False,
            }
        return {
            "token": token,
            "qr_png": "data:image/png;base64," + base64.b64encode(resp.content).decode(),
            "expires_in": self.QR_TTL,
        }

    # -------------------- 轮询扫码状态 --------------------
    def poll(self, token: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._sessions.get(token)
        if not entry or time.time() - entry["created"] > self.QR_TTL:
            return {"status": "expired", "message": "二维码已过期, 请点击刷新"}
        if entry["done"]:
            return entry["result"]

        sess: requests.Session = entry["session"]
        params = {
            "u1": _S_URL,
            "ptqrtoken": str(_ptqrtoken(entry["qrsig"])),
            "loginfromqrcode": "1",
            "appid": _APPID,
            "daid": _DAID,
        }
        resp = sess.get(_QR_POLL, params=params, timeout=self.timeout)
        resp.encoding = "utf-8"
        m = _POLL_RE.search(resp.text or "")
        if not m:
            return {"status": "error", "message": "登录接口返回异常, 请刷新重试"}

        code, _, check_url, _, nickname = m.groups()
        nickname = _decode_nick(nickname)

        if code == "65":
            return {"status": "waiting", "message": "等待扫码"}
        if code == "66":
            return {"status": "scanned", "message": "已扫码, 请在手机上确认"}
        if code == "67":
            return {"status": "expired", "message": "二维码已过期, 请点击刷新"}
        if code != "0":
            return {"status": "error", "message": f"登录失败 (code={code})"}

        # code == 0: 访问 check_sig 换取登录 cookie (requests 自动跟随重定向)
        sess.get(check_url, timeout=self.timeout)
        cookie_str = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
        uin = ""
        for part in cookie_str.split(";"):
            p = part.strip()
            if p.startswith("uin="):
                uin = p[4:].lstrip("oO")
        if not uin or "p_skey" not in sess.cookies:
            result = {
                "status": "error",
                "message": "登录态换 cookie 失败 (缺少 uin/p_skey), 可改用粘贴 cookie 登录",
            }
        else:
            result = {
                "status": "success",
                "uin": uin,
                "nickname": nickname or f"QQ用户{uin}",
                "cookie": cookie_str,
            }
        with self._lock:
            entry["done"] = True
            entry["result"] = result
        return result
