"""
爬虫基类
- 提供 logger
- 统一异常处理
- 子类只需实现 fetch() 方法
"""
from typing import Optional, Dict, Any
from ..utils import get_logger


class BaseSpider:
    name: str = "base"

    def __init__(self, name: Optional[str] = None):
        self.name = name or self.__class__.__name__
        self.logger = get_logger(f"qqmusic.{self.name}")
        self.logger.debug(f"初始化 {self.name}")

    def fetch(self, *args, **kwargs) -> Dict[str, Any]:
        """子类重写: 执行实际抓取"""
        raise NotImplementedError

    def safe_fetch(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        try:
            result = self.fetch(*args, **kwargs)
            self.logger.info(f"{self.name} 抓取成功")
            return result
        except Exception as e:
            self.logger.error(f"{self.name} 抓取失败: {e}", exc_info=True)
            return None
