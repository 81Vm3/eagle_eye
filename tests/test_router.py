"""router：并发、部分失败、去重、排序、limit、带前缀的 options。"""

import threading
import time
import unittest

from eagle_eye.core.channel import Channel
from eagle_eye.core.models import ChannelError, Descriptor, Item, Metrics, Query, SearchResult
from eagle_eye.core.router import fan_out, merge, split_options, url_key


def item(ch, id, url=None, score=None, created=None):
    return Item(ch, str(id), url or f"https://{ch}.example/{id}", score=score, created_at=created, metrics=Metrics())


class FakeChannel(Channel):
    def __init__(self, name, items=(), delay=0.0, error=None):
        super().__init__(name)
        self.items, self.delay, self.error = list(items), delay, error
        self.queries = []

    def describe(self):
        return Descriptor(self.name)

    def search(self, query):
        self.queries.append(query)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return SearchResult(list(self.items), {"notes": "ok"})


def quiet(_):
    pass


class FanOutTest(unittest.TestCase):
    def test_runs_concurrently(self):
        chans = [FakeChannel(f"c{i}", [item(f"c{i}", 1)], delay=0.4) for i in range(3)]
        t0 = time.monotonic()
        agg = fan_out(chans, Query("q"), log=quiet)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(len(agg.items), 3)
        self.assertEqual(agg.errors, {})
        self.assertFalse(agg.all_failed)

    def test_partial_failure(self):
        chans = [
            FakeChannel("ok", [item("ok", 1)]),
            FakeChannel("cool", error=ChannelError("cooldown", "冷却", 60)),
            FakeChannel("bug", error=ZeroDivisionError("boom")),
        ]
        agg = fan_out(chans, Query("q"), log=quiet)
        self.assertEqual([i.id for i in agg.items], ["1"])
        self.assertEqual(agg.errors["cool"].code, "cooldown")
        self.assertEqual(agg.errors["cool"].channel, "cool")
        self.assertEqual(agg.errors["bug"].code, "internal")
        self.assertIn("ZeroDivisionError", agg.errors["bug"].message)
        self.assertEqual(agg.ok_channels, ["ok"])
        d = agg.to_dict()
        self.assertEqual(d["errors"]["cool"]["retry_after_seconds"], 60)
        self.assertEqual(d["meta"]["ok"]["count"], 1)

    def test_all_failed(self):
        agg = fan_out([FakeChannel("a", error=ChannelError("busy", "m"))], Query("q"), log=quiet)
        self.assertTrue(agg.all_failed)

    def test_options_split_per_channel(self):
        a, b = FakeChannel("x"), FakeChannel("xhs")
        fan_out([a, b], Query("q", options={"common": 1, "x.pages": 2, "xhs.detail": 0, "x.common": 9}), log=quiet)
        self.assertEqual(a.queries[0].options, {"common": 9, "pages": 2})
        self.assertEqual(b.queries[0].options, {"common": 1, "detail": 0})
        self.assertEqual(split_options({"other.k": 1}, ["x"]), {"x": {"other.k": 1}})

    def test_limit_and_sort_score(self):
        chans = [FakeChannel("a", [item("a", i, score=i) for i in range(5)]),
                 FakeChannel("b", [item("b", i, score=i + 0.5) for i in range(5)])]
        agg = fan_out(chans, Query("q", limit=3, sort="score"), log=quiet)
        self.assertEqual([(i.channel, i.score) for i in agg.items], [("b", 4.5), ("a", 4), ("b", 3.5)])

    def test_thread_safety_of_independent_channels(self):
        seen = set()
        lock = threading.Lock()

        class Rec(FakeChannel):
            def search(self, query):
                with lock:
                    seen.add(threading.get_ident())
                time.sleep(0.2)
                return SearchResult([], {})

        fan_out([Rec("a"), Rec("b")], Query("q"), log=quiet)
        self.assertEqual(len(seen), 2)


class MergeTest(unittest.TestCase):
    def test_relevance_interleaves(self):
        got = merge([("a", [item("a", 1), item("a", 2), item("a", 3)]), ("b", [item("b", 1)])], "relevance", 0)
        self.assertEqual([(i.channel, i.id) for i in got], [("a", "1"), ("b", "1"), ("a", "2"), ("a", "3")])

    def test_time_sort_unknown_last(self):
        got = merge([("a", [item("a", 1, created="2026-11-01T00:00:00Z"), item("a", 2)]),
                     ("b", [item("b", 1, created="2026-11-02T00:00:00+08:00")])], "time", 0)
        self.assertEqual([(i.channel, i.id) for i in got], [("b", "1"), ("a", "1"), ("a", "2")])

    def test_score_sort_none_last(self):
        got = merge([("a", [item("a", 1, score=None), item("a", 2, score=0)])], "score", 0)
        self.assertEqual([i.id for i in got], ["2", "1"])

    def test_cross_channel_url_dedupe(self):
        got = merge([
            ("x", [item("x", 1, url="https://x.com/u/status/1")]),
            ("y", [item("y", 9, url="http://twitter.com/u/status/1/"),
                   item("y", 2, url="https://www.xiaohongshu.com/explore/abc?xsec_token=T1")]),
            ("z", [item("z", 3, url="https://xiaohongshu.com/explore/abc?xsec_token=T2&utm_source=s#f")]),
        ], "relevance", 0)
        self.assertEqual([(i.channel, i.id) for i in got], [("x", "1"), ("z", "3")])  # 交错顺序下 z3 先于 y2

    def test_same_channel_id_dedupe(self):
        got = merge([("a", [item("a", 1, url="https://e.com/1"), item("a", 1, url="https://e.com/other")])], "relevance", 0)
        self.assertEqual(len(got), 1)

    def test_url_key_keeps_meaningful_params(self):
        self.assertNotEqual(url_key("https://e.com/p?id=1"), url_key("https://e.com/p?id=2"))
        self.assertEqual(url_key("https://e.com/p?b=2&a=1"), url_key("https://E.com/p/?a=1&b=2"))


if __name__ == "__main__":
    unittest.main()
