"""X 渠道原生命令：eagle x <子命令> ...（原 xsearch 的全部能力）

stdout 只输出结果（Markdown / JSON / 文件路径），进度和错误都走 stderr，方便 agent 直接读取。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import core, pipeline, report, topics
from . import tws
from .api import make_client

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # 仓库根目录
RUNS_DIR = PROJECT_ROOT / "runs" / "x"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def emit(items, fmt, title, meta, full=False):
    if fmt == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        limit = 0 if full else 500
        print(report.to_markdown(items, title, meta, text_limit=limit, article_limit=limit))


def save_run(out_dir, raw, items, md):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False))
    (out_dir / "results.json").write_text(json.dumps(items, ensure_ascii=False, indent=2))
    (out_dir / "results.md").write_text(md)


def summarize(command, prompt, md, out_dir, timeout=900):
    """把 results.md 通过 stdin 交给 claude -p 归纳，写到 digest.md。"""
    if not shutil.which(command[0]):
        log(f"找不到 {command[0]}，跳过总结")
        return None
    log("调用 Claude 生成总结…")
    r = subprocess.run(command + [prompt], input=md, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        log(f"总结失败 (exit {r.returncode}): {r.stderr[:500]}")
        return None
    path = out_dir / "digest.md"
    path.write_text(r.stdout)
    return path


def meta_lines(hours, raw, tweets, items):
    window = f"过去 {hours:g} 小时" if hours else "不限"
    return [
        f"生成时间: {datetime.now():%Y-%m-%d %H:%M}",
        f"时间窗口: {window}",
        f"原始 {sum(len(v) for v in raw.values())} 条 → 去重 {len(tweets)} 条 → 输出 {len(items)} 条",
    ]


def cmd_search(args):
    raw, tweets = core.collect(args.queries, args.hours, args.pages, args.type, args.suffix, args.articles)
    picked = pipeline.select(tweets, args.keywords, args.exclude, args.min_score, args.limit, args.sort)
    items = [pipeline.slim(t) for t in picked]
    meta = meta_lines(args.hours, raw, tweets, items)
    title = "X 搜索: " + " | ".join(args.queries)
    if args.save:
        save_run(Path(args.save), raw, items, report.to_markdown(items, title, meta))
        log(f"已保存到 {args.save}")
    emit(items, args.format, title, meta, args.full)


def cmd_run(args):
    t = topics.load(args.topic)
    hours = t["window_hours"] if args.hours is None else args.hours
    raw = json.loads(Path(args.from_raw).read_text()) if args.from_raw else None
    if raw is None:
        log(f"主题 {t['name']}: 过去 {hours:g} 小时，{len(t['queries'])} 组查询")
    raw, tweets = core.collect(
        t["queries"],
        hours,
        t["pages_per_query"],
        t["query_type"],
        t["query_suffix"],
        0 if args.no_articles else t["fetch_articles"],
        raw=raw,
    )
    picked = pipeline.select(tweets, t["keywords"], t["exclude"], t["min_score"], t["max_results"], t["sort"])
    items = [pipeline.slim(x) for x in picked]
    log(f"过滤后保留 {len(items)} 条")

    out_dir = Path(args.out) if args.out else RUNS_DIR / t["name"] / datetime.now().strftime("%Y%m%d-%H%M%S")
    md = report.to_markdown(items, t["description"] or t["name"], meta_lines(hours, raw, tweets, items))
    save_run(out_dir, raw, items, md)

    result = out_dir / "results.md"
    if args.summarize:
        if not t["prompt"]:
            log(f"主题 {t['name']} 没有 prompt，跳过总结")
        else:
            result = summarize(t["summarize_command"], t["prompt"], md, out_dir) or result
    log(f"输出目录: {out_dir}")
    print(result)


def cmd_article(args):
    client = make_client()
    arts = []
    for tid in args.tweet_ids:
        art = pipeline.parse_article(client.article(tid))
        arts.append({"tweet_id": tid, **art})
    if args.format == "json":
        print(json.dumps(arts, ensure_ascii=False, indent=2))
    else:
        for a in arts:
            print(f"# {a['title'] or '(无标题)'}\n\n推文 ID: {a['tweet_id']}\n\n{a['text']}\n")


def cmd_login(args):
    """默认：等 Chrome 扩展自动推送 cookie；--manual NAME：手动粘贴。"""
    if not args.manual:
        try:
            import twscrape  # noqa: F401
        except ImportError as e:
            raise RuntimeError("未安装 twscrape：make venv（或 uv pip install --python .venv/bin/python twscrape）") from e
        from . import cookie_server

        try:
            saved = cookie_server.serve(timeout=args.timeout, keep=args.keep, log=log)
        except OSError as e:
            raise RuntimeError(f"端口 {cookie_server.port()} 启动失败: {e}") from e
        if not saved:
            raise RuntimeError("超时没收到 cookie：确认扩展已安装、已登录 x.com，再点扩展里的「立即同步」")
        return
    exe = shutil.which("twscrape", path=str(Path(sys.executable).parent)) or shutil.which("twscrape")
    if not exe:
        raise RuntimeError("未安装 twscrape：make venv（或 uv pip install --python .venv/bin/python twscrape）")
    db = tws.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    log(f"账号库: {db}")
    log("从浏览器 x.com → F12 → Application → Cookies 复制 auth_token 和 ct0，")
    log("粘贴格式: auth_token=xxx; ct0=yyy（建议用小号，有封号风险）")
    env = {**os.environ, "TWS_TELEMETRY": "0"}
    rc = subprocess.call([exe, "--db", str(db), "add_cookie", args.manual], env=env)
    if rc == 0:
        subprocess.call([exe, "--db", str(db), "accounts"], env=env)
    else:
        raise RuntimeError(f"twscrape add_cookie 退出码 {rc}")


def cmd_topics(args):
    names = topics.names()
    if not names:
        log(f"还没有预设主题：在 {topics.TOPICS_DIR} 下新建 <name>.toml（至少要有 queries），"
            "或直接 eagle x run <toml 文件路径>。字段说明见 README「X 预设主题」。")
        return
    for name in names:
        t = topics.load(name)
        print(f"{name}\t{t['description']}")


def build_parser():
    p = argparse.ArgumentParser(prog="eagle x", description="搜索 X (Twitter)：twscrape（自己的账号 cookie）或 twitterapi.io")
    p.add_argument(
        "--backend",
        choices=["auto", "twscrape", "twitterapi"],
        help="搜索后端，默认读 EAGLE_X_BACKEND / XSEARCH_BACKEND，再默认 auto（登记过账号就用 twscrape）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="临时搜索任意查询")
    s.add_argument("queries", nargs="+", help="一条或多条 X 高级搜索查询，结果自动去重合并")
    s.add_argument("--hours", type=float, default=24, help="只看过去 N 小时，0 表示不限（默认 24）")
    s.add_argument("--pages", type=int, default=1, help="每条查询最多翻几页，每页约 20 条（默认 1）")
    s.add_argument("--type", default="Latest", choices=["Latest", "Top"], help="按时间或按热门（默认 Latest）")
    s.add_argument("--suffix", default="-is:retweet", help="附加到每条查询末尾，传空串关闭（默认 -is:retweet）")
    s.add_argument("--articles", type=int, default=0, help="为最热的 N 条纯链接推文抓 X 长文正文")
    s.add_argument("--keywords", nargs="*", default=[], help="本地过滤：至少命中一个（不区分大小写）")
    s.add_argument("--exclude", nargs="*", default=[], help="本地过滤：命中任一即丢弃")
    s.add_argument("--min-score", type=int, default=0, help="最低热度（赞 + 2×转推）")
    s.add_argument("--limit", type=int, default=50, help="最多输出条数，0 表示全部（默认 50）")
    s.add_argument("--sort", default="score", choices=["score", "time"])
    s.add_argument("--format", default="md", choices=["md", "json"])
    s.add_argument("--full", action="store_true", help="Markdown 输出不截断正文")
    s.add_argument("--save", help="同时把 raw.json / results.json / results.md 存到该目录")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("run", help="运行预设主题（eagle_eye/channels/x/topics/），结果写入 runs/x/<主题>/<时间>/，stdout 打印结果文件路径")
    r.add_argument("topic", help="主题名（eagle_eye/channels/x/topics/<name>.toml）或 toml 文件路径")
    r.add_argument("--hours", type=float, help="覆盖主题的 window_hours")
    r.add_argument("--from-raw", help="复用已有 raw.json，不调用搜索接口（调过滤规则用）")
    r.add_argument("--no-articles", action="store_true", help="不抓长文")
    r.add_argument("--summarize", action="store_true", help="用主题里的 prompt 调 claude -p 生成 digest.md")
    r.add_argument("-o", "--out", help="输出目录")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("article", help="抓取 X 长文正文")
    a.add_argument("tweet_ids", nargs="+")
    a.add_argument("--format", default="md", choices=["md", "json"])
    a.set_defaults(func=cmd_article)

    lg = sub.add_parser("login", help="导入 X 账号 cookie 供 twscrape 后端使用（默认等 Chrome 扩展推送）")
    lg.add_argument("--manual", metavar="NAME", help="不用扩展，手动粘贴 cookie，NAME 为本地账号名")
    lg.add_argument("--timeout", type=float, default=300, help="等待扩展推送的秒数（默认 300）")
    lg.add_argument("--keep", action="store_true", help="一直运行，cookie 变化时持续同步（Ctrl+C 退出）")
    lg.set_defaults(func=cmd_login)

    t = sub.add_parser("topics", help="列出预设主题")
    t.set_defaults(func=cmd_topics)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.backend:
        os.environ["EAGLE_X_BACKEND"] = args.backend
    try:
        args.func(args)
    except RuntimeError as e:
        log(f"错误: {e}")
        return 1
    return 0
