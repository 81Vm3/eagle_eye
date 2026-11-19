"""Channel 抽象：所有渠道（进程内 Python 类或外部程序适配器）对上层暴露同一个接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import UNAVAILABLE, ChannelError, Descriptor, Query, SearchResult


class Channel(ABC):
    """渠道基类。

    子类构造签名统一为 ``__init__(self, name, config, root)``：
    - name：eagle.toml 里的渠道名（[channels.<name>]）
    - config：该表的全部键（dict）
    - root：仓库根目录（Path），相对路径都以它为基准
    """

    def __init__(self, name, config=None, root=None):
        self.name = name
        self.config = dict(config or {})
        self.root = root

    @abstractmethod
    def describe(self) -> Descriptor:
        """返回渠道元数据。依赖缺失时可抛 ChannelError(unavailable)。"""

    @abstractmethod
    def search(self, query: Query) -> SearchResult:
        """执行一次搜索。失败抛 ChannelError；其他异常由 router 归为 internal。"""

    def status(self):
        """返回 (available: bool, reason: str)。默认：能 describe 就算可用。"""
        try:
            self.describe()
        except ChannelError as e:
            return False, f"{e.code}: {e.message}"
        except Exception as e:  # noqa: BLE001
            return False, f"internal: {type(e).__name__}: {e}"
        return True, ""

    def native(self, argv) -> int:
        """渠道原生子命令（eagle <name> ...）。默认不支持。"""
        raise ChannelError(UNAVAILABLE, f"渠道 {self.name} 没有原生子命令", channel=self.name)


class BrokenChannel(Channel):
    """构造失败的渠道（导入错误、配置错误等）：describe/search 都报 unavailable，不影响其他渠道。"""

    def __init__(self, name, config=None, root=None, reason=""):
        super().__init__(name, config, root)
        self.reason = reason

    def describe(self):
        raise ChannelError(UNAVAILABLE, self.reason, channel=self.name)

    def search(self, query):
        raise ChannelError(UNAVAILABLE, self.reason, channel=self.name)
