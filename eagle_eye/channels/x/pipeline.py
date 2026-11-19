"""搜索 → 去重 → 补全长文 → 过滤排序。"""

import re
from datetime import datetime

LINK_ONLY = re.compile(r"^\s*(https://t\.co/\S+\s*)+$")


def build_query(query, since=None, suffix=""):
    parts = [query]
    if since:
        parts.append(f"since_time:{since}")
    if suffix:
        parts.append(suffix)
    return " ".join(parts)


def search_all(client, queries, since, pages, suffix="", query_type="Latest", log=print, errors=None):
    """逐条执行搜索，返回 {query: [tweet, ...]}。单条失败不影响其他查询；传 errors 列表时把异常收集进去。"""
    out = {}
    for i, q in enumerate(queries, 1):
        tweets, cursor = [], ""
        for _ in range(pages):
            try:
                d = client.search(build_query(q, since, suffix), cursor, query_type)
            except RuntimeError as e:
                log(f"  [{i}] 失败: {e}")
                if errors is not None:
                    errors.append(e)
                break
            tweets += d.get("tweets", [])
            cursor = d.get("next_cursor") or ""
            if not d.get("has_next_page") or not cursor:
                break
        log(f"[{i}/{len(queries)}] {len(tweets):>3} 条  {q[:70]}")
        out[q] = tweets
    return out


def dedupe(raw):
    seen = {}
    for tweets in raw.values():
        for t in tweets:
            seen.setdefault(t["id"], t)
    return list(seen.values())


def score(t):
    return t.get("likeCount", 0) + 2 * t.get("retweetCount", 0)


def parse_time(created_at):
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None


def is_link_only(t):
    return bool(LINK_ONLY.match(t.get("text", "")))


def parse_article(resp):
    a = resp.get("article") or resp.get("data") or {}
    blocks = a.get("contents") or []
    body = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return {"title": a.get("title") or "", "text": body or a.get("preview_text") or ""}


def enrich_articles(client, tweets, top_n, log=print):
    """只有链接的推文多半是 X 长文，按热度取前 top_n 条拉正文。"""
    targets = sorted((t for t in tweets if is_link_only(t)), key=score, reverse=True)
    for t in targets[:top_n]:
        try:
            art = parse_article(client.article(t["id"]))
        except RuntimeError as e:
            log(f"  长文 {t['id']} 失败: {e}")
            continue
        if art["title"] or art["text"]:
            t["_article"] = art
            log(f"  长文 @{t.get('author', {}).get('userName')}: {art['title'][:50]}")


def haystack(t):
    art = t.get("_article") or {}
    return " ".join([t.get("text", ""), art.get("title", ""), art.get("text", "")]).lower()


def matched(text, keywords):
    return [k for k in keywords if k.lower() in text]


def select(tweets, keywords=(), exclude=(), min_score=0, limit=None, sort="score"):
    """丢掉命中排除词、没命中关键词（关键词为空则不限）、热度不够的推文，再排序截断。

    sort: "score" 按 赞+2×转推 降序；"time" 按发布时间降序；"none"/"relevance" 保持接口返回顺序。
    """
    out = []
    for t in tweets:
        text = haystack(t)
        if matched(text, exclude):
            continue
        hits = matched(text, keywords)
        if keywords and not hits:
            continue
        if score(t) < min_score:
            continue
        t["_hits"] = hits
        out.append(t)
    if sort == "time":
        epoch = datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
        out.sort(key=lambda t: parse_time(t.get("createdAt")) or epoch, reverse=True)
    elif sort == "score":
        out.sort(key=score, reverse=True)
    return out[:limit] if limit else out


def slim(t):
    a = t.get("author") or {}
    return {
        "id": t.get("id"),
        "url": t.get("url"),
        "createdAt": t.get("createdAt"),
        "user": a.get("userName"),
        "name": a.get("name"),
        "followers": a.get("followers"),
        "likes": t.get("likeCount", 0),
        "retweets": t.get("retweetCount", 0),
        "replies": t.get("replyCount", 0),
        "views": t.get("viewCount"),
        "lang": t.get("lang"),
        "isReply": t.get("isReply", False),
        "score": score(t),
        "hits": t.get("_hits", []),
        "text": t.get("text", ""),
        "article": t.get("_article"),
    }
