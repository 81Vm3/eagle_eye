"""统一数据模型，与 docs/channel-protocol.md 一一对应。

所有模型都提供 to_dict() / from_dict()：to_dict 产出协议 JSON 形状，from_dict 忽略未知字段（向前兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

PROTOCOL = 1

# ---- 错误码 ----
AUTH_REQUIRED = "auth_required"
RATE_LIMITED = "rate_limited"
COOLDOWN = "cooldown"
BUSY = "busy"
BAD_QUERY = "bad_query"
UNAVAILABLE = "unavailable"
INTERNAL = "internal"
ERROR_CODES = (AUTH_REQUIRED, RATE_LIMITED, COOLDOWN, BUSY, BAD_QUERY, UNAVAILABLE, INTERNAL)

SORTS = ("relevance", "time", "score")
METRIC_KEYS = ("likes", "reposts", "comments", "views", "collects")


class ChannelError(Exception):
    """渠道失败。code 取 ERROR_CODES 之一；未知 code 一律归为 internal。"""

    def __init__(self, code, message="", retry_after_seconds=None, channel=None):
        if code not in ERROR_CODES:
            message = f"[{code}] {message}" if code else message
            code = INTERNAL
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        self.channel = channel

    def __str__(self):
        prefix = f"{self.channel}: " if self.channel else ""
        return f"{prefix}{self.code}: {self.message}"

    def to_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.retry_after_seconds is not None:
            d["retry_after_seconds"] = self.retry_after_seconds
        if self.channel:
            d["channel"] = self.channel
        return d

    @classmethod
    def from_dict(cls, d, channel=None):
        if not isinstance(d, dict):
            return cls(INTERNAL, f"非法错误对象: {d!r}"[:300], channel=channel)
        retry = d.get("retry_after_seconds")
        return cls(
            str(d.get("code") or INTERNAL),
            str(d.get("message") or ""),
            _opt_int(retry),
            channel=channel or d.get("channel"),
        )


def _opt_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return None
    return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _str(v):
    return "" if v is None else str(v)


def to_rfc3339(dt):
    """datetime → RFC3339 UTC（2026-11-19T08:00:00Z）；None 原样返回。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(s):
    """RFC3339 字符串 → aware datetime；解析失败返回 None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Query:
    text: str
    limit: int = 20
    since_hours: float | None = None
    sort: str = "relevance"
    options: dict = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise ChannelError(BAD_QUERY, "text 不能为空")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 0:
            raise ChannelError(BAD_QUERY, f"limit 必须是非负整数: {self.limit!r}")
        if self.since_hours is not None and (not isinstance(self.since_hours, (int, float)) or self.since_hours < 0):
            raise ChannelError(BAD_QUERY, f"since_hours 必须是非负数或 null: {self.since_hours!r}")
        if self.sort not in SORTS:
            raise ChannelError(BAD_QUERY, f"sort 只能是 {'/'.join(SORTS)}: {self.sort!r}")
        if not isinstance(self.options, dict):
            raise ChannelError(BAD_QUERY, "options 必须是对象")
        return self

    def to_dict(self):
        return {
            "protocol": PROTOCOL,
            "text": self.text,
            "limit": self.limit,
            "since_hours": self.since_hours,
            "sort": self.sort,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ChannelError(BAD_QUERY, "Query 必须是 JSON 对象")
        q = cls(
            text=d.get("text"),
            limit=d.get("limit", 20) if d.get("limit") is not None else 20,
            since_hours=d.get("since_hours") or None,
            sort=d.get("sort") or "relevance",
            options={} if d.get("options") is None else d.get("options"),
        )
        return q.validate()

    def with_options(self, options):
        return Query(self.text, self.limit, self.since_hours, self.sort, dict(options))


@dataclass
class Author:
    id: str = ""
    name: str = ""
    url: str = ""

    def to_dict(self):
        return {"id": self.id, "name": self.name, "url": self.url}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(_str(d.get("id")), _str(d.get("name")), _str(d.get("url")))


@dataclass
class Metrics:
    likes: int | None = None
    reposts: int | None = None
    comments: int | None = None
    views: int | None = None
    collects: int | None = None

    def to_dict(self):
        return {k: getattr(self, k) for k in METRIC_KEYS}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(**{k: _opt_int(d.get(k)) for k in METRIC_KEYS})


@dataclass
class Item:
    channel: str
    id: str
    url: str
    title: str = ""
    text: str = ""
    author: Author = field(default_factory=Author)
    created_at: str | None = None
    metrics: Metrics = field(default_factory=Metrics)
    score: float | None = None
    lang: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "channel": self.channel,
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "author": self.author.to_dict(),
            "created_at": self.created_at,
            "metrics": self.metrics.to_dict(),
            "score": self.score,
            "lang": self.lang,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ValueError("Item 必须是 JSON 对象")
        missing = [k for k in ("channel", "id", "url") if not d.get(k)]
        if missing:
            raise ValueError(f"Item 缺少必填字段: {', '.join(missing)}")
        created = d.get("created_at")
        return cls(
            channel=str(d["channel"]),
            id=str(d["id"]),
            url=str(d["url"]),
            title=_str(d.get("title")),
            text=_str(d.get("text")),
            author=Author.from_dict(d.get("author")),
            created_at=created if isinstance(created, str) and created else None,
            metrics=Metrics.from_dict(d.get("metrics")),
            score=_num(d.get("score")),
            lang=d.get("lang") if isinstance(d.get("lang"), str) and d.get("lang") else None,
            extra=d.get("extra") if isinstance(d.get("extra"), dict) else {},
        )

    def created_dt(self):
        return parse_rfc3339(self.created_at)


@dataclass
class Descriptor:
    name: str
    title: str = ""
    description: str = ""
    capabilities: list = field(default_factory=lambda: ["search"])
    query_syntax: str = ""
    cost_hint: str = ""
    options: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "protocol": PROTOCOL,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "query_syntax": self.query_syntax,
            "cost_hint": self.cost_hint,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict) or not d.get("name"):
            raise ValueError("Descriptor 必须是含 name 的 JSON 对象")
        caps = d.get("capabilities")
        opts = d.get("options")
        return cls(
            name=str(d["name"]),
            title=_str(d.get("title")),
            description=_str(d.get("description")),
            capabilities=[str(c) for c in caps] if isinstance(caps, list) else ["search"],
            query_syntax=_str(d.get("query_syntax")),
            cost_hint=_str(d.get("cost_hint")),
            options=opts if isinstance(opts, dict) else {},
        )


@dataclass
class SearchResult:
    """单个渠道一次搜索的结果（协议里的 SearchResponse）。"""

    items: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return {"protocol": PROTOCOL, "items": [i.to_dict() for i in self.items], "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, d, channel=None):
        """解析 SearchResponse。含 error 时抛 ChannelError；非法 Item 跳过并记到 meta.invalid_items。"""
        if not isinstance(d, dict):
            raise ChannelError(INTERNAL, "SearchResponse 必须是 JSON 对象", channel=channel)
        if d.get("error"):
            raise ChannelError.from_dict(d["error"], channel=channel)
        raw = d.get("items")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ChannelError(INTERNAL, "SearchResponse.items 必须是数组", channel=channel)
        items, bad = [], 0
        for x in raw:
            try:
                it = Item.from_dict(x)
            except ValueError:
                bad += 1
                continue
            if channel:
                it.channel = channel
            items.append(it)
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        if bad:
            meta = {**meta, "invalid_items": bad}
        return cls(items, meta)
