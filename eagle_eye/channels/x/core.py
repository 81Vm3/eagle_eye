"""对外的 Python 接口：一次调用完成 搜索 → 去重 → 长文补全 → 过滤排序。"""

import sys
import time

from . import pipeline
from .api import make_client


def _stderr(msg):
    print(msg, file=sys.stderr, flush=True)


def since_ts(hours):
    return int(time.time() - hours * 3600) if hours else None


def collect(
    queries,
    hours=24,
    pages=1,
    query_type="Latest",
    suffix="-is:retweet",
    articles=0,
    client=None,
    raw=None,
    log=_stderr,
    errors=None,
):
    """返回 (raw, tweets)：raw 为 {query: [原始推文]}，tweets 为去重后的原始推文。

    传入 raw 时跳过搜索（用于离线复跑）；articles>0 时为最热的 N 条纯链接推文补全长文。
    """
    if raw is None:
        client = client or make_client()
        raw = pipeline.search_all(client, queries, since_ts(hours), pages, suffix, query_type, log, errors)
    tweets = pipeline.dedupe(raw)
    log(f"去重后 {len(tweets)} 条")
    if articles:
        pipeline.enrich_articles(client or make_client(), tweets, articles, log)
    return raw, tweets


def search(
    queries,
    hours=24,
    pages=1,
    query_type="Latest",
    suffix="-is:retweet",
    articles=0,
    keywords=(),
    exclude=(),
    min_score=0,
    limit=50,
    sort="score",
    client=None,
    log=_stderr,
):
    """搜索 X 并返回精简后的推文 dict 列表（字段见 pipeline.slim）。

    queries 可以是单个字符串或列表，支持 X 高级搜索语法（from:、lang:、min_faves:、OR、-排除词 等）。
    hours=0 表示不加时间限制。
    """
    if isinstance(queries, str):
        queries = [queries]
    _, tweets = collect(queries, hours, pages, query_type, suffix, articles, client, log=log)
    picked = pipeline.select(tweets, keywords, exclude, min_score, limit, sort)
    return [pipeline.slim(t) for t in picked]
