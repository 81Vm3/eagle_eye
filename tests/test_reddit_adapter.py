"""Reddit adapter：字段映射、路由、补全、缓存、排序/过滤与错误码。不访问网络。"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from eagle_eye.channels.reddit.adapter import RedditChannel, _parse_post_url
from eagle_eye.channels.reddit.cache import make_key
from eagle_eye.channels.reddit.providers import (
    AUTH_REQUIRED,
    INTERNAL,
    RATE_LIMITED,
    UNAVAILABLE,
    RedditProviderError,
    TavilyProvider,
)
from eagle_eye.core.models import ChannelError, Query

NOW = 1_700_000_000.0


def iso(offset=0):
    return datetime.fromtimestamp(NOW + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc1123(offset=0):
    return format_datetime(datetime.fromtimestamp(NOW + offset, timezone.utc), usegmt=True)


def tavily_raw(post_id="abc", subreddit="LocalLLaMA", offset=-3600, **kw):
    data = {
        "title": f"Title {post_id}",
        "url": f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/slug/?utm_source=search",
        "content": f"Chunk {post_id}",
        "published_date": rfc1123(offset),
        "score": 0.87,
    }
    data.update(kw)
    return data


def arctic_raw(post_id="abc", subreddit="LocalLLaMA", offset=-3600, author="alice", score=42, **kw):
    data = {
        "id": post_id,
        "subreddit": subreddit,
        "title": f"Arctic {post_id}",
        "selftext": f"Body {post_id}",
        "author": author,
        "score": score,
        "num_comments": 7,
        "created_utc": NOW + offset,
        "permalink": f"/r/{subreddit}/comments/{post_id}/slug/",
        "url": f"https://example.com/{post_id}",
    }
    data.update(kw)
    return data


class FakeTavily:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def search(self, text, limit=20, subreddit=None, since_hours=None, now=None):
        self.calls.append({
            "text": text,
            "limit": limit,
            "subreddit": subreddit,
            "since_hours": since_hours,
            "now": now() if callable(now) else now,
        })
        if self.error:
            raise self.error
        return list(self.results)


class FakeArctic:
    def __init__(self, results=None, error=None, fetch_results=None, fetch_error=None):
        self.results = list(results or [])
        self.error = error
        self.fetch_results = list(fetch_results or [])
        self.fetch_error = fetch_error
        self.search_calls = []
        self.fetch_calls = []
        self.fetch_timeouts = []

    def search(self, text, subreddit=None, author=None, since_hours=None, limit=20, now=None):
        self.search_calls.append({
            "text": text,
            "subreddit": subreddit,
            "author": author,
            "since_hours": since_hours,
            "limit": limit,
        })
        if self.error:
            raise self.error
        return list(self.results)

    def fetch_by_ids(self, ids, timeout=None):
        self.fetch_calls.append(list(ids))
        self.fetch_timeouts.append(timeout)
        if self.fetch_error:
            raise self.fetch_error
        return list(self.fetch_results)


class TavilyResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RedditAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"EAGLE_HOME": str(self.home)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def make(self, tavily=None, arctic=None, key="test-key", config_text=None, clock=None):
        if config_text is not None:
            (self.home / "reddit").mkdir(parents=True, exist_ok=True)
            (self.home / "reddit" / "config.toml").write_text(config_text, encoding="utf-8")
        if key is None:
            os.environ.pop("EAGLE_REDDIT_TAVILY_API_KEY", None)
        else:
            os.environ["EAGLE_REDDIT_TAVILY_API_KEY"] = key
        return RedditChannel(
            "reddit",
            home=self.home,
            tavily_provider=tavily,
            arctic_provider=arctic,
            clock=clock or (lambda: NOW),
        )

    def test_tavily_mapping_and_non_post_filtering(self):
        ch = self.make(
            tavily=FakeTavily([tavily_raw(), {"url": "https://www.reddit.com/r/LocalLLaMA/"}]),
            arctic=FakeArctic(),
        )
        result = ch.search(Query("q", limit=5))
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual((item.channel, item.id, item.url), (
            "reddit", "abc", "https://www.reddit.com/r/LocalLLaMA/comments/abc/",
        ))
        self.assertEqual(item.title, "Title abc")
        self.assertEqual(item.text, "Chunk abc")
        self.assertEqual(item.created_at, iso(-3600))
        self.assertEqual(item.author.to_dict(), {"id": "", "name": "", "url": ""})
        self.assertIsNone(item.score)
        self.assertEqual(item.metrics.to_dict(), {
            "likes": None, "reposts": None, "comments": None, "views": None, "collects": None,
        })
        self.assertEqual(item.extra["provider"], "tavily")
        self.assertEqual(item.extra["subreddit"], "LocalLLaMA")
        self.assertEqual(item.extra["relevance_score"], 0.87)
        self.assertFalse(item.extra["enriched"])
        self.assertIn("Tavily 丢弃 1 条", result.meta["notes"])
        self.assertFalse(result.meta["degraded"])

    def test_arctic_mapping(self):
        ch = self.make(arctic=FakeArctic([arctic_raw()]), key=None)
        result = ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        item = result.items[0]
        self.assertEqual(item.id, "abc")
        self.assertEqual(item.url, "https://www.reddit.com/r/LocalLLaMA/comments/abc/slug/")
        self.assertEqual(item.text, "Body abc")
        self.assertEqual(item.author.to_dict(), {
            "id": "alice", "name": "alice", "url": "https://www.reddit.com/user/alice/",
        })
        self.assertEqual(item.metrics.likes, 42)
        self.assertEqual(item.metrics.comments, 7)
        self.assertEqual(item.score, 42)
        self.assertEqual(item.created_at, iso(-3600))
        self.assertEqual(item.extra["provider"], "arctic")
        self.assertEqual(item.extra["external_url"], "https://example.com/abc")
        self.assertEqual(result.meta["provider"], "arctic")

    def test_subreddit_and_author_route_to_arctic(self):
        arctic = FakeArctic([arctic_raw()])
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, arctic=arctic, key=None)
        ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        ch.search(Query("q", options={"author": "spez"}))
        self.assertEqual(tavily.calls, [])
        self.assertEqual([c["subreddit"] for c in arctic.search_calls], ["LocalLLaMA", None])
        self.assertEqual([c["author"] for c in arctic.search_calls], [None, "spez"])

    def test_arctic_failure_falls_back_for_subreddit(self):
        tavily = FakeTavily([tavily_raw("good"), tavily_raw("wrong", subreddit="other")])
        arctic = FakeArctic(error=RedditProviderError(UNAVAILABLE, "timeout"))
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in result.items], ["good"])
        self.assertEqual(tavily.calls[0]["subreddit"], "LocalLLaMA")
        self.assertIn("回退 Tavily", result.meta["notes"])
        self.assertNotIn("options.subreddit", result.meta["ignored"])

    def test_arctic_422_still_falls_back(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(error=RedditProviderError(INTERNAL, "Arctic Shift HTTP 422"))
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in result.items], ["abc"])
        self.assertIn("Arctic Shift 失败", result.meta["notes"])
        self.assertIn("回退 Tavily", result.meta["notes"])

    def test_author_fallback_marks_ignored(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(error=RedditProviderError(UNAVAILABLE, "offline"))
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q", options={"author": "spez"}))
        self.assertEqual(result.meta["provider"], "tavily")
        self.assertIn("回退 Tavily", result.meta["notes"])
        self.assertIn("author", result.meta["ignored"])

    def test_arctic_works_without_tavily_key(self):
        arctic = FakeArctic([arctic_raw()])
        ch = self.make(tavily=FakeTavily(), arctic=arctic, key=None)
        result = ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in result.items], ["abc"])
        self.assertFalse(ch.status()[0])
        self.assertIn("Arctic Shift", ch.status()[1])

    def test_arctic_failure_without_key_preserves_arctic_error(self):
        for code in (UNAVAILABLE, INTERNAL):
            with self.subTest(code=code):
                tavily = FakeTavily([tavily_raw()])
                arctic = FakeArctic(error=RedditProviderError(code, f"Arctic {code}"))
                ch = self.make(tavily=tavily, arctic=arctic, key=None)
                with self.assertRaises(ChannelError) as cm:
                    ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
                self.assertEqual(cm.exception.code, code)
                self.assertIn(f"Arctic {code}", cm.exception.message)
                self.assertIn("配置 Tavily API key 后可自动回退", cm.exception.message)
                self.assertEqual(tavily.calls, [])

    def test_fallback_failure_includes_arctic_reason(self):
        tavily = FakeTavily(error=RedditProviderError(INTERNAL, "Tavily down"))
        arctic = FakeArctic(error=RedditProviderError(UNAVAILABLE, "Arctic down"))
        ch = self.make(tavily=tavily, arctic=arctic)
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual(cm.exception.code, INTERNAL)
        self.assertIn("Arctic Shift 失败", cm.exception.message)
        self.assertIn("Arctic down", cm.exception.message)
        self.assertIn("Tavily 回退也失败", cm.exception.message)
        self.assertIn("Tavily down", cm.exception.message)

    def test_arctic_429_does_not_fall_back(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(error=RedditProviderError(RATE_LIMITED, "Arctic limited", 90))
        ch = self.make(tavily=tavily, arctic=arctic)
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual(cm.exception.code, RATE_LIMITED)
        self.assertEqual(cm.exception.retry_after_seconds, 90)
        self.assertEqual(tavily.calls, [])

    def test_tavily_without_key_auth_required(self):
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, key=None)
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertEqual(cm.exception.code, AUTH_REQUIRED)
        self.assertEqual(tavily.calls, [])
        self.assertIn("EAGLE_REDDIT_TAVILY_API_KEY", cm.exception.message)
        self.assertNotIn("test-key", cm.exception.message)

    def test_enrich_success_partial_and_missing_fields(self):
        tavily = FakeTavily([tavily_raw("abc"), tavily_raw("def")])
        arctic = FakeArctic(fetch_results=[
            arctic_raw("abc"),
            {"id": "def", "score": 5},
        ])
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q", limit=2))
        abc, partial = result.items
        self.assertEqual(abc.author.name, "alice")
        self.assertEqual(abc.metrics.likes, 42)
        self.assertEqual(abc.metrics.comments, 7)
        self.assertEqual(abc.score, 42)
        self.assertEqual(abc.created_at, iso(-3600))
        self.assertEqual(abc.text, "Body abc")
        self.assertIn("author", abc.extra["enriched_fields"])
        self.assertEqual(partial.score, 5)
        self.assertEqual(partial.author.name, "")
        self.assertIsNone(partial.metrics.comments)
        self.assertEqual(partial.text, "Chunk def")
        self.assertTrue(partial.extra["enriched"])
        self.assertEqual(arctic.fetch_calls, [["abc", "def"]])
        self.assertEqual(arctic.fetch_timeouts, [5.0])
        self.assertFalse(result.meta["degraded"])

    def test_enrich_failure_keeps_main_results(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(fetch_error=TimeoutError("slow"))
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q"))
        self.assertEqual(len(result.items), 1)
        self.assertIn("Arctic 补全失败", result.meta["notes"])
        self.assertFalse(result.items[0].extra["enriched"])
        self.assertTrue(result.meta["degraded"])

    def test_enrich_false_skips_fetch(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(fetch_results=[arctic_raw()])
        ch = self.make(tavily=tavily, arctic=arctic)
        result = ch.search(Query("q", options={"enrich": False}))
        self.assertEqual(arctic.fetch_calls, [])
        self.assertFalse(result.items[0].extra["enriched"])

    def test_enrich_uses_configured_short_timeout(self):
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic()
        ch = self.make(
            tavily=tavily,
            arctic=arctic,
            config_text="enrich_timeout_seconds = 2.5\n",
        )
        ch.search(Query("q"))
        self.assertEqual(arctic.fetch_timeouts, [2.5])

    def test_since_hours_filters_locally(self):
        tavily = FakeTavily([
            tavily_raw("recent", offset=-3600),
            tavily_raw("old", offset=-48 * 3600),
            {"title": "unknown", "url": "https://www.reddit.com/r/test/comments/unknown/", "content": "x"},
        ])
        ch = self.make(tavily=tavily, arctic=FakeArctic())
        result = ch.search(Query("q", since_hours=24))
        self.assertEqual([i.id for i in result.items], ["recent"])
        self.assertEqual(tavily.calls[0]["since_hours"], 24)
        self.assertIn("since_hours 本地过滤", result.meta["notes"])

    def test_sort_time_score_and_limit(self):
        arctic = FakeArctic([
            arctic_raw("old", offset=-7200, score=1),
            arctic_raw("new", offset=-60, score=5),
            arctic_raw("mid", offset=-3600, score=9),
        ])
        ch = self.make(arctic=arctic, key=None)
        by_time = ch.search(Query("q", limit=2, sort="time", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in by_time.items], ["new", "mid"])
        by_score = ch.search(Query("q", limit=2, sort="score", options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in by_score.items], ["mid", "new"])

    def test_no_results_notes(self):
        ch = self.make(tavily=FakeTavily([]))
        result = ch.search(Query("q"))
        self.assertEqual(result.items, [])
        self.assertIn("没有结果", result.meta["notes"])

    def test_bad_queries(self):
        ch = self.make(tavily=FakeTavily())
        cases = [
            Query(""),
            Query("q", options={"subreddit": "bad/name"}),
            Query("q", options={"author": "bad name"}),
            Query("q", options={"enrich": "maybe"}),
            Query("q", options={"cache": "maybe"}),
        ]
        for query in cases:
            with self.subTest(query=query.to_dict()):
                with self.assertRaises(ChannelError) as cm:
                    ch.search(query)
                self.assertEqual(cm.exception.code, "bad_query")

    def test_provider_codes_pass_through(self):
        for code in (AUTH_REQUIRED, RATE_LIMITED, INTERNAL):
            with self.subTest(code=code):
                ch = self.make(tavily=FakeTavily(error=RedditProviderError(code, "provider failure", 30)))
                with self.assertRaises(ChannelError) as cm:
                    ch.search(Query("q"))
                self.assertEqual(cm.exception.code, code)
                if code == RATE_LIMITED:
                    self.assertEqual(cm.exception.retry_after_seconds, 30)

    def test_key_never_appears_in_errors(self):
        key = "sk-super-secret-123"
        ch = self.make(tavily=FakeTavily(error=RuntimeError(f"boom {key}")), key=key)
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertNotIn(key, cm.exception.message)
        self.assertNotIn(key, str(cm.exception))

    def test_key_resolution_env_first_then_file(self):
        env_ch = self.make(key="env-key", config_text='api_key = "file-key"\n')
        self.assertEqual(env_ch.api_key, "env-key")
        file_ch = self.make(key=None, config_text='api_key = "file-key"\n')
        self.assertEqual(file_ch.api_key, "file-key")

    def test_cache_hit_and_age(self):
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, arctic=FakeArctic())
        first = ch.search(Query("q", limit=3))
        self.assertEqual(first.meta["cache"], "miss")
        second = ch.search(Query("q", limit=3))
        self.assertEqual(second.meta["cache"], "hit")
        self.assertEqual(second.meta["cache_age_seconds"], 0)
        self.assertIn("cached_at", second.meta)
        self.assertEqual(len(tavily.calls), 1)
        self.assertEqual(second.items[0].id, "abc")

    def test_since_hours_cache_hit_refilters_with_current_clock(self):
        now = [NOW]
        arctic = FakeArctic([arctic_raw("p1", offset=-1800)])
        ch = self.make(arctic=arctic, key=None, clock=lambda: now[0])
        first = ch.search(Query("q", since_hours=1, options={"subreddit": "LocalLLaMA"}))
        self.assertEqual([i.id for i in first.items], ["p1"])
        now[0] += 20 * 3600
        second = ch.search(Query("q", since_hours=1, options={"subreddit": "LocalLLaMA"}))
        self.assertEqual(second.meta["cache"], "hit")
        self.assertEqual(second.items, [])
        self.assertIn("缓存命中后 since_hours 过滤掉 1 条", second.meta["notes"])
        self.assertEqual(len(arctic.search_calls), 1)

    def test_since_hours_cache_hit_without_removals_has_no_filter_note(self):
        now = [NOW]
        arctic = FakeArctic([arctic_raw("p1", offset=-1800)])
        ch = self.make(arctic=arctic, key=None, clock=lambda: now[0])
        ch.search(Query("q", since_hours=1, options={"subreddit": "LocalLLaMA"}))
        now[0] += 60
        second = ch.search(Query("q", since_hours=1, options={"subreddit": "LocalLLaMA"}))
        self.assertEqual(second.meta["cache"], "hit")
        self.assertEqual([i.id for i in second.items], ["p1"])
        self.assertNotIn("过滤掉 0 条", second.meta["notes"])
        self.assertNotIn("缓存命中后 since_hours", second.meta["notes"])

    def test_cache_key_uses_validated_normalized_names(self):
        arctic = FakeArctic([arctic_raw()])
        ch = self.make(arctic=arctic, key=None)
        ch.search(Query("q", options={"subreddit": "r/LocalLLaMA"}))
        result = ch.search(Query("q", options={"subreddit": "localllama"}))
        self.assertEqual(result.meta["cache"], "hit")
        self.assertEqual(len(arctic.search_calls), 1)

    def test_degraded_fallback_uses_short_ttl_and_marks_cache(self):
        now = [NOW]
        tavily = FakeTavily([tavily_raw()])
        arctic = FakeArctic(error=RedditProviderError(UNAVAILABLE, "offline"))
        ch = self.make(tavily=tavily, arctic=arctic, clock=lambda: now[0])
        first = ch.search(Query("q", options={"author": "spez"}))
        self.assertTrue(first.meta["degraded"])
        self.assertIn("author", first.meta["ignored"])
        self.assertEqual(first.meta["cache"], "miss")
        cached = [json.loads(p.read_text(encoding="utf-8")) for p in (self.home / "reddit" / "cache").glob("*.json")]
        self.assertTrue(cached and all(payload["degraded"] is True for payload in cached))

        now[0] += 30 * 60
        hit = ch.search(Query("q", options={"author": "spez"}))
        self.assertEqual(hit.meta["cache"], "hit")
        self.assertTrue(hit.meta["degraded"])
        self.assertEqual((len(arctic.search_calls), len(tavily.calls)), (1, 1))

        now[0] += 40 * 60
        refreshed = ch.search(Query("q", options={"author": "spez"}))
        self.assertEqual(refreshed.meta["cache"], "miss")
        self.assertTrue(refreshed.meta["degraded"])
        self.assertEqual((len(arctic.search_calls), len(tavily.calls)), (2, 2))

    def test_unknown_options_are_ignored_but_do_not_mark_degraded(self):
        ch = self.make(tavily=FakeTavily([tavily_raw()]), arctic=FakeArctic())
        result = ch.search(Query("q", options={"zzz": 1}))
        self.assertIn("options.zzz", result.meta["ignored"])
        self.assertFalse(result.meta["degraded"])

    def test_cache_false_skips_read_but_writes(self):
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, arctic=FakeArctic())
        ch.search(Query("q"))
        bypass = ch.search(Query("q", options={"cache": False}))
        self.assertEqual(bypass.meta["cache"], "bypass")
        self.assertEqual(len(tavily.calls), 2)
        hit = ch.search(Query("q"))
        self.assertEqual(hit.meta["cache"], "hit")
        self.assertEqual(len(tavily.calls), 2)

    def test_cache_ttl_zero_disables(self):
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, arctic=FakeArctic(), config_text="cache_ttl_hours = 0\n")
        first = ch.search(Query("q"))
        second = ch.search(Query("q"))
        self.assertEqual((first.meta["cache"], second.meta["cache"]), ("disabled", "disabled"))
        self.assertEqual(len(tavily.calls), 2)

    def test_corrupt_cache_is_miss(self):
        tavily = FakeTavily([tavily_raw()])
        ch = self.make(tavily=tavily, arctic=FakeArctic())
        ch.search(Query("q", limit=3))
        key = make_key("tavily", "q", 3, None, "relevance", {"enrich": True})
        path = self.home / "reddit" / "cache" / f"{key}.json"
        for bad in ("{broken", json.dumps({"cached_at": NOW, "items": "oops", "meta": {}})):
            with self.subTest(cache=bad):
                path.write_text(bad, encoding="utf-8")
                result = ch.search(Query("q", limit=3))
                self.assertEqual(result.meta["cache"], "miss")
        self.assertEqual(len(tavily.calls), 3)

    def test_parse_post_url_supported_forms_and_invalid_ids(self):
        supported = {
            "https://www.reddit.com/r/Py/comments/abc123/t/": ("Py", "abc123"),
            "https://new.reddit.com/r/Py/comments/abc/": ("Py", "abc"),
            "https://sh.reddit.com/r/Py/comments/abc": ("Py", "abc"),
            "https://reddit.com/comments/abc/": (None, "abc"),
            "https://www.reddit.com/user/bob/comments/abc/t/": (None, "abc"),
            "https://www.reddit.com/r/../comments/abc/": (None, "abc"),
        }
        for url, expected in supported.items():
            with self.subTest(url=url):
                self.assertEqual(_parse_post_url(url), expected)
        rejected = [
            "https://redd.it/abc",
            "https://www.reddit.com/r/Py/s/XyZ",
            "https://www.reddit.com/r/Py/comments/%2e%2e/t/",
            "https://www.reddit.com/r/Py/comments/abcdefghijklmn/t/",
            "https://www.reddit.com/r/Py/comments/ABC/t/",
            "https://evilreddit.com/r/Py/comments/abc/",
            "https://www.reddit.com.evil.com/r/Py/comments/abc/",
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(_parse_post_url(url))

    def test_comments_url_without_subreddit_maps_to_reddit_url(self):
        tavily = FakeTavily([{
            "title": "No sub",
            "url": "https://new.reddit.com/comments/abc123/",
            "content": "chunk",
        }])
        ch = self.make(tavily=tavily, arctic=FakeArctic())
        result = ch.search(Query("q"))
        self.assertEqual(result.items[0].url, "https://www.reddit.com/comments/abc123/")
        self.assertEqual(result.items[0].extra["subreddit"], "")

    def test_real_provider_key_not_in_errors_or_headers(self):
        key = "tvly-SECRET-xyz"
        seen = {}

        def opener(req, timeout=None):
            seen["authorization"] = req.get_header("Authorization")
            seen["body"] = req.data.decode("utf-8")
            raise urllib.error.HTTPError(req.full_url, 401, f"Unauthorized {key}", {}, None)

        ch = self.make(tavily=TavilyProvider(key, opener=opener), arctic=FakeArctic(), key=key)
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertNotIn(key, cm.exception.message)
        self.assertNotIn(key, str(cm.exception))
        self.assertNotIn(key, seen["body"])
        self.assertEqual(seen["authorization"], f"Bearer {key}")
        self.assertFalse(list((self.home / "reddit" / "cache").glob("*.json")))

    def test_real_provider_key_not_in_cache_or_meta(self):
        key = "tvly-SECRET-xyz"
        seen = {}

        def opener(req, timeout=None):
            seen["authorization"] = req.get_header("Authorization")
            seen["body"] = req.data.decode("utf-8")
            return TavilyResponse({"results": [tavily_raw()]})

        ch = self.make(tavily=TavilyProvider(key, opener=opener), arctic=FakeArctic(), key=key)
        result = ch.search(Query("q"))
        self.assertEqual(seen["authorization"], f"Bearer {key}")
        self.assertNotIn(key, seen["body"])
        self.assertNotIn(key, json.dumps(result.meta, ensure_ascii=False))
        self.assertNotIn(key, json.dumps([item.to_dict() for item in result.items], ensure_ascii=False))
        blob = "".join(path.read_text(encoding="utf-8") for path in self.home.rglob("*.json"))
        self.assertNotIn(key, blob)

    def test_describe_options(self):
        d = self.make().describe().to_dict()
        for key in ("subreddit", "author", "enrich", "cache"):
            self.assertIn(key, d["options"])


if __name__ == "__main__":
    unittest.main()
