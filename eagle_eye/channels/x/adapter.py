"""X 渠道适配：把 xsearch 的 slim() 结果映射成统一 Item，实现 core.Channel。

Query 映射：
- text：X 高级搜索语法原样透传（from:、lang:、min_faves:、OR、-排除词 …）
- since_hours：拼成 since_time:<unix>；null/0 不限
- sort：time → Latest + 按时间排；score → Latest + 按 赞+2×转推 排；relevance → Top，保持 X 返回顺序
- limit：本地截断条数
- options：pages / type / articles / keywords / exclude / min_score / suffix / backend（见 OPTIONS）
"""

from __future__ import annotations

import sys

from ...core.channel import Channel
from ...core.models import (
    BAD_QUERY,
    INTERNAL,
    Author,
    ChannelError,
    Descriptor,
    Item,
    Metrics,
    SearchResult,
    to_rfc3339,
)
from . import core, pipeline
from .api import XError, make_client

OPTIONS = {
    "pages": {"type": "int", "default": 1, "description": "每页约 20 条，最多翻几页（每页 = 1 次请求）"},
    "type": {"type": "str", "default": None, "description": "Latest | Top；不填时 sort=relevance 用 Top，否则 Latest"},
    "articles": {"type": "int", "default": 0, "description": "为最热的 N 条纯链接推文抓 X 长文正文（每篇 1 次请求）"},
    "keywords": {"type": "list[str]", "default": [], "description": "本地过滤：至少命中一个（逗号分隔或 JSON 数组）"},
    "exclude": {"type": "list[str]", "default": [], "description": "本地过滤：命中任一即丢弃"},
    "min_score": {"type": "int", "default": 0, "description": "最低热度（赞 + 2×转推）"},
    "suffix": {"type": "str", "default": "-is:retweet", "description": "追加到查询末尾，空串关闭（默认去掉转推）"},
    "backend": {"type": "str", "default": "auto", "description": "twscrape | twitterapi | auto"},
}

DESCRIPTOR = Descriptor(
    name="x",
    title="X (Twitter)",
    description="搜索 X 推文（twscrape 用自己的账号 cookie，或 twitterapi.io 付费 API），可抓 X 长文正文",
    capabilities=["search"],
    query_syntax=(
        'X 高级搜索语法原样透传："精确短语"、A OR B、-排除词、from:用户、to:用户、lang:zh|en|ja、'
        "min_faves:N、min_retweets:N、filter:links、-filter:replies、url:github.com、since:/until:（此时别传 since_hours）"
    ),
    cost_hint="快：每页约 20 条 ≈ 1 次请求、数秒；每个账号约 50 次搜索 / 15 分钟，超限返回 rate_limited",
    options=OPTIONS,
)


def _stderr(msg):
    print(msg, file=sys.stderr, flush=True)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, (list, tuple)):
        return [str(s) for s in v]
    raise ChannelError(BAD_QUERY, f"需要字符串列表: {v!r}", channel="x")


def _as_int(name, v, default):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ChannelError(BAD_QUERY, f"options.{name} 必须是整数: {v!r}", channel="x") from None


def to_item(d, channel="x"):
    """slim() 的 dict → Item。"""
    user = d.get("user") or ""
    created = pipeline.parse_time(d.get("createdAt"))
    art = d.get("article")
    return Item(
        channel=channel,
        id=str(d.get("id") or ""),
        url=d.get("url") or (f"https://x.com/{user}/status/{d.get('id')}" if user else f"https://x.com/i/status/{d.get('id')}"),
        title=(art or {}).get("title") or "",
        text=d.get("text") or "",
        author=Author(id=user, name=d.get("name") or user, url=f"https://x.com/{user}" if user else ""),
        created_at=to_rfc3339(created),
        metrics=Metrics(
            likes=d.get("likes"),
            reposts=d.get("retweets"),
            comments=d.get("replies"),
            views=d.get("views"),
            collects=None,
        ),
        score=d.get("score"),
        lang=d.get("lang") or None,
        extra={
            "followers": d.get("followers"),
            "isReply": bool(d.get("isReply")),
            "article": art,
            "hits": d.get("hits") or [],
        },
    )


