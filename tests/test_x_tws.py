"""twscrape 后端适配层测试：不需要装 twscrape，也不访问网络。"""

import asyncio
import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from eagle_eye.channels.x import pipeline, tws


def fake_tweet(i, reply=False):
    user = NS(username=f"u{i}", displayname=f"U{i}", followersCount=100 + i)
    return NS(
        id_str=str(i),
        url=f"https://x.com/u{i}/status/{i}",
        date=datetime(2026, 11, 16, 8, 0, i, tzinfo=timezone.utc),
        user=user,
        likeCount=i,
        retweetCount=1,
        replyCount=0,
        quoteCount=0,
        viewCount=None,
        lang="zh",
        inReplyToTweetId=1 if reply else None,
        rawContent=f"text {i}",
    )


class FakeAPI:
    def __init__(self, pages):
        self.pages = pages  # list[list[tweet]]
        self.closed = 0
        self.calls = []

    def search_raw(self, query, kv=None):
        self.calls.append((query, kv))
        api = self

        async def gen():
            try:
                for p in api.pages:
                    yield p
            finally:
                api.closed += 1

        return gen()

    async def tweet_details(self, twid):
        art = NS(
            title="标题",
            previewText="预览",
            contentState=NS(blocks=[NS(text="第一段"), NS(text="第二段")]),
        )
        return NS(article=art)


def make_client(api):
    c = tws.TwscrapeClient.__new__(tws.TwscrapeClient)
    c.interval = 0
    c._last = 0.0
    c._runner = asyncio.Runner()
    c.api = api
    c._gen = None
    c._seen = set()
    return c


class TwscrapeClientTest(unittest.TestCase):
    def setUp(self):
        # 用假的 twscrape.models.parse_tweets：一页就是一个 tweet 列表
        mod = types.ModuleType("twscrape.models")
        mod.parse_tweets = lambda rep, seen_ids=None: iter(rep)
        pkg = types.ModuleType("twscrape")
        pkg.models = mod
        self._saved = {k: sys.modules.get(k) for k in ("twscrape", "twscrape.models")}
        sys.modules.update({"twscrape": pkg, "twscrape.models": mod})

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_to_dict_shape_matches_pipeline(self):
        d = tws.to_dict(fake_tweet(5, reply=True))
        self.assertEqual(d["id"], "5")
        self.assertEqual(d["author"], {"userName": "u5", "name": "U5", "followers": 105})
        self.assertTrue(d["isReply"])
        self.assertIsNotNone(pipeline.parse_time(d["createdAt"]))
        self.assertEqual(pipeline.score(d), 5 + 2)
        self.assertEqual(pipeline.slim(d)["user"], "u5")

    def test_paging_via_search_all(self):
        api = FakeAPI([[fake_tweet(1), fake_tweet(2)], [fake_tweet(3)], [fake_tweet(4)]])
        c = make_client(api)
        raw = pipeline.search_all(c, ["q1", "q2"], since=None, pages=2, query_type="Top", log=lambda m: None)
        self.assertEqual([t["id"] for t in raw["q1"]], ["1", "2", "3"])
        self.assertEqual(api.calls, [("q1", {"product": "Top"}), ("q2", {"product": "Top"})])
        # 第二条查询开始时关闭了第一条的生成器（释放账号锁）
        self.assertEqual(api.closed, 1)
        c.close()
        self.assertEqual(api.closed, 2)

    def test_stops_when_exhausted(self):
        api = FakeAPI([[fake_tweet(1)]])
        c = make_client(api)
        raw = pipeline.search_all(c, ["q"], since=None, pages=5, log=lambda m: None)
        self.assertEqual([t["id"] for t in raw["q"]], ["1"])
        c.close()

    def test_article(self):
        c = make_client(FakeAPI([]))
        art = pipeline.parse_article(c.article("123"))
        self.assertEqual(art, {"title": "标题", "text": "第一段 第二段"})
        c.close()


class NoAccountError(Exception):
    pass


class ClassifyTest(unittest.TestCase):
    def client(self, nat):
        async def next_available_at(queue):
            return nat

        c = make_client(NS(pool=NS(next_available_at=next_available_at)))
        return c

    def test_no_active_account_is_auth_required(self):
        c = self.client(None)
        e = c._classify(NoAccountError("No account available for queue SearchTimeline"))
        self.assertEqual(e.code, "auth_required")
        self.assertIn("eagle x login", str(e))
        c.close()

    def test_all_locked_is_rate_limited(self):
        c = self.client("12:00:00")
        e = c._classify(NoAccountError("No account"))
        self.assertEqual((e.code, e.retry_after_seconds), ("rate_limited", 900))
        self.assertIsInstance(e, RuntimeError)
        c.close()

    def test_other_errors_are_internal(self):
        c = self.client(None)
        self.assertEqual(c._classify(ValueError("x")).code, "internal")
        c.close()


if __name__ == "__main__":
    unittest.main()
