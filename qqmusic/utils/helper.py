"""
通用工具
- get_logger() 统一 logger
- config() 读环境变量
- clean_content() 清洗 QQ 音乐评论正文
- is_trivial() 判断水评
"""
import os
import re
import sys
import logging
from pathlib import Path
from typing import Optional

# QQ 音乐表情占位码, 形如 [em]e400867[/em]
_EMOJI_PATTERN = re.compile(r"\[em\][^\[\]]*\[/em\]")
# 话题标签, 形如 #哪一瞬间你听懂了Eason#
_TOPIC_PATTERN = re.compile(r"#[^#]{1,50}#")
# 空白压缩
_SPACE_PATTERN = re.compile(r"\s+")

# 短于该长度视为水评 (实测 QQ 评论长度中位数仅 10 字)
TRIVIAL_MIN_LEN = 6


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

    QQ 音乐返回的正文里混着表情占位码 (形如 [em]e400867[/em]),
    不清洗会污染 AI 情感分析。

    :param raw: 原始正文
    :param drop_topic: 是否一并去掉话题标签 (#xxx#)
    :return: 清洗后的正文
    """
    if not raw:
        return ""
    text = _EMOJI_PATTERN.sub("", raw)
    if drop_topic:
        text = _TOPIC_PATTERN.sub("", text)
    text = _SPACE_PATTERN.sub(" ", text)
    return text.strip()


def is_trivial(content: str, min_len: int = TRIVIAL_MIN_LEN) -> bool:
    """
    判断是否为水评

    实测 QQ 音乐评论约 1/3 短于 6 字 (如 "脸" / "敬礼" / "好听"),
    且深翻页的水评比例明显更高, 分析前建议先过滤。
    """
    if not content:
        return True
    return len(content.strip()) < min_len
