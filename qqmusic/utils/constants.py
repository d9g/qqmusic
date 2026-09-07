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
