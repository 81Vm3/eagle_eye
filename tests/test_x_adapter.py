"""X 渠道适配：slim → Item 映射、Query → xsearch 参数、错误码映射。不访问网络。"""

import os
import unittest
from unittest import mock

from eagle_eye.channels.x import api, pipeline
from eagle_eye.channels.x.adapter import XChannel, to_item
from eagle_eye.channels.x.api import XError
from eagle_eye.core.models import ChannelError, Query


def tweet(id, text="hello", likes=0, rts=0, user="alice", created="Tue Oct 27 14:09:26 +0000 2026", **kw):
    return {
        "id": id, "url": f"https://x.com/{user}/status/{id}", "text": text, "likeCount": likes,
        "retweetCount": rts, "replyCount": 2, "viewCount": 100, "lang": "en", "createdAt": created,
        "author": {"userName": user, "name": user.upper(), "followers": 1234}, **kw,
    }


class FakeClient:
    def __init__(self, pages=None, error=None):
        self.pages = pages or {"": {"tweets": [], "has_next_page": False}}
        self.error = error
        self.calls = []
        self.closed = False

    def search(self, query, cursor="", query_type="Latest"):
        self.calls.append((query, cursor, query_type))
        if self.error:
            raise self.error
        return self.pages[cursor]

    def article(self, tid):
        return {"article": {"title": "长文", "contents": [{"text": "正文"}]}}

    def close(self):
        self.closed = True


def channel(client):
    return XChannel("x", client_factory=lambda *a: client, log=lambda m: None)


class ToItemTest(unittest.TestCase):
    def test_mapping(self):
        t = tweet("9", likes=3, rts=1, isReply=True)
        t["_hits"] = ["hello"]
        t["_article"] = {"title": "标题", "text": "正文"}
        it = to_item(pipeline.slim(t))
        d = it.to_dict()
        self.assertEqual((d["channel"], d["id"], d["url"]), ("x", "9", "https://x.com/alice/status/9"))
        self.assertEqual(d["author"], {"id": "alice", "name": "ALICE", "url": "https://x.com/alice"})
        self.assertEqual(d["created_at"], "2026-10-27T14:09:26Z")
        self.assertEqual(d["metrics"], {"likes": 3, "reposts": 1, "comments": 2, "views": 100, "collects": None})
        self.assertEqual(d["score"], 5)
        self.assertEqual(d["title"], "标题")
        self.assertEqual(d["extra"], {"followers": 1234, "isReply": True, "article": {"title": "标题", "text": "正文"}, "hits": ["hello"]})

    def test_missing_fields(self):
        it = to_item({"id": "1", "url": None, "user": None, "createdAt": "garbage"})
        self.assertEqual((it.url, it.created_at, it.author.url), ("https://x.com/i/status/1", None, ""))


