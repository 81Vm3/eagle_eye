"""eagle_eye 核心中间层：统一模型、Channel 接口、注册表、并发路由、输出渲染。"""

from .channel import BrokenChannel, Channel
from .models import (
    AUTH_REQUIRED,
    BAD_QUERY,
    BUSY,
    COOLDOWN,
    ERROR_CODES,
    INTERNAL,
    PROTOCOL,
    RATE_LIMITED,
    UNAVAILABLE,
    Author,
    ChannelError,
    Descriptor,
    Item,
    Metrics,
    Query,
    SearchResult,
)
from .registry import Registry
from .router import AggregateResult, fan_out

__all__ = [
    "AUTH_REQUIRED", "BAD_QUERY", "BUSY", "COOLDOWN", "ERROR_CODES", "INTERNAL", "PROTOCOL",
    "RATE_LIMITED", "UNAVAILABLE", "AggregateResult", "Author", "BrokenChannel", "Channel",
    "ChannelError", "Descriptor", "Item", "Metrics", "Query", "Registry", "SearchResult", "fan_out",
]
