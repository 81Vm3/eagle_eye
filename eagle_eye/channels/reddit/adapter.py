"""Reddit 渠道：Tavily 全站搜索，subreddit/author 查询优先 Arctic Shift，可本地缓存。

不读取、不打印、不记录 Tavily API key；所有 provider 错误都会在转成 ChannelError 前脱敏。
"""

from __future__ import annotations

import os
import re
import time
import tomllib
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from ... import paths
from ...core.channel import Channel
from ...core.models import (
    AUTH_REQUIRED,
    BAD_QUERY,
    INTERNAL,
    UNAVAILABLE,
    Author,
    ChannelError,
    Descriptor,
    Item,
    Metrics,
    Query,
    SearchResult,
    parse_rfc3339,
    to_rfc3339,
)
from .cache import ResultCache, make_key
from .providers import (
    DEFAULT_ARCTIC_BASE_URL,
    DEFAULT_TAVILY_BASE_URL,
    DEFAULT_TIMEOUT,
    ArcticProvider,
    RedditProviderError,
    TavilyProvider,
    redact_secret,
)

OPTIONS = {
    "subreddit": {"type": "str", "default": None, "description": "限定 subreddit，例如 LocalLLaMA；优先走 Arctic Shift"},
    "author": {"type": "str", "default": None, "description": "限定作者，例如 u/spez；优先走 Arctic Shift"},
    "enrich": {"type": "bool", "default": True, "description": "Tavily 结果用 Arctic Shift 批量补 author/score/comments/created_at/selftext"},
    "cache": {"type": "bool", "default": True, "description": "是否读本地缓存；false 时跳过本次读取但结果仍写回"},
}

DESCRIPTOR = Descriptor(
    name="reddit",
    title="Reddit",
    description="通过 Tavily 搜索 Reddit 公开讨论；subreddit/author 查询优先走 Arctic Shift，非官方接口，字段可能不完整",
    capabilities=["search"],
    query_syntax="普通关键词；subreddit/author 用 --opt 限定（如 --opt subreddit=LocalLLaMA）；不支持 Reddit 高级搜索语法",
    cost_hint="Tavily 免费档约 1,000 信用/月，basic 搜索 1 信用/次；结果缓存默认 24 小时；Arctic Shift 免费但无 SLA；单次约 1–3 秒",
    options=OPTIONS,
)

