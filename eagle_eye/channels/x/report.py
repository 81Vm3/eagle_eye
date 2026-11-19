"""把推文渲染成 Markdown，供人或 agent 阅读、交给 LLM 归纳。"""

from .pipeline import parse_time


def fmt_time(created_at):
    dt = parse_time(created_at)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else (created_at or "?")


def one_line(text, limit):
    text = " ".join((text or "").split())
    return text if not limit or len(text) <= limit else text[:limit] + "…"


def to_markdown(items, title, meta_lines=(), text_limit=500, article_limit=600):
    lines = [f"# {title}", ""]
    lines += [f"- {m}" for m in meta_lines]
    lines.append("")
    for i, t in enumerate(items, 1):
        followers = t.get("followers")
        followers = f"{followers:,}" if isinstance(followers, int) else "?"
        reply = " · 回复" if t.get("isReply") else ""
        lines.append(
            f"## {i}. @{t['user']} · {t['likes']}♥ {t['retweets']}RT · "
            f"{followers} 粉 · {fmt_time(t['createdAt'])}{reply}"
        )
        lines.append(t["url"] or "")
        if t.get("hits"):
            lines.append(f"命中: {', '.join(t['hits'])}")
        lines.append("")
        lines.append(f"> {one_line(t['text'], text_limit)}")
        art = t.get("article")
        if art:
            lines.append("")
            lines.append(f"**长文《{one_line(art['title'], 120)}》** {one_line(art['text'], article_limit)}")
        lines.append("")
    return "\n".join(lines)