class XChannelSearchTest(unittest.TestCase):
    def pages(self, *tweets):
        return {"": {"tweets": list(tweets), "has_next_page": True, "next_cursor": "1"},
                "1": {"tweets": [tweet("p2", likes=50)], "has_next_page": False}}

    def test_relevance_uses_top_and_keeps_order(self):
        c = FakeClient(self.pages(tweet("1", likes=1), tweet("2", likes=9)))
        res = channel(c).search(Query("Claude Code lang:zh", limit=2))
        self.assertEqual(c.calls, [("Claude Code lang:zh -is:retweet", "", "Top")])
        self.assertEqual([i.id for i in res.items], ["1", "2"])
        self.assertTrue(c.closed)
        self.assertIn("type=Top", res.meta["notes"])

    def test_score_sort_pages_and_hours(self):
        c = FakeClient(self.pages(tweet("1", likes=1), tweet("2", likes=9)))
        with mock.patch("time.time", return_value=10000.0):
            res = channel(c).search(Query("q", limit=0, since_hours=1, sort="score", options={"pages": 2, "suffix": ""}))
        self.assertEqual(c.calls[0], ("q since_time:6400", "", "Latest"))
        self.assertEqual(len(c.calls), 2)
        self.assertEqual([i.id for i in res.items], ["p2", "2", "1"])
        self.assertEqual(res.meta["query"], "q since_time:6400")

    def test_time_sort_and_local_filters(self):
        c = FakeClient({"": {"tweets": [
            tweet("old", "开源 A", created="Mon Oct 26 10:00:00 +0000 2026"),
            tweet("new", "开源 B", created="Tue Oct 27 10:00:00 +0000 2026"),
            tweet("spam", "开源 spam"),
            tweet("off", "天气"),
        ], "has_next_page": False}})
        res = channel(c).search(Query("q", sort="time", options={"keywords": "开源,教程", "exclude": ["spam"], "type": "Top", "zzz": 1}))
        self.assertEqual([i.id for i in res.items], ["new", "old"])
        self.assertEqual(c.calls[0][2], "Top")
        self.assertEqual(res.items[0].extra["hits"], ["开源"])
        self.assertEqual(res.meta["ignored"], ["options.zzz"])

    def test_articles(self):
        c = FakeClient({"": {"tweets": [tweet("1", "https://t.co/abc", likes=5)], "has_next_page": False}})
        res = channel(c).search(Query("q", options={"articles": 1}))
        self.assertEqual(res.items[0].title, "长文")
        self.assertEqual(res.items[0].extra["article"], {"title": "长文", "text": "正文"})

    def test_bad_options(self):
        for opts in ({"type": "Hot"}, {"pages": "x"}, {"pages": 0}, {"keywords": 5}):
            with self.assertRaises(ChannelError) as cm:
                channel(FakeClient()).search(Query("q", options=opts))
            self.assertEqual(cm.exception.code, "bad_query")

    def test_error_mapping_when_all_queries_fail(self):
        for err, code in [
            (XError("HTTP 402", api.http_code(402)), "rate_limited"),
            (XError("HTTP 401", api.http_code(401)), "auth_required"),
            (XError("no acc", "rate_limited", 900), "rate_limited"),
            (RuntimeError("weird"), "internal"),
        ]:
            with self.assertRaises(ChannelError) as cm:
                channel(FakeClient(error=err)).search(Query("q"))
            self.assertEqual(cm.exception.code, code)
            self.assertEqual(cm.exception.channel, "x")

    def test_client_construction_errors(self):
        def factory(*a):
            raise XError("缺少 key", "auth_required")
        with self.assertRaises(ChannelError) as cm:
            XChannel("x", client_factory=factory, log=lambda m: None).search(Query("q"))
        self.assertEqual(cm.exception.code, "auth_required")

    def test_backend_option_passed_to_factory(self):
        got = []
        c = FakeClient()
        XChannel("x", client_factory=lambda *a: got.append(a) or c, log=lambda m: None).search(Query("q", options={"backend": "twitterapi"}))
        self.assertEqual(got, [("twitterapi",)])

    def test_describe(self):
        d = XChannel("x").describe()
        self.assertEqual(d.name, "x")
        self.assertIn("pages", d.options)


class EnvAliasTest(unittest.TestCase):
    # twscrape 账号库默认路径（旧库存在才沿用）见 tests/test_paths.py MiscTest.test_twscrape_db_default
    def test_eagle_alias_wins_and_xsearch_still_works(self):
        from eagle_eye.channels.x import cookie_server, tws
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("EAGLE_", "XSEARCH_"))}
        with mock.patch.dict(os.environ, {**clean, "XSEARCH_TWS_DB": "/tmp/a.db", "XSEARCH_COOKIE_PORT": "1234"}, clear=True):
            self.assertEqual(str(tws.db_path()), "/tmp/a.db")
            self.assertEqual(cookie_server.port(), 1234)
            with mock.patch.dict(os.environ, {"EAGLE_X_TWS_DB": "/tmp/b.db"}):
                self.assertEqual(str(tws.db_path()), "/tmp/b.db")

if __name__ == "__main__":
    unittest.main()