def to_channel_error(e):
    if isinstance(e, XError):
        return ChannelError(e.code, str(e), e.retry_after_seconds, channel="x")
    return ChannelError(INTERNAL, str(e), channel="x")


class XChannel(Channel):
    """进程内 X 渠道。client_factory 可注入假客户端（测试用）。"""

    def __init__(self, name="x", config=None, root=None, client_factory=None, log=_stderr):
        super().__init__(name, config, root)
        self.client_factory = client_factory or make_client
        self.log = log

    def describe(self):
        d = Descriptor.from_dict(DESCRIPTOR.to_dict())
        d.name = self.name
        return d

    def status(self):
        """可用 = 能选出一个后端：twscrape（已装 + 账号库存在）或 TWITTERAPI_IO_KEY 已设置。不发网络请求。"""
        import os

        from . import tws
        from .settings import env

        backend = (env("BACKEND") or "auto").lower()
        has_tws, has_key = tws.available(), bool(os.environ.get("TWITTERAPI_IO_KEY"))
        if backend == "twscrape" and not has_tws:
            return False, f"auth_required: twscrape 未安装或账号库不存在（{tws.db_path()}），需要人工运行 bin/eagle x login"
        if backend == "twitterapi" and not has_key:
            return False, "auth_required: 缺少环境变量 TWITTERAPI_IO_KEY"
        if backend == "auto" and not (has_tws or has_key):
            return False, f"auth_required: 没有 twscrape 账号库（{tws.db_path()}）也没有 TWITTERAPI_IO_KEY，需要人工运行 bin/eagle x login"
        return True, ""

    def search(self, query):
        query.validate()
        o = query.options or {}
        unknown = sorted(set(o) - set(OPTIONS))
        sort = query.sort
        qtype = o.get("type") or ("Top" if sort == "relevance" else "Latest")
        if qtype not in ("Latest", "Top"):
            raise ChannelError(BAD_QUERY, f"options.type 只能是 Latest / Top: {qtype!r}", channel=self.name)
        pages = _as_int("pages", o.get("pages"), 1)
        articles = _as_int("articles", o.get("articles"), 0)
        min_score = _as_int("min_score", o.get("min_score"), 0)
        if pages < 1 or pages > 50:
            raise ChannelError(BAD_QUERY, f"options.pages 应在 1–50: {pages}", channel=self.name)
        suffix = o.get("suffix", "-is:retweet")
        suffix = "" if suffix is None else str(suffix)
        hours = query.since_hours or 0

        try:
            client = self.client_factory(o.get("backend")) if o.get("backend") else self.client_factory()
        except XError as e:
            raise to_channel_error(e) from e
        errors = []
        try:
            raw, tweets = core.collect(
                [query.text], hours, pages, qtype, suffix, articles, client=client, log=self.log, errors=errors
            )
        except XError as e:
            raise to_channel_error(e) from e
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        if errors and not tweets:
            raise to_channel_error(errors[0])

        local_sort = {"time": "time", "score": "score"}.get(sort, "none")
        picked = pipeline.select(
            tweets, _as_list(o.get("keywords")), _as_list(o.get("exclude")), min_score, query.limit or None, local_sort
        )
        items = [to_item(pipeline.slim(t), self.name) for t in picked]
        meta = {
            "query": pipeline.build_query(query.text, core.since_ts(hours), suffix),
            "ignored": [f"options.{k}" for k in unknown],
            "notes": f"type={qtype}，原始 {sum(len(v) for v in raw.values())} 条 → 去重 {len(tweets)} 条",
        }
        if errors:
            meta["warnings"] = [str(e) for e in errors]
        return SearchResult(items, meta)

    def native(self, argv):
        from . import cli

        return cli.main(list(argv))

