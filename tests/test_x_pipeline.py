import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eagle_eye.channels.x import pipeline, report, topics


def tweet(id, text, likes=0, rts=0, user="u", **kw):
    return {
        "id": id,
        "url": f"https://x.com/{user}/status/{id}",
        "text": text,
        "likeCount": likes,
        "retweetCount": rts,
        "createdAt": "Tue Oct 27 14:09:26 +0000 2026",
        "author": {"userName": user, "followers": 1234},
        **kw,
    }


class PipelineTest(unittest.TestCase):
    def test_build_query(self):
        self.assertEqual(
            pipeline.build_query("开源 AI", 100, "-is:retweet"),
            "开源 AI since_time:100 -is:retweet",
        )
        self.assertEqual(pipeline.build_query("x", 1), "x since_time:1")
        self.assertEqual(pipeline.build_query("from:a", None, ""), "from:a")

    def test_dedupe_keeps_first(self):
        raw = {"a": [tweet("1", "first")], "b": [tweet("1", "dup"), tweet("2", "other")]}
        got = pipeline.dedupe(raw)
        self.assertEqual([t["id"] for t in got], ["1", "2"])
        self.assertEqual(got[0]["text"], "first")

    def test_is_link_only(self):
        self.assertTrue(pipeline.is_link_only(tweet("1", "https://t.co/abc")))
        self.assertTrue(pipeline.is_link_only(tweet("1", " https://t.co/a https://t.co/b ")))
        self.assertFalse(pipeline.is_link_only(tweet("1", "看这里 https://t.co/abc")))

    def test_parse_article(self):
        resp = {"article": {"title": "T", "contents": [{"text": "a"}, {"text": "b"}, "x"]}}
        self.assertEqual(pipeline.parse_article(resp), {"title": "T", "text": "a b"})
        self.assertEqual(
            pipeline.parse_article({"article": {"title": "", "preview_text": "p"}}),
            {"title": "", "text": "p"},
        )

    def test_select_filters_and_sorts(self):
        tweets = [
            tweet("1", "今天发布了一个开源编辑器", likes=1),
            tweet("2", "open source spam bot", likes=50),
            tweet("3", "今天天气不错", likes=100),
            tweet("4", "Rust open source release", likes=10, rts=5),
            tweet("5", "https://t.co/x", likes=30, _article={"title": "入门教程", "text": ""}),
        ]
        got = pipeline.select(tweets, ["开源", "open source", "教程"], ["spam"])
        self.assertEqual([t["id"] for t in got], ["5", "4", "1"])
        self.assertEqual(got[0]["_hits"], ["教程"])

    def test_select_min_score_and_limit(self):
        tweets = [tweet(str(i), "开源", likes=i) for i in range(5)]
        got = pipeline.select(tweets, ["开源"], [], min_score=2, limit=2)
        self.assertEqual([t["id"] for t in got], ["4", "3"])

    def test_select_no_keywords_keeps_all_and_sorts_by_time(self):
        tweets = [
            tweet("old", "a", likes=9, createdAt="Mon Oct 26 10:00:00 +0000 2026"),
            tweet("new", "b", likes=1, createdAt="Tue Oct 27 10:00:00 +0000 2026"),
            tweet("bad", "c", createdAt="garbage"),
        ]
        got = pipeline.select(tweets, sort="time")
        self.assertEqual([t["id"] for t in got], ["new", "old", "bad"])


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search(self, query, cursor="", query_type="Latest"):
        self.calls.append((query, cursor, query_type))
        return self.pages[cursor]


class SearchAllTest(unittest.TestCase):
    def test_paginates_until_no_next_page(self):
        client = FakeClient({
            "": {"tweets": [tweet("1", "a")], "has_next_page": True, "next_cursor": "c1"},
            "c1": {"tweets": [tweet("2", "b")], "has_next_page": False, "next_cursor": ""},
        })
        raw = pipeline.search_all(client, ["q"], 100, pages=5, suffix="-is:retweet", query_type="Top", log=lambda m: None)
        self.assertEqual([t["id"] for t in raw["q"]], ["1", "2"])
        self.assertEqual(client.calls, [("q since_time:100 -is:retweet", "", "Top"), ("q since_time:100 -is:retweet", "c1", "Top")])


SAMPLE_TOPIC = '''
description = "示例主题"
queries = ["example lang:en", "示例"]
prompt = "总结"
'''


class TopicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # 包内主题目录和 EAGLE_HOME 下的用户主题目录都换成临时目录（不读真实 $EAGLE_HOME/x/topics）
        for patcher in (mock.patch.object(topics, "TOPICS_DIR", self.dir),
                        mock.patch.dict(os.environ, {"EAGLE_HOME": str(self.dir / "eagle_home")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_topic_loads_with_defaults(self):
        (self.dir / "sample.toml").write_text(SAMPLE_TOPIC)
        self.assertEqual(topics.names(), ["sample"])
        t = topics.load("sample")
        self.assertEqual(t["name"], "sample")
        self.assertEqual(t["queries"], ["example lang:en", "示例"])
        self.assertTrue(t["prompt"].strip())
        self.assertEqual(t["window_hours"], 24)
        self.assertEqual(t["summarize_command"], ["claude", "-p"])

    def test_load_by_path(self):
        p = self.dir / "elsewhere.toml"
        p.write_text(SAMPLE_TOPIC)
        self.assertEqual(topics.load(str(p))["name"], "elsewhere")

    def test_empty_or_missing_dir(self):
        self.assertEqual(topics.names(), [])
        with mock.patch.object(topics, "TOPICS_DIR", self.dir / "nope"):
            self.assertEqual(topics.names(), [])

    def test_missing_topic(self):
        with self.assertRaises(RuntimeError):
            topics.load("does-not-exist")

    def test_missing_queries(self):
        (self.dir / "bad.toml").write_text('description = "x"\n')
        with self.assertRaises(RuntimeError):
            topics.load("bad")


class ReportTest(unittest.TestCase):
    def test_to_markdown(self):
        t = tweet("9", "line1\n\nline2 开源", likes=3, rts=1, user="alice", isReply=True)
        t["_hits"] = ["开源"]
        t["_article"] = {"title": "标题", "text": "正文"}
        md = report.to_markdown([pipeline.slim(t)], "Title", ["meta"])
        self.assertIn("# Title", md)
        self.assertIn("- meta", md)
        self.assertIn("## 1. @alice · 3♥ 1RT · 1,234 粉 · 2026-10-27 14:09 UTC · 回复", md)
        self.assertIn("> line1 line2 开源", md)
        self.assertIn("命中: 开源", md)
        self.assertIn("**长文《标题》** 正文", md)

    def test_fmt_time_fallback(self):
        self.assertEqual(report.fmt_time("bad"), "bad")
        self.assertEqual(report.fmt_time(None), "?")


if __name__ == "__main__":
    unittest.main()
