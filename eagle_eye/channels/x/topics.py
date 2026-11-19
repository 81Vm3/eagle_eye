"""X 预设搜索主题：eagle_eye/channels/x/topics/<name>.toml，一个文件描述一类长期要搜的东西。"""

import tomllib
from pathlib import Path

TOPICS_DIR = Path(__file__).resolve().parent / "topics"

DEFAULTS = {
    "description": "",
    "window_hours": 24,
    "pages_per_query": 2,
    "query_type": "Latest",
    "query_suffix": "-is:retweet",
    "fetch_articles": 0,
    "keywords": [],
    "exclude": [],
    "min_score": 0,
    "max_results": 150,
    "sort": "score",
    "prompt": "",
    "summarize_command": ["claude", "-p"],
}


def path_for(name):
    p = Path(name)
    if p.suffix == ".toml" and p.exists():
        return p
    return TOPICS_DIR / f"{name}.toml"


def load(name):
    p = path_for(name)
    if not p.exists():
        raise RuntimeError(f"找不到主题 {name}（{p}）。可用主题: {', '.join(names()) or '无'}")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    if not data.get("queries"):
        raise RuntimeError(f"{p} 缺少 queries")
    topic = {**DEFAULTS, **data}
    topic["name"] = p.stem
    return topic


def names():
    return sorted(p.stem for p in TOPICS_DIR.glob("*.toml"))
