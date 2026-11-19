"""统一命令行：bin/eagle <子命令> ...

    eagle channels [--format md|json]           列出渠道、describe 元数据和是否可用
    eagle search <查询...> [-c x,xhs] [...]     多渠道并发搜索，合并输出
    eagle setup [--check] [--version V]        准备 EAGLE_HOME：下载小红书渠道二进制、生成配置；--check 只体检
    eagle skill install [--agent agents|claude | --target DIR] [--copy]  安装 Agent Skill（默认 ~/.agents/skills，软链）
    eagle skill path | show                     打印包内 Skill 目录 / 打印 SKILL.md 正文（供不支持 Skill 的 Agent）
    eagle <渠道名> ...                          渠道原生子命令（eagle x search|run|topics|article|login|extension；eagle xhs login|browser）

stdout 只有结果；进度和错误都在 stderr。search 全部渠道失败时 exit 1，参数错误 exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import output
from .core.models import SORTS, ChannelError, Query
from .core.registry import Registry
from .core.router import fan_out

COMMANDS = ("channels", "search", "setup", "skill")
NO_REGISTRY = ("setup", "skill")  # 不需要先读渠道配置（配置坏了也能 setup）


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_value(v):
    """--opt 的值：能按 JSON 解析就用 JSON（2 / true / ["a","b"]），否则当字符串。"""
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


def parse_opts(pairs):
    opts = {}
    for p in pairs or []:
        k, eq, v = p.partition("=")
        if not eq or not k.strip():
            raise ValueError(f"--opt 需要 键=值 格式: {p!r}")
        opts[k.strip()] = parse_value(v)
    return opts


def channel_rows(reg, names=None):
    rows = []
    for name in names or reg.names():
        ch = reg.get(name)
        available, reason = ch.status()
        desc = None
        try:
            desc = ch.describe().to_dict() if available else None
        except ChannelError as e:
            available, reason = False, f"{e.code}: {e.message}"
        except Exception as e:  # noqa: BLE001
            available, reason = False, f"internal: {type(e).__name__}: {e}"
        if desc is None:
            fb = getattr(ch, "fallback_descriptor", None)
            desc = fb().to_dict() if fb else None
        rows.append({
            "name": name,
            "type": reg.entries[name].get("type"),
            "enabled": reg.is_enabled(name),
            "default": reg.is_default(name),
            "available": available,
            "reason": reason,
            "descriptor": desc,
        })
    return rows


def cmd_channels(args, reg):
    rows = channel_rows(reg)
    print(output.channels_to_json(rows) if args.format == "json" else output.channels_to_markdown(rows))
    return 0


def cmd_search(args, reg):
    if args.channels:
        names = [n.strip() for n in args.channels.split(",") if n.strip()]
        unknown = [n for n in names if n not in reg.entries]
        if unknown:
            log(f"错误: 未知渠道 {', '.join(unknown)}（已登记: {', '.join(reg.names())}）")
            return 2
    else:
        names = reg.default_names()
    names = list(dict.fromkeys(names))
    if not names:
        log("错误: 没有默认参与搜索的渠道（检查 eagle.toml 的 enabled / default，或用 -c 指定）")
        return 2
    try:
        opts = parse_opts(args.opt)
        query = Query(" ".join(args.query), args.limit, args.hours or None, args.sort, opts).validate()
    except (ValueError, ChannelError) as e:
        log(f"错误: {e}")
        return 2
    log(f"搜索 {', '.join(names)}: {query.text}")
    agg = fan_out([reg.get(n) for n in names], query, log=log)
    if args.format == "json":
        print(output.to_json(agg))
    else:
        print(output.to_markdown(agg, text_limit=0 if args.full else 500))
    if agg.all_failed:
        log("全部渠道失败")
        return 1
    return 0


def build_parser(channel_names=()):
    natives = f"；渠道原生命令: {', '.join(f'eagle {n} ...' for n in channel_names)}" if channel_names else ""
    p = argparse.ArgumentParser(
        prog="eagle",
        description=f"eagle_eye：给 Agent 用的统一搜索中间层{natives}",
    )
    p.add_argument("--config", help="渠道配置文件（默认 EAGLE_CONFIG > $EAGLE_HOME/eagle.toml > 包内默认）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("channels", help="列出渠道、describe 元数据和是否可用")
    c.add_argument("--format", default="md", choices=["md", "json"])
    c.set_defaults(func=cmd_channels)

    s = sub.add_parser("search", help="多渠道并发搜索（stdout 只有结果）")
    s.add_argument("query", nargs="+", help="查询文本，多个词用空格拼接；各渠道语法见 eagle channels")
    s.add_argument("-c", "--channels", help="逗号分隔的渠道名；不填时只搜 enabled 且 default=true 的渠道（xhs 默认不参与，需 -c xhs）")
    s.add_argument("--limit", type=int, default=20, help="合并后最多输出条数，也传给每个渠道（默认 20，0 表示不限）")
    s.add_argument("--hours", type=float, default=None, help="只要最近 N 小时，默认不限")
    s.add_argument("--sort", default="relevance", choices=list(SORTS), help="relevance（默认，各渠道顺序交错）| time | score")
    s.add_argument(
        "--opt", action="append", metavar="K=V",
        help="渠道私有参数，可重复；带渠道前缀只给该渠道，如 --opt x.pages=2 --opt xhs.detail=0；值按 JSON 解析",
    )
    s.add_argument("--format", default="md", choices=["md", "json"])
    s.add_argument("--full", action="store_true", help="Markdown 不截断正文")
    s.set_defaults(func=cmd_search)

    from .setup_cmd import cmd_setup
    from .skill_cmd import cmd_skill

    st = sub.add_parser("setup", help="准备 EAGLE_HOME：下载并校验小红书渠道二进制、生成 xhs/config.yaml（幂等）")
    st.add_argument("--check", action="store_true", help="只体检（渠道可用性、二进制版本、配置、Chrome 调试端口），不改文件")
    st.add_argument("--version", help="eagle-xhs 版本（默认与本包版本相同）")
    st.add_argument("--from-source", action="store_true", help="在源码仓库里用 go 本地构建，而不是下载")
    st.add_argument("--force", action="store_true", help="即使已是目标版本也重新下载")
    st.set_defaults(func=cmd_setup)

    from .skill_cmd import AGENT_DIRS, DEFAULT_AGENT

    sk = sub.add_parser("skill", help="Agent Skill（eagle-search，开放标准 Agent Skills）：安装 / 查看")
    sks = sk.add_subparsers(dest="skill_cmd", required=True)
    si = sks.add_parser(
        "install",
        help="把 eagle-search Skill 装到 <skills 目录>/eagle-search（默认 ~/.agents/skills，软链，已存在不覆盖）",
    )
    where = si.add_mutually_exclusive_group()
    where.add_argument(
        "--agent",
        choices=sorted(AGENT_DIRS),
        help=f"预设 skills 目录：agents = ~/.agents/skills（默认，跨工具的用户级约定），"
        f"claude = ~/.claude/skills（Claude Code）；默认 {DEFAULT_AGENT}",
    )
    where.add_argument("--target", metavar="DIR", help="自定义 skills 目录（其他 Agent 请查阅其文档），装到 DIR/eagle-search")
    si.add_argument("--copy", action="store_true", help="复制而不是软链")
    si.set_defaults(func=cmd_skill)
    sks.add_parser("path", help="打印包内 Skill 目录").set_defaults(func=cmd_skill)
    sks.add_parser(
        "show", help="把 SKILL.md 正文打印到 stdout（不支持 Skill 的 Agent：放进 AGENTS.md / 系统提示词 / 规则文件）"
    ).set_defaults(func=cmd_skill)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config = None
    if len(argv) >= 2 and argv[0] == "--config":
        config, argv = argv[1], argv[2:]
    if argv and argv[0] in NO_REGISTRY:
        args = build_parser().parse_args(argv)
        args.config = config
        return args.func(args)
    try:
        reg = Registry.load(config)
    except (OSError, ValueError) as e:
        log(f"错误: {e}")
        return 2

    # eagle <渠道名> ... → 渠道原生子命令
    if argv and argv[0] in reg.entries and argv[0] not in COMMANDS:
        name = argv[0]
        try:
            return reg.get(name).native(argv[1:]) or 0
        except ChannelError as e:
            log(f"错误: {e.code}: {e.message}")
            return 1

    args = build_parser(reg.names()).parse_args(argv)
    return args.func(args, reg)
