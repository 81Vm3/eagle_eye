"""X (Twitter) 渠道：原 x_resource_search/xsearch 迁移而来。

- adapter.XChannel：实现 core.Channel，供 eagle search 使用
- core.search()：原 xsearch.search() 的 Python 接口（返回 slim dict 列表）
- cli：原生子命令 eagle x search|run|topics|article|login
"""

from .core import search

__all__ = ["search"]
