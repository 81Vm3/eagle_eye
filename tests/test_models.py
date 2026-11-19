"""models：协议 JSON 往返、校验、向前兼容。"""

import unittest

from eagle_eye.core.models import (
    BAD_QUERY,
    INTERNAL,
    Author,
    ChannelError,
    Descriptor,
    Item,
    Metrics,
    Query,
    SearchResult,
    parse_rfc3339,
    to_rfc3339,
)


def full_item():
    return Item(
        channel="xhs", id="65f0", url="https://www.xiaohongshu.com/explore/65f0?xsec_token=abc",
        title="标题", text="正文", author=Author("u1", "名字", "https://x/u1"),
        created_at="2026-11-19T08:00:00Z", metrics=Metrics(12, 0, 3, None, 5), score=17, lang="zh",
        extra={"tags": ["a"]},
    )


class RoundTripTest(unittest.TestCase):
    def test_item_round_trip(self):
        it = full_item()
        d = it.to_dict()
        self.assertEqual(set(d), {"channel", "id", "url", "title", "text", "author", "created_at", "metrics", "score", "lang", "extra"})
        self.assertEqual(d["metrics"], {"likes": 12, "reposts": 0, "comments": 3, "views": None, "collects": 5})
        self.assertEqual(Item.from_dict(d), it)

    def test_item_minimal_and_unknown_fields(self):
        it = Item.from_dict({"channel": "x", "id": 5, "url": "https://x.com/a/status/5", "future": {"a": 1}})
        self.assertEqual((it.id, it.title, it.text, it.created_at, it.score, it.extra), ("5", "", "", None, None, {}))
        self.assertEqual(it.metrics, Metrics())
        self.assertEqual(it.author, Author())

    def test_item_coerces_bad_types(self):
        it = Item.from_dict({"channel": "x", "id": "1", "url": "u", "metrics": {"likes": "7", "views": "x"},
                             "score": "3.5", "extra": [], "author": None, "created_at": 123})
        self.assertEqual((it.metrics.likes, it.metrics.views, it.score, it.extra, it.created_at), (7, None, 3.5, {}, None))

    def test_item_required_fields(self):
        for bad in [{}, {"channel": "x", "id": "1"}, {"channel": "x", "url": "u"}, "str"]:
            with self.assertRaises(ValueError):
                Item.from_dict(bad)

    def test_query_round_trip_and_defaults(self):
        q = Query("Claude Code 教程", 5, 24, "time", {"detail": 3})
        d = q.to_dict()
        self.assertEqual(d, {"protocol": 1, "text": "Claude Code 教程", "limit": 5, "since_hours": 24, "sort": "time", "options": {"detail": 3}})
        self.assertEqual(Query.from_dict(d), q)
        q2 = Query.from_dict({"text": "a", "since_hours": 0, "extra": 1})
        self.assertEqual((q2.limit, q2.since_hours, q2.sort, q2.options), (20, None, "relevance", {}))

    def test_query_validation(self):
        for bad in [{"text": ""}, {"text": "a", "sort": "hot"}, {"text": "a", "limit": -1}, {"text": "a", "options": []}, "x"]:
            with self.assertRaises(ChannelError) as cm:
                Query.from_dict(bad)
            self.assertEqual(cm.exception.code, BAD_QUERY)

    def test_descriptor_round_trip(self):
        d = Descriptor("xhs", "小红书", "desc", ["search"], "关键词", "慢", {"detail": {"type": "int", "default": 3}})
        self.assertEqual(Descriptor.from_dict({**d.to_dict(), "new": 1}), d)
        with self.assertRaises(ValueError):
            Descriptor.from_dict({"title": "no name"})

    def test_error_round_trip(self):
        e = ChannelError("cooldown", "冷却", 21600)
        self.assertEqual(e.to_dict(), {"code": "cooldown", "message": "冷却", "retry_after_seconds": 21600})
        e2 = ChannelError.from_dict(e.to_dict(), channel="xhs")
        self.assertEqual((e2.code, e2.message, e2.retry_after_seconds, e2.channel), ("cooldown", "冷却", 21600, "xhs"))

    def test_unknown_error_code_becomes_internal(self):
        e = ChannelError.from_dict({"code": "weird", "message": "m"})
        self.assertEqual(e.code, INTERNAL)
        self.assertIn("weird", e.message)
        self.assertEqual(ChannelError.from_dict("oops").code, INTERNAL)

    def test_search_result(self):
        r = SearchResult([full_item()], {"query": "q"})
        back = SearchResult.from_dict(r.to_dict(), channel="xhs")
        self.assertEqual(back.items, r.items)
        bad = SearchResult.from_dict({"items": [full_item().to_dict(), {"id": "x"}], "meta": None}, channel="renamed")
        self.assertEqual(bad.meta, {"invalid_items": 1})
        self.assertEqual(bad.items[0].channel, "renamed")
        with self.assertRaises(ChannelError) as cm:
            SearchResult.from_dict({"protocol": 1, "error": {"code": "busy", "message": "m"}}, channel="xhs")
        self.assertEqual((cm.exception.code, cm.exception.channel), ("busy", "xhs"))

    def test_time_helpers(self):
        dt = parse_rfc3339("2026-11-19T08:00:00Z")
        self.assertEqual(to_rfc3339(dt), "2026-11-19T08:00:00Z")
        self.assertEqual(to_rfc3339(parse_rfc3339("2026-11-19T16:00:00+08:00")), "2026-11-19T08:00:00Z")
        self.assertIsNone(parse_rfc3339("garbage"))
        self.assertIsNone(to_rfc3339(None))


if __name__ == "__main__":
    unittest.main()
