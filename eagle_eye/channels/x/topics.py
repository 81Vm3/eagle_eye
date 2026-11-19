"""X 预设搜索主题：<name>.toml，一个文件描述一类长期要搜的东西。

查找目录（同名时前者优先）：$EAGLE_HOME/x/topics/（用户主题，安装模式推荐放这里）、
包内 eagle_eye/channels/x/topics/（源码仓库里就是该目录）。
"""

import tomllib
from pathlib import Path

from ... import paths

TOPICS_DIR = Path(__file__).resolve().parent / "topics"


def user_topics_dir():
    return paths.eagle_home() / "x" / "topics"


def topic_dirs():
    return [user_topics_dir(), TOPICS_DIR]

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
    for d in topic_dirs():
        if (d / f"{name}.toml").exists():
            return d / f"{name}.toml"
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
    return sorted({p.stem for d in topic_dirs() if d.is_dir() for p in d.glob("*.toml")})
