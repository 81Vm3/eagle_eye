"""eagle skill install|path|show：把包内 Agent Skill（skills/eagle-search，遵循开放标准 Agent Skills）交给用户的 Agent。

install：装到 <skills 目录>/eagle-search。skills 目录默认 ~/.agents/skills（跨工具的用户级约定）；
  --agent 只放有把握的预设（agents → ~/.agents/skills，claude → ~/.claude/skills），其他 Agent 用 --target DIR。
  默认软链（升级 eagle-eye 后自动跟着更新），--copy 复制一份（可自行修改）。目标已存在时不覆盖。只在用户主动运行时安装。
path：打印包内 Skill 目录。
show：把 SKILL.md 正文（去掉 frontmatter）打印到 stdout，供不支持 Skill 的 Agent 放进 AGENTS.md / 系统提示词 / 规则文件。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import paths

SKILL = "eagle-search"

# 只放有把握的预设；其他 Agent 的 skills 目录请查阅其文档，用 --target 指定。
AGENT_DIRS = {
    "agents": (".agents", "skills"),
    "claude": (".claude", "skills"),
}
DEFAULT_AGENT = "agents"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def skill_dir():
    return paths.bundled("skills", SKILL).resolve()


def agent_target(agent=DEFAULT_AGENT):
    return Path.home().joinpath(*AGENT_DIRS[agent])


def default_target():
    return agent_target(DEFAULT_AGENT)


def install(target=None, copy=False, agent=None):
    if target and agent:
        log("--target 和 --agent 只能选一个")
        return 2
    src = skill_dir()
    target = Path(target).expanduser() if target else agent_target(agent or DEFAULT_AGENT)
    dest = target / SKILL
    if dest.is_symlink() or dest.exists():
        if dest.is_symlink() and dest.resolve() == src:
            log(f"Skill 已安装（软链接）: {dest} -> {src}")
            print(dest)
            return 0
        log(f"{dest} 已存在，不覆盖。要重装请先删除它（rm -r {dest}），再运行 eagle skill install")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(src, dest)
        log(f"已复制 Skill 到 {dest}（升级 eagle-eye 后需删除再重装才会更新）")
    else:
        dest.symlink_to(src, target_is_directory=True)
        log(f"已软链 {dest} -> {src}（重装 eagle-eye 后若链接失效，删除后重新运行 eagle skill install）")
    print(dest)
    return 0


def split_frontmatter(text):
    """返回 (frontmatter 里的 description, 正文)。只做 SKILL.md 需要的最简解析（单行 key: value）。"""
    if text.startswith("---"):
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                desc = ""
                for line in lines[1:i]:
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()
                return desc, "".join(lines[i + 1 :]).lstrip("\n")
    return "", text


def show():
    desc, body = split_frontmatter((skill_dir() / "SKILL.md").read_text(encoding="utf-8"))
    # 加一行标题和用途说明，粘贴进 AGENTS.md / 系统提示词时 Agent 知道何时用
    head = f"# {SKILL}\n\n" + (f"{desc}\n\n" if desc else "")
    sys.stdout.write(head + body)
    return 0


def cmd_skill(args):
    try:
        if args.skill_cmd == "path":
            print(skill_dir())
            return 0
        if args.skill_cmd == "show":
            return show()
        return install(args.target, args.copy, args.agent)
    except OSError as e:
        log(f"错误: {e}")
        return 1
