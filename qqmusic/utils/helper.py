"""
通用工具
- get_logger() 统一 logger
- config() 读环境变量
- clean_content() 清洗 QQ 音乐评论正文
- analyze_quality() 判定水评并返回原因
- is_trivial() 简版判定 (仅看是否水评, 兼容旧调用)
"""
import os
import re
import sys
import logging
from pathlib import Path
from typing import NamedTuple, Optional

# QQ 音乐表情占位码, 形如 [em]e400867[/em]
_EMOJI_PATTERN = re.compile(r"\[em\][^\[\]]*\[/em\]")
# 媒体占位符。实测除 [em] 外还有 [图片] / [表情] / [音频] / [视频],
# 不去的话 "[图片]" 会被当成 4 个有效字符, 干扰长度判定
_MEDIA_PATTERN = re.compile(r"\[(?:图片|表情|音频|视频|动图)\]")
# 话题标签, 形如 #哪一瞬间你听懂了Eason#
_TOPIC_PATTERN = re.compile(r"#[^#]{1,50}#")
# 空白压缩
_SPACE_PATTERN = re.compile(r"\s+")
# 链接 (广告/引流)
_URL_PATTERN = re.compile(r"(https?://|www\.)\S+", re.I)
# 系统占位文本
_SYSTEM_TEXTS = {"评论审核中", "该评论已删除", "评论不存在"}


def get_logger(name: str = "qqmusic") -> logging.Logger:
    """统一 logger 入口, 确保所有模块日志格式一致"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    from .constants import LOGS_DIR
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(f"{LOGS_DIR}/qqmusic.log", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger.propagate = False
    return logger


def config(key: str, default: Optional[str] = None) -> Optional[str]:
    """从环境变量读配置"""
    val = os.getenv(key)
    if val is not None:
        return val
    return default


def clean_content(raw: str, drop_topic: bool = False) -> str:
    """
    清洗评论正文

    QQ 音乐返回的正文里混着三类占位符, 不清洗既污染 AI 情感分析,
    也会让 "[图片]" 这类占位符被算成有效字数:
      1. 表情码     [em]e400867[/em]
      2. 媒体占位符 [图片] / [表情] / [音频] / [视频]
      3. 话题标签   #哪一瞬间你听懂了Eason#  (可选, 默认保留)

    :param raw: 原始正文
    :param drop_topic: 是否一并去掉话题标签 (#xxx#)
    :return: 清洗后的正文
    """
    if not raw:
        return ""
    text = _EMOJI_PATTERN.sub("", raw)
    text = _MEDIA_PATTERN.sub("", text)
    if drop_topic:
        text = _TOPIC_PATTERN.sub("", text)
    text = _SPACE_PATTERN.sub(" ", text)
    return text.strip()


class QualityResult(NamedTuple):
    """水评判定结果"""

    is_trivial: bool
    reason: str  # ok / empty / short / system / url


# 判定阈值集中在这里, 改完跑 `cli recheck` 即可按新规则重算全库
try:
    from .constants import TRIVIAL_MIN_LEN, TRIVIAL_MIN_LIKED_KEEP
except ImportError:  # pragma: no cover
    TRIVIAL_MIN_LEN, TRIVIAL_MIN_LIKED_KEEP = 6, 20


def analyze_quality(
    content: str,
    liked_count: int = 0,
    min_len: int = TRIVIAL_MIN_LEN,
    liked_keep: int = TRIVIAL_MIN_LIKED_KEEP,
) -> QualityResult:
    """
    判定是否为水评, 并给出原因

    规则 (2026-09 基于 325 条真实样本校准):
      1. 清洗后为空                -> empty
      2. 系统占位文本              -> system
      3. 含链接 (广告/引流)        -> url
      4. 短于 min_len 且非高赞     -> short

    两条"看起来该加但实测会误伤"的规则, 已刻意不做:
      - 字符重复率: "小学时候听《稻香》, 初中时候听《稻香》..." 重复率高
        但内容真实, 会被误杀
      - 纯符号判定: 325 条样本中命中 0 条, 收益为零
    """
    text = (content or "").strip()

    if not text:
        return QualityResult(True, "empty")
    if text in _SYSTEM_TEXTS:
        return QualityResult(True, "system")
    if _URL_PATTERN.search(text):
        return QualityResult(True, "url")
    if len(text) < min_len and (liked_count or 0) < liked_keep:
        return QualityResult(True, "short")

    return QualityResult(False, "ok")


def is_trivial(
    content: str,
    min_len: int = TRIVIAL_MIN_LEN,
    liked_count: int = 0,
) -> bool:
    """
    简版判定: 只回答"是不是水评", 不关心原因

    保留此函数是为了兼容旧调用; 新代码建议用 analyze_quality() 拿原因,
    便于入库时记录、后续统计各类水评占比。
    """
    return analyze_quality(content, liked_count=liked_count, min_len=min_len).is_trivial
