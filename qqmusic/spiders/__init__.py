"""爬虫模块"""
from .base import BaseSpider
from .comment import CommentSpider
from .search import SearchSpider
from .browse import QQBrowseSpider
from .auth import QQAuthSpider

__all__ = ["BaseSpider", "CommentSpider", "SearchSpider", "QQBrowseSpider", "QQAuthSpider"]
