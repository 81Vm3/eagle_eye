"""把 AggregateResult / 渠道列表渲染成 JSON 或 Markdown。Markdown 每条都标注渠道和原文链接。"""

from __future__ import annotations

import json
from datetime import datetime

METRIC_LABELS = (("likes", "赞"), ("reposts", "转"), ("comments", "评"), ("collects", "藏"), ("views", "阅"))


def one_line(text, limit):
    text = " ".join((text or "").split())
    return text if not limit or len(text) <= limit else text[:limit] + "…"


def fmt_time(item):
    dt = item.created_dt()
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "时间未知"


def fmt_metrics(m):
    parts = [f"{getattr(m, k)}{label}" for k, label in METRIC_LABELS if getattr(m, k) is not None]
    return " ".join(parts)


def to_json(agg):
    return json.dumps(agg.to_dict(), ensure_ascii=False, indent=2)


def to_markdown(agg, text_limit=500):
    q = agg.query
    lines = [f"# 搜索: {q.text}", ""]
    window = f"过去 {q.since_hours:g} 小时" if q.since_hours else "不限"
    lines.append(f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"- 渠道: {', '.join(agg.channels) or '无'} · 时间窗口: {window} · 排序: {q.sort} · 输出 {len(agg.items)} 条")
    for name in agg.channels:
        m = agg.meta.get(name, {})
        if name in agg.errors:
            e = agg.errors[name]
            retry = f"（{e.retry_after_seconds} 秒后可重试）" if e.retry_after_seconds else ""
            lines.append(f"- ⚠️ [{name}] 失败 `{e.code}`: {e.message}{retry}")
        else:
            extra = []
            if m.get("ignored"):
                extra.append(f"忽略参数: {', '.join(map(str, m['ignored']))}")
            if m.get("notes"):
                extra.append(str(m["notes"]))
            tail = f"（{'；'.join(extra)}）" if extra else ""
            lines.append(f"- [{name}] {m.get('count', 0)} 条{tail}")
    lines.append("")
    for i, it in enumerate(agg.items, 1):
        who = it.author.name or it.author.id or "?"
        if it.author.id and it.author.name and it.author.id != it.author.name:
            who = f"{it.author.name} (@{it.author.id})"
        head = f"## {i}. [{it.channel}] {one_line(it.title, 80) + ' · ' if it.title else ''}{who}"
        metrics = fmt_metrics(it.metrics)
        lines.append(head)
        lines.append(" · ".join(x for x in (fmt_time(it), metrics, f"score {it.score:g}" if it.score is not None else "") if x))
        lines.append(it.url)
        hits = it.extra.get("hits") if isinstance(it.extra, dict) else None
        if hits:
            lines.append(f"命中: {', '.join(map(str, hits))}")
        if it.text:
            lines.append("")
            lines.append(f"> {one_line(it.text, text_limit)}")
        art = it.extra.get("article") if isinstance(it.extra, dict) else None
        if isinstance(art, dict) and (art.get("title") or art.get("text")):
            lines.append("")
            lines.append(f"**长文《{one_line(art.get('title'), 120)}》** {one_line(art.get('text'), text_limit)}")
        lines.append("")
    if not agg.items:
        lines.append("（没有结果）")
    return "\n".join(lines)


def channels_to_json(rows):
    return json.dumps(rows, ensure_ascii=False, indent=2)


def channels_to_markdown(rows):
    lines = ["# eagle_eye 渠道", ""]
    for r in rows:
        state = "✅ 可用" if r["available"] else f"❌ 不可用（{r['reason']}）"
        if not r["enabled"]:
            state = "⏸ 未启用 · " + state
        elif not r.get("default", True):
            state = "需显式 -c " + r["name"] + " · " + state
        d = r.get("descriptor") or {}
        title = d.get("title") or r.get("title") or ""
        lines.append(f"## {r['name']}{' · ' + title if title else ''} — {state}")
        joins = "是" if r.get("default", True) and r["enabled"] else "否（需 -c 显式指定）"
        lines.append(f"- 类型: {r['type']} · 默认参与搜索: {joins}")
        for key, label in (("description", "说明"), ("query_syntax", "查询语法"), ("cost_hint", "代价")):
            if d.get(key):
                lines.append(f"- {label}: {d[key]}")
        opts = d.get("options") or {}
        if opts:
            lines.append("- 私有参数（--opt 键=值，多渠道时可写 渠道名.键=值）:")
            for k, spec in opts.items():
                spec = spec if isinstance(spec, dict) else {}
                default = f"，默认 {json.dumps(spec['default'], ensure_ascii=False)}" if "default" in spec else ""
                lines.append(f"  - `{k}` ({spec.get('type', '?')}{default}) {spec.get('description', '')}")
        lines.append("")
    return "\n".join(lines)
