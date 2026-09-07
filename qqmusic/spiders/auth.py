"""
QQ 音乐登录爬虫 (ptlogin 扫码 + cookie 粘贴)

实测结论 (2026-09-07, 服务器上逐步验证):
- 直连流 (daid=384, s_url=y.qq.com) 已失效: ptqrlogin 返回 23013「当前应用版本过低」
- 可用流是【OAuth 三方授权】: appid=716027609 + daid=383 + pt_3rd_aid=100497308,
  s_url 走 graph.qq.com/oauth2.0/login_jump (即网页版 y.qq.com 登录弹窗的流程)

流程:
1. GET xui.ptlogin2.qq.com/cgi-bin/xlogin   -> Set-Cookie: pt_login_sig 等
2. GET ssl.ptlogin2.qq.com/ptqrshow         -> 二维码 PNG + Set-Cookie: qrsig
3. 轮询 ssl.ptlogin2.qq.com/ptqrlogin       -> ptuiCB('<code>',...,'<check_sig_url>','<code2>','<消息>','')
   响应是 GBK 编码; code 0=成功, 其余按消息判断 (未失效=等待扫码 / 认证中=已扫码 / 失效=过期)
4. 成功后 GET check_sig_url (跟随重定向) 再访问 y.qq.com,
   会话 jar 里落下 uin / p_skey 等 cookie, 拼成 cookie 串与「粘贴 cookie」登录等价

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

_XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
_QR_SHOW = "https://ssl.ptlogin2.qq.com/ptqrshow"
_QR_POLL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_APPID = "716027609"        # QQ 音乐 web 端 appid
_DAID = "383"               # OAuth 授权流 daid (384 直连流已失效, 见模块注释)
_PT_3RD_AID = "100497308"   # QQ 音乐在 graph.qq.com 的三方应用 id
_OAUTH_JUMP = "https://graph.qq.com/oauth2.0/login_jump"
_S_URL = "https://y.qq.com/"

_XLOGIN_PARAMS = {
    "appid": _APPID, "daid": _DAID, "style": "33",
    "login_text": "授权并登录", "hide_title_bar": "1", "hide_border": "1",
    "target": "self", "s_url": _OAUTH_JUMP,
    "pt_3rd_aid": _PT_3RD_AID,
    "pt_feedback_link": "https://support.qq.com/products/77942?customInfo=.appid100497308",
}

_PT_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
}

# ptqrlogin 响应形如: ptuiCB('66','0','','0','二维码未失效。','')
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

    def __init__(self, timeout: int = 15):
        super().__init__()
        self.timeout = timeout

    # token -> {"session", "qrsig", "xlogin_url", "created", "done", "result"}
    _sessions: Dict[str, dict] = {}
    _lock = threading.Lock()
    QR_TTL = 180  # 二维码有效期约 2~3 分钟

    def _cleanup(self) -> None:
        now = time.time()
        for k in [k for k, v in self._sessions.items() if now - v["created"] > self.QR_TTL]:
            self._sessions.pop(k, None)

    # -------------------- 生成二维码 --------------------
    def create_qr(self) -> Dict[str, Any]:
        sess = requests.Session()
        sess.headers.update(_PT_HEADERS)
        # 1. xlogin 拿 pt_login_sig (缺失会导致轮询报参数错误)
        resp0 = sess.get(_XLOGIN_URL, params=_XLOGIN_PARAMS, timeout=self.timeout)
        resp0.raise_for_status()
        xlogin_url = resp0.url
        # 2. ptqrshow 拿二维码 + qrsig
        params = {
            "appid": _APPID, "e": "2", "l": "M", "s": "3", "d": "72", "v": "4",
            "t": str(random.random()), "daid": _DAID, "pt_3rd_aid": _PT_3RD_AID,
        }
        resp = sess.get(_QR_SHOW, params=params, timeout=self.timeout)
        resp.raise_for_status()
        qrsig = resp.cookies.get("qrsig")
        if not qrsig:
            raise RuntimeError("二维码生成失败: 响应未带 qrsig")

        token = uuid.uuid4().hex
        with self._lock:
            self._cleanup()
            self._sessions[token] = {
                "session": sess, "qrsig": qrsig, "xlogin_url": xlogin_url,
                "created": time.time(), "done": False, "result": None,
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
            "u1": _OAUTH_JUMP,
            "ptqrtoken": str(_ptqrtoken(entry["qrsig"])),
            "ptredirect": "0", "h": "1", "t": "1", "g": "1", "from_ui": "1",
            "ptui_language": "2052",
            # 注意传原始值, requests 会编码成 %3A%3A; 传已编码值会双重编码导致 code=7
            "fpinfo": "::",
            "loginfromqrcode": "1",
            "appid": _APPID, "daid": _DAID, "pt_3rd_aid": _PT_3RD_AID,
        }
        resp = sess.get(
            _QR_POLL, params=params, timeout=self.timeout,
            headers={"Referer": entry["xlogin_url"]},
        )
        # 响应编码实测为 UTF-8 (旧文档说 GBK, 两种都兼容: 哪份解出有效中文用哪份)
        raw = resp.content
        m = None
        for text in (raw.decode("utf-8", "ignore"), raw.decode("gbk", "ignore")):
            cand = _POLL_RE.search(text)
            if cand and any(k in (cand.group(5) or "") for k in ("失效", "认证", "扫描", "扫")):
                m = cand
                break
        if m is None:
            # 两份都没匹配到中文关键字, 退回第一份 (至少能解析出 code/url 结构)
            m = _POLL_RE.search(raw.decode("utf-8", "ignore"))
        if not m:
            return {"status": "error", "message": "登录接口返回异常, 请刷新重试"}

        code, _, check_url, _, msg = m.groups()
        msg = msg.strip().rstrip("。")

        if code == "0":
            # 成功响应的第 5 个字段是昵称 (非成功响应里是消息文本)
            return self._finish_login(entry, check_url, msg)
        # 非成功码: 按消息判断状态 (不同 daid 流的 code 编号有差异, 消息更可靠)
        if "认证中" in msg or "已扫描" in msg:
            return {"status": "scanned", "message": "已扫码, 请在手机上确认授权"}
        if "未失效" in msg:
            return {"status": "waiting", "message": "等待扫码"}
        if "失效" in msg:
            return {"status": "expired", "message": "二维码已过期, 请点击刷新"}
        return {"status": "error", "message": f"登录失败 (code={code}, {msg})"}

    def _finish_login(self, entry: dict, check_url: str, nick_raw: str) -> Dict[str, Any]:
        """code=0: 跟随 check_sig 重定向链换取登录 cookie"""
        sess: requests.Session = entry["session"]
        try:
            sess.get(check_url, timeout=self.timeout)
            # OAuth 落地后访问 y.qq.com, 触发其设置自家 cookie (uin/p_skey/qm_keyst 等)
            sess.get(_S_URL, timeout=self.timeout)
        except Exception as e:
            self.logger.warning(f"check_sig 跟随异常(继续尝试取 cookie): {e}")

        cookie_str = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
        cookie_keys = sorted(sess.cookies.keys())
        self.logger.info(f"扫码登录 cookie 落地: keys={cookie_keys}")

        uin = ""
        for part in cookie_str.split(";"):
            p = part.strip()
            if p.startswith("uin="):
                uin = p[4:].lstrip("oO")
        if not uin or "p_skey" not in sess.cookies:
            result = {
                "status": "error",
                "message": (
                    f"登录成功但换 cookie 不完整 (拿到: {','.join(cookie_keys) or '无'}), "
                    "可改用「粘贴 cookie 登录」"
                ),
            }
        else:
            nick = _decode_nick(nick_raw) if "\\u" in nick_raw else nick_raw
            result = {
                "status": "success",
                "uin": uin,
                "nickname": (nick or "").strip() or f"QQ用户{uin}",
                "cookie": cookie_str,
            }
        with self._lock:
            entry["done"] = True
            entry["result"] = result
        return result