_POST_PATH = re.compile(r"^/(?:r/([^/]+)/)?(?:user/[^/]+/)?comments/([^/]+)(?:/|$)", re.IGNORECASE)
_POST_ID = re.compile(r"^[a-z0-9]{1,13}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _as_bool(name, value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    raise ChannelError(BAD_QUERY, f"options.{name} 必须是布尔值: {value!r}")


def _as_name(name, value, prefix):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChannelError(BAD_QUERY, f"options.{name} 必须是字符串: {value!r}")
    value = value.strip()
    lowered = value.lower()
    for candidate in (prefix, f"/{prefix}"):
        if lowered.startswith(candidate):
            value = value[len(candidate):]
            break
    if not value or not _NAME.match(value):
        raise ChannelError(BAD_QUERY, f"options.{name} 不是合法的 Reddit 名称: {value!r}")
    return value


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return int(result) if result.is_integer() else result


def _parse_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _reddit_url(subreddit, post_id):
    if subreddit:
        return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    return f"https://www.reddit.com/comments/{post_id}/"


def _parse_post_url(url):
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    hostname = (parts.hostname or "").casefold()
    if hostname != "reddit.com" and not hostname.endswith(".reddit.com"):
        return None
    match = _POST_PATH.match(parts.path)
    if not match:
        return None
    subreddit = match.group(1)
    if subreddit is not None and not _NAME.fullmatch(subreddit):
        subreddit = None
    post_id = match.group(2)
    if not _POST_ID.fullmatch(post_id):
        return None
    return subreddit, post_id


def tavily_to_item(raw):
    """Tavily result → Item；解析不到帖子 id 的非帖子 URL 返回 None。"""
    parsed = _parse_post_url(raw.get("url"))
    if not parsed:
        return None
    subreddit, post_id = parsed
    content = raw.get("content")
    if not content and raw.get("raw_content"):
        content = raw.get("raw_content")
    extra = {
        "provider": "tavily",
        "enriched": False,
        "subreddit": subreddit or "",
        "text_source": "tavily_chunk",
    }
    relevance = _num(raw.get("score"))
    if relevance is not None:
        extra["relevance_score"] = relevance
    return Item(
        channel="reddit",
        id=post_id,
        url=_reddit_url(subreddit, post_id),
        title=str(raw.get("title") or ""),
        text=str(content or ""),
        author=Author(),
        created_at=to_rfc3339(_parse_date(raw.get("published_date"))),
        metrics=Metrics(),
        score=None,
        extra=extra,
    )


def arctic_to_item(raw):
    post_id = str(raw.get("id") or "")
    if not _POST_ID.fullmatch(post_id):
        return None
    subreddit = str(raw.get("subreddit") or "")
    permalink = raw.get("permalink")
    if isinstance(permalink, str) and permalink:
        if permalink.startswith("http"):
            url = permalink
        else:
            url = f"https://www.reddit.com{permalink}"
    elif subreddit:
        url = _reddit_url(subreddit, post_id)
    else:
        url = str(raw.get("url") or "")
    if not url:
        return None
    author_name = str(raw.get("author") or "")
    score = _num(raw.get("score"))
    extra = {
        "provider": "arctic",
        "enriched": True,
        "subreddit": subreddit,
        "text_source": "arctic_selftext",
    }
    for key in ("author_fullname", "over_18", "link_flair_text"):
        if key in raw:
            extra[key] = raw.get(key)
    if isinstance(raw.get("url"), str) and raw.get("url"):
        extra["external_url"] = raw["url"]
    return Item(
        channel="reddit",
        id=str(post_id),
        url=url,
        title=str(raw.get("title") or ""),
        text=str(raw.get("selftext") or ""),
        author=Author(
            id=author_name,
            name=author_name,
            url=f"https://www.reddit.com/user/{author_name}/" if author_name else "",
        ),
        created_at=to_rfc3339(_parse_date(raw.get("created_utc"))),
        metrics=Metrics(likes=score, comments=_num(raw.get("num_comments"))),
        score=score,
        extra=extra,
    )


def _apply_arctic_fields(item, raw):
    fields = []
    author_name = raw.get("author")
    if isinstance(author_name, str) and author_name:
        item.author = Author(
            id=author_name,
            name=author_name,
            url=f"https://www.reddit.com/user/{author_name}/",
        )
        fields.append("author")
    score = _num(raw.get("score"))
    if score is not None:
        item.metrics.likes = score
        item.score = score
        fields.append("score")
    comments = _num(raw.get("num_comments"))
    if comments is not None:
        item.metrics.comments = comments
        fields.append("num_comments")
    created = _parse_date(raw.get("created_utc"))
    if created is not None:
        item.created_at = to_rfc3339(created)
        fields.append("created_utc")
    selftext = raw.get("selftext")
    if isinstance(selftext, str) and selftext:
        item.text = selftext
        item.extra["text_source"] = "arctic_selftext"
        fields.append("selftext")
    subreddit = raw.get("subreddit")
    if isinstance(subreddit, str) and subreddit:
        item.extra["subreddit"] = subreddit
        fields.append("subreddit")
    for key in ("author_fullname", "over_18", "link_flair_text"):
        if key in raw and raw.get(key) is not None:
            item.extra[key] = raw.get(key)
            fields.append(key)
    if fields:
        item.extra["enriched"] = True
        item.extra["enriched_fields"] = fields
    return item


def _in_window(item, cutoff):
    created = parse_rfc3339(item.created_at)
    if created is None:
        return False
    return created >= cutoff


def _sort_items(items, sort):
    if sort == "time":
        def key(pair):
            idx, item = pair
            created = parse_rfc3339(item.created_at)
            if created is None:
                return (1, 0.0, idx)
            return (0, -created.timestamp(), idx)
        return [item for _, item in sorted(enumerate(items), key=key)]
    if sort == "score":
        def key(pair):
            idx, item = pair
            value = _num(item.score)
            return (1, 0.0, idx) if value is None else (0, -value, idx)
        return [item for _, item in sorted(enumerate(items), key=key)]
    return list(items)


def _join_notes(notes):
    return "；".join(n for n in notes if n)


def _append_note(meta, note):
    meta["notes"] = _join_notes([meta.get("notes"), note])


class RedditChannel(Channel):
    def __init__(
        self,
        name="reddit",
        config=None,
        root=None,
        home=None,
        tavily_provider=None,
        arctic_provider=None,
        clock=time.time,
    ):
        super().__init__(name, config, root)
        self.home = Path(home) if home is not None else paths.eagle_home()
        self.clock = clock
        self._file_config, self._config_error = self._load_private_config()
        self.api_key = self._resolve_api_key()
        self.base_url = str(self._cfg("base_url", DEFAULT_TAVILY_BASE_URL))
        self.arctic_base_url = str(self._cfg("arctic_base_url", DEFAULT_ARCTIC_BASE_URL))
        self.timeout = self._positive_float("timeout_seconds", DEFAULT_TIMEOUT)
        self.cache_ttl_hours = self._nonnegative_float("cache_ttl_hours", 24.0)
        self.degraded_cache_ttl_hours = self._nonnegative_float("degraded_cache_ttl_hours", 1.0)
        self.enrich_timeout = self._positive_float("enrich_timeout_seconds", 5.0)
        try:
            self.default_enrich = _as_bool("enrich", self._cfg("enrich", True), True)
        except ChannelError:
            self.default_enrich = True
        self.tavily_provider = tavily_provider
        self.arctic_provider = arctic_provider
        self.cache = ResultCache(
            self.home / "reddit" / "cache",
            self.cache_ttl_hours,
            degraded_ttl_hours=self.degraded_cache_ttl_hours,
            clock=clock,
        )

    def _load_private_config(self):
        path = self.home / "reddit" / "config.toml"
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            return data if isinstance(data, dict) else {}, ""
        except FileNotFoundError:
            return {}, ""
        except (OSError, tomllib.TOMLDecodeError) as e:
            return {}, f"读取 {path} 失败: {type(e).__name__}"

    def _cfg(self, key, default):
        if key in self.config:
            return self.config[key]
        return self._file_config.get(key, default)

    def _positive_float(self, key, default):
        try:
            value = float(self._cfg(key, default))
            return value if value > 0 else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _nonnegative_float(self, key, default):
        try:
            return max(0.0, float(self._cfg(key, default)))
        except (TypeError, ValueError):
            return float(default)

    def _resolve_api_key(self):
        env_key = os.environ.get("EAGLE_REDDIT_TAVILY_API_KEY")
        if env_key and env_key.strip():
            return env_key.strip()
        file_key = self._file_config.get("api_key")
        if isinstance(file_key, str) and file_key.strip():
            return file_key.strip()
        return ""

    def describe(self):
        descriptor = Descriptor.from_dict(deepcopy(DESCRIPTOR.to_dict()))
        descriptor.name = self.name
        descriptor.options["enrich"]["default"] = self.default_enrich
        return descriptor

    def fallback_descriptor(self):
        """eagle channels 在渠道不可用时仍展示查询参数（不联网）。"""
        return self.describe()

    def status(self):
        if self._config_error:
            return False, f"unavailable: {self._config_error}"
        if not self.api_key:
            return (
                False,
                "auth_required: 未配置 Tavily API key（设置 EAGLE_REDDIT_TAVILY_API_KEY，"
                f"或写入 {self.home / 'reddit' / 'config.toml'} 的 api_key）；仅 subreddit/author 查询可走 Arctic Shift",
            )
        return True, ""

    def _provider_error(self, error):
        if isinstance(error, ChannelError):
            return error
        if isinstance(error, RedditProviderError):
            return ChannelError(
                error.code,
                redact_secret(error.message, self.api_key),
                error.retry_after_seconds,
                channel=self.name,
            )
        return ChannelError(
            INTERNAL, redact_secret(f"{type(error).__name__}: {error}", self.api_key), channel=self.name
        )

    def _tavily(self):
        if not self.api_key:
            raise ChannelError(
                AUTH_REQUIRED,
                "缺少 Tavily API key：设置 EAGLE_REDDIT_TAVILY_API_KEY，"
                f"或写入 {self.home / 'reddit' / 'config.toml'} 的 api_key；"
                "带 subreddit/author 的 Arctic Shift 查询不需要 key。",
                channel=self.name,
            )
        if self.tavily_provider is None:
            self.tavily_provider = TavilyProvider(self.api_key, self.base_url, self.timeout)
        return self.tavily_provider

    def _arctic(self):
        if self.arctic_provider is None:
            self.arctic_provider = ArcticProvider(self.arctic_base_url, self.timeout)
        return self.arctic_provider

    def _tavily_search(self, query, subreddit, now):
        provider = self._tavily()
        try:
            raw = provider.search(
                query.text,
                limit=query.limit,
                subreddit=subreddit,
                since_hours=query.since_hours,
                now=lambda: now,
            )
        except Exception as e:  # noqa: BLE001
            raise self._provider_error(e) from None
        items = []
        seen = set()
        dropped = 0
        for entry in raw:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            item = tavily_to_item(entry)
            if item is None or item.id in seen:
                dropped += 1
                continue
            seen.add(item.id)
            items.append(item)
        return items, dropped

    def _arctic_search(self, query, subreddit, author, now):
        try:
            raw = self._arctic().search(
                query.text,
                subreddit=subreddit,
                author=author,
                since_hours=query.since_hours,
                limit=query.limit,
                now=lambda: now,
            )
        except Exception as e:  # noqa: BLE001
            raise self._provider_error(e) from None
        items = [arctic_to_item(entry) for entry in raw if isinstance(entry, dict)]
        return [item for item in items if item is not None]

    def _enrich(self, items, notes):
        ids = [item.id for item in items if item.id]
        if not ids:
            return items, False
        try:
            posts = self._arctic().fetch_by_ids(ids, timeout=self.enrich_timeout)
        except Exception as e:  # noqa: BLE001
            notes.append(f"Arctic 补全失败（主结果保留）：{self._provider_error(e).message}")
            return items, True
        if not isinstance(posts, list):
            notes.append("Arctic 补全失败（主结果保留）：响应不是数组")
            return items, True
        by_id = {}
        for post in posts:
            if isinstance(post, dict) and post.get("id") is not None:
                by_id[str(post["id"])] = post
        for item in items:
            post = by_id.get(item.id)
            if post is not None:
                _apply_arctic_fields(item, post)
        return items, False

    def search(self, query):
        query.validate()
        options = query.options or {}
        unknown = sorted(set(options) - set(OPTIONS))
        subreddit = _as_name("subreddit", options.get("subreddit"), "r/")
        author = _as_name("author", options.get("author"), "u/")
        enrich = _as_bool("enrich", options.get("enrich"), self.default_enrich)
        read_cache = _as_bool("cache", options.get("cache"), True)
        ignored = [f"options.{key}" for key in unknown]
        now = float(self.clock())
        route = "arctic" if (subreddit or author) else "tavily"
        cache_options = dict(options)
        cache_options.pop("cache", None)
        if subreddit is None:
            cache_options.pop("subreddit", None)
        else:
            cache_options["subreddit"] = subreddit
        if author is None:
            cache_options.pop("author", None)
        else:
            cache_options["author"] = author
        cache_options["enrich"] = enrich
        cache_key = make_key(route, query.text, query.limit, query.since_hours, query.sort, cache_options)

        if self.cache_ttl_hours > 0 and read_cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                meta = dict(hit.result.meta)
                items = list(hit.result.items)
                if query.since_hours:
                    cutoff = datetime.fromtimestamp(now - float(query.since_hours) * 3600, timezone.utc)
                    before = len(items)
                    items = [item for item in items if _in_window(item, cutoff)]
                    removed = before - len(items)
                    if removed > 0:
                        _append_note(meta, f"缓存命中后 since_hours 过滤掉 {removed} 条")
                        if not items:
                            _append_note(meta, "缓存过滤后没有结果")
                meta["cache"] = "hit"
                meta["cached_at"] = to_rfc3339(datetime.fromtimestamp(hit.cached_at, timezone.utc))
                meta["cache_age_seconds"] = int(hit.age_seconds)
                meta["degraded"] = hit.degraded
                return SearchResult(items, meta)

        notes = []
        provider_used = route
        items = []
        enrich_failed = False
        dropped = 0
        if route == "arctic":
            try:
                items = self._arctic_search(query, subreddit, author, now)
                notes.append("Arctic Shift 限定搜索")
            except ChannelError as e:
                if e.code not in (UNAVAILABLE, INTERNAL):
                    raise
                if not self.api_key:
                    message = redact_secret(
                        f"{e.message}；配置 Tavily API key 后可自动回退", self.api_key
                    )
                    raise ChannelError(e.code, message, e.retry_after_seconds, channel=self.name) from None
                notes.append(f"Arctic Shift 失败（{e.code}: {e.message}），回退 Tavily")
                provider_used = "tavily"
                if author:
                    ignored.append("author")
                try:
                    items, dropped = self._tavily_search(query, subreddit, now)
                except ChannelError as fallback_error:
                    message = redact_secret(
                        f"Arctic Shift 失败（{e.code}: {e.message}）；"
                        f"Tavily 回退也失败（{fallback_error.code}: {fallback_error.message}）",
                        self.api_key,
                    )
                    raise ChannelError(
                        fallback_error.code,
                        message,
                        fallback_error.retry_after_seconds,
                        channel=self.name,
                    ) from None
        else:
            items, dropped = self._tavily_search(query, subreddit, now)

        if dropped:
            notes.append(f"Tavily 丢弃 {dropped} 条（非帖子或重复）")

        if subreddit:
            wanted = subreddit.casefold()
            items = [
                item for item in items
                if str(item.extra.get("subreddit") or "").casefold() == wanted
            ]

        if provider_used == "tavily" and enrich:
            items, enrich_failed = self._enrich(items, notes)

        if query.since_hours:
            cutoff = datetime.fromtimestamp(now - float(query.since_hours) * 3600, timezone.utc)
            before = len(items)
            items = [item for item in items if _in_window(item, cutoff)]
            if len(items) != before:
                notes.append(f"since_hours 本地过滤掉 {before - len(items)} 条（缺 created_at 的也丢弃）")

        items = _sort_items(items, query.sort)
        if query.limit > 0:
            items = items[:query.limit]

        if not items:
            notes.append("没有结果")
        elif provider_used == "tavily":
            notes.append("Tavily content 为搜索摘要片段，正文可能不完整")

        meta = {
            "query": query.text,
            "provider": provider_used,
            "ignored": sorted(set(ignored)),
            "notes": _join_notes(notes),
            "degraded": provider_used != route or enrich_failed or "author" in ignored,
        }
        result = SearchResult(items, meta)

        if self.cache_ttl_hours > 0:
            try:
                written = self.cache.put(cache_key, result, degraded=meta["degraded"])
                meta["cache"] = ("miss" if read_cache else "bypass") if written else "disabled"
            except OSError:
                meta["cache"] = "write_failed"
        else:
            meta["cache"] = "disabled"
        return result
