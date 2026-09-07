"""
QQ 音乐项目常量

集中管理接口地址 / 默认 headers / 存储路径
"""
import os

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://y.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "qqmusic", "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "qqmusic", "logs")

DEFAULT_DB_URL = f"sqlite:///{DATA_DIR}/qqmusic.db"

# ==================== 接口地址 ====================
# 评论接口 (2026-09 实测可用, 无需登录)
COMMENT_URL = "https://c.y.qq.com/base/fcgi-bin/fcg_global_comment_h5.fcg"
# 搜索接口 (返回 songid / songmid)
# 注意: client_search_cp 与 u.y.qq.com/cgi-bin/musicu.fcg 已失效, 勿用
SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp"

# ==================== 抓取参数 (实测结论, 勿随意改) ====================
# pagesize 硬上限: QQ 服务端最大只支持 25,
# 传更大的值不会报错, 而是静默退化成只返回 10 条 (比 25 还少)
MAX_PAGE_SIZE = 25
DEFAULT_PAGE_SIZE = 25

# 单歌可翻页深度上限 (实测约 2 万条, 超过后返回空列表且 code=0)
# 用于在入库时标记 is_complete, 区分"抓全了"和"被截断"
PRACTICAL_DEPTH_CAP = 20000

# 请求间隔下限 (秒)。实测无频率限制, 但保持礼貌
MIN_REQUEST_INTERVAL = 0.35

# 增量抓取: 连续多少页"全是已抓过的评论"就停止。
# 评论按时间倒序返回, 新评论只会插在前面, 所以撞到旧评论即可收工。
# 取 2 而不是 1: 单页可能因为评论被删除而恰好无新增, 容忍一次。
INCREMENTAL_STOP_PAGES = 2

# ==================== 水评判定阈值 ====================
# 实测: 375 条样本中短于 6 字的占约 14%, 且其中点赞 >=20 的为 0 条,
# 说明短评基本无分析价值。
#
# 两个值都可用环境变量覆盖, 改完跑 `cli recheck` 即可按新规则重算全库,
# 不用重新抓取 —— 前提是入库时没把水评丢掉 (默认行为就是不丢)。
def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or default)
    except ValueError:
        return default


# 短于该长度视为水评
TRIVIAL_MIN_LEN = _int_env("QQMUSIC_TRIVIAL_MIN_LEN", 6)
# 高赞豁免: 短评若点赞数达到该值, 视为"神评"保留。
# 近 700 条样本中触发 0 次, 属保险丝性质, 防止误杀高赞短评。
TRIVIAL_MIN_LIKED_KEEP = _int_env("QQMUSIC_TRIVIAL_MIN_LIKED_KEEP", 20)
