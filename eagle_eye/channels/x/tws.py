"""twscrape 后端：用自己的 X 账号 cookie 调 X 网页版 GraphQL，不消耗 twitterapi.io 额度。

对外接口与 api.Client 一致（search / article），返回的推文转换成 twitterapi.io 的字段形状，
所以 pipeline、raw.json、--from-raw 都不用改。

账号登记（一次性，cookie 至少含 auth_token 和 ct0，建议用小号）：
    bin/eagle x login            # 配合 chrome-extension/ 自动导入
    bin/eagle x login --manual 名字   # 手动粘贴
"""

import asyncio
import atexit
import os
import time
from pathlib import Path

from .api import XError
from .settings import env

# twscrape 默认会往 PostHog 发匿名统计，这里关掉（必须在 import twscrape 之前设置）
os.environ.setdefault("TWS_TELEMETRY", "0")

DEFAULT_DB = Path.home() / ".config" / "xsearch" / "twscrape.db"


def db_path():
    return Path(env("TWS_DB") or DEFAULT_DB)


def available():
    """装了 twscrape 且账号库存在时返回 True。"""
    try:
        import twscrape  # noqa: F401
    except ImportError:
        return False
    return db_path().exists()


async def _await(aw):
    # asyncio.Runner.run 只接受协程，anext()/aclose() 返回的是普通 awaitable
    return await aw


def to_dict(t):
    """twscrape Tweet → twitterapi.io 风格的 dict。"""
    u = t.user
    return {
        "id": t.id_str,
        "url": t.url,
        "createdAt": t.date.strftime("%a %b %d %H:%M:%S %z %Y"),
        "author": {
            "userName": u.username,
            "name": u.displayname,
            "followers": u.followersCount,
        },
        "likeCount": t.likeCount or 0,
        "retweetCount": t.retweetCount or 0,
        "replyCount": t.replyCount or 0,
        "quoteCount": t.quoteCount or 0,
        "viewCount": t.viewCount,
        "lang": t.lang,
        "isReply": t.inReplyToTweetId is not None,
        "text": t.rawContent,
    }


def article_to_dict(a):
    """twscrape Article → api.Client.article() 的返回形状，供 pipeline.parse_article 使用。"""
    if a is None:
        return {}
    blocks = a.contentState.blocks if a.contentState else []
    return {
        "article": {
            "title": a.title,
            "preview_text": a.previewText,
            "contents": [{"text": b.text} for b in blocks],
        }
    }


class TwscrapeClient:
    def __init__(self, db=None, interval=None, wait_timeout=None):
        try:
            from twscrape import API
        except ImportError as e:
            raise XError("未安装 twscrape：make venv（或 uv pip install --python .venv/bin/python twscrape）", "unavailable") from e
        db = Path(db or db_path())
        if not db.exists():
            raise XError(f"twscrape 账号库不存在: {db}，需要人工运行 bin/eagle x login（配合 chrome-extension/ 自动导入）", "auth_required")
        if interval is None:
            interval = float(env("TWS_INTERVAL", "2"))
        if wait_timeout is None:
            wait_timeout = float(env("TWS_WAIT", "60"))
        self.interval = interval
        self._last = 0.0
        self._runner = asyncio.Runner()
        self.api = API(str(db), raise_when_no_account=True, wait_timeout=wait_timeout)
        self._gen = None
        self._seen = set()
        atexit.register(self.close)

    def _run(self, coro):
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            return self._runner.run(coro)
        except Exception as e:  # twscrape 的 NoAccountError、网络错误等统一转成 XError（RuntimeError 子类）
            raise self._classify(e) from e
        finally:
            self._last = time.monotonic()

    def _classify(self, e):
        """把 twscrape 异常映射成带错误码的 XError。"""
        msg = f"twscrape: {type(e).__name__}: {e}"
        if type(e).__name__ != "NoAccountError":
            return XError(msg, "internal")
        # 没有任何 active 账号（cookie 失效）→ auth_required；有但都被锁/限速 → rate_limited
        nat = None
        try:
            nat = self._runner.run(self.api.pool.next_available_at("SearchTimeline"))
        except Exception:
            pass
        if not nat:
            return XError(msg + "（没有可用账号，cookie 可能已失效：需要人工运行 bin/eagle x login）", "auth_required")
        return XError(msg + f"（所有账号限速中，预计 {nat} 可用）", "rate_limited", 900)

    def _close_gen(self):
        if self._gen is not None:
            gen, self._gen = self._gen, None
            try:
                self._runner.run(_await(gen.aclose()))  # 释放账号锁
            except Exception:
                pass

    def close(self):
        self._close_gen()
        try:
            self._runner.close()
        except Exception:
            pass

    def search(self, query, cursor="", query_type="Latest"):
        """返回一页结果。twscrape 内部自己维护翻页游标，这里用生成器续页，cursor 只是个序号。"""
        from twscrape.models import parse_tweets

        if not cursor:
            self._close_gen()
            self._seen = set()
            self._gen = self.api.search_raw(query, kv={"product": query_type})
        if self._gen is None:
            return {"tweets": [], "has_next_page": False}
        rep = self._run(_await(anext(self._gen, None)))
        if rep is None:
            self._close_gen()
            return {"tweets": [], "has_next_page": False}
        tweets = [to_dict(t) for t in parse_tweets(rep, seen_ids=self._seen)]
        page = int(cursor or 0) + 1
        return {"tweets": tweets, "has_next_page": True, "next_cursor": str(page)}

    def article(self, tweet_id):
        t = self._run(self.api.tweet_details(int(tweet_id)))
        if t is None:
            raise XError(f"推文 {tweet_id} 不存在或不可见", "bad_query")
        return article_to_dict(t.article)
