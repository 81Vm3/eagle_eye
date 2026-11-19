"""多渠道并发 fan-out：单渠道失败不影响其他渠道；按 sort 合并、跨渠道 URL 去重、截断到 limit。"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit

from .models import INTERNAL, ChannelError, Query

# 去重时丢弃的易变 / 追踪类 query 参数
VOLATILE_PARAMS = {"xsec_token", "xsec_source", "s", "t", "ref", "ref_src", "source", "share_id"}
HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "mobile.x.com": "x.com"}


@dataclass
class AggregateResult:
    query: Query
    channels: list  # 实际搜索的渠道名
    items: list = field(default_factory=list)  # 合并后的 Item
    errors: dict = field(default_factory=dict)  # 渠道名 -> ChannelError
    meta: dict = field(default_factory=dict)  # 渠道名 -> 该渠道 SearchResult.meta（含 count / elapsed_seconds）

    @property
    def ok_channels(self):
        return [c for c in self.channels if c not in self.errors]

    @property
    def all_failed(self):
        return bool(self.channels) and not self.ok_channels

    def to_dict(self):
        q = self.query.to_dict()
        q.pop("protocol", None)
        return {
            "protocol": 1,
            "query": q,
            "channels": list(self.channels),
            "items": [i.to_dict() for i in self.items],
            "errors": {k: v.to_dict() for k, v in self.errors.items()},
            "meta": self.meta,
        }


def _stderr(msg):
    print(msg, file=sys.stderr, flush=True)


def split_options(options, channel_names):
    """把 {"pages": 2, "x.pages": 3, "xhs.detail": 1} 拆成每个渠道自己的 options。

    带 "<渠道名>." 前缀的只给该渠道（覆盖同名无前缀键）；无前缀的给所有渠道。
    """
    names = set(channel_names)
    common, scoped = {}, {n: {} for n in channel_names}
    for k, v in (options or {}).items():
        prefix, dot, rest = k.partition(".")
        if dot and prefix in names and rest:
            scoped[prefix][rest] = v
        else:
            common[k] = v
    return {n: {**common, **scoped[n]} for n in channel_names}


def url_key(url):
    """归一化 URL 用于跨渠道去重：忽略协议、www、大小写主机名、结尾斜杠、fragment 和易变参数。"""
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip()
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    host = HOST_ALIASES.get(host, host)
    path = u.path.rstrip("/") or "/"
    params = sorted(
        (k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in VOLATILE_PARAMS and not k.lower().startswith("utm_")
    )
    return f"{host}{path}" + (f"?{urlencode(params)}" if params else "")


def dedupe(items):
    seen, out = set(), []
    for it in items:
        keys = {("url", url_key(it.url)), ("id", it.channel, it.id)}
        if keys & seen:
            continue
        seen |= keys
        out.append(it)
    return out


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def merge(per_channel, sort, limit):
    """per_channel: [(渠道名, [Item...])]，按渠道顺序。

    - relevance：各渠道相关度不可比，按各自原顺序轮流交错（round-robin）
    - time：按 created_at 降序，未知时间排最后
    - score：按 score 降序，未知分数排最后
    排序是稳定的，同分保持交错顺序。
    """
    lists = [list(items) for _, items in per_channel]
    interleaved = []
    for i in range(max((len(x) for x in lists), default=0)):
        interleaved += [x[i] for x in lists if i < len(x)]
    if sort == "time":
        interleaved.sort(key=lambda it: it.created_dt() or _EPOCH, reverse=True)
    elif sort == "score":
        interleaved.sort(key=lambda it: (it.score is not None, it.score or 0), reverse=True)
    out = dedupe(interleaved)
    return out[:limit] if limit else out


def _run_one(channel, query, log):
    t0 = time.monotonic()
    try:
        res = channel.search(query)
    except ChannelError as e:
        e.channel = e.channel or channel.name
        return None, e, time.monotonic() - t0
    except Exception as e:  # noqa: BLE001 — 渠道 bug 不能拖垮整次搜索
        return None, ChannelError(INTERNAL, f"{type(e).__name__}: {e}", channel=channel.name), time.monotonic() - t0
    for it in res.items:
        it.channel = it.channel or channel.name
    return res, None, time.monotonic() - t0


def fan_out(channels, query, log=_stderr, max_workers=None):
    """并发向多个渠道发同一个 Query，返回 AggregateResult。

    query.options 支持 "<渠道名>.<键>" 前缀，由 split_options 拆给对应渠道。
    """
    query.validate()
    names = [c.name for c in channels]
    opts = split_options(query.options, names)
    agg = AggregateResult(query=query, channels=names)
    if not channels:
        return agg
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers or len(channels)) as pool:
        futs = {c.name: pool.submit(_run_one, c, query.with_options(opts[c.name]), log) for c in channels}
        for name, fut in futs.items():
            res, err, elapsed = fut.result()
            if err:
                agg.errors[name] = err
                agg.meta[name] = {"elapsed_seconds": round(elapsed, 2)}
                log(f"[{name}] 失败 {err.code}: {err.message}")
            else:
                results[name] = res
                agg.meta[name] = {**res.meta, "count": len(res.items), "elapsed_seconds": round(elapsed, 2)}
                log(f"[{name}] {len(res.items)} 条（{elapsed:.1f}s）")
    agg.items = merge([(n, results[n].items) for n in names if n in results], query.sort, query.limit)
    return agg
