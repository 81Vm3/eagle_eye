"""Reddit 结果缓存：命中、过期、关闭、损坏与并发原子写入。不访问网络。"""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eagle_eye.channels.reddit.cache import ResultCache, make_key
from eagle_eye.core.models import Item, SearchResult


def result(post_id="abc"):
    return SearchResult([Item("reddit", post_id, f"https://www.reddit.com/r/test/comments/{post_id}/")], {"provider": "tavily"})


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.key = make_key("tavily", "query", 3, None, "relevance", {})

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_normalizes_text_and_ignores_cache_switch(self):
        a = make_key("tavily", "  Local   LLM ", 3, None, "relevance", {"subreddit": "LocalLLaMA", "cache": True})
        b = make_key("tavily", "local llm", 3, None, "relevance", {"subreddit": "LocalLLaMA", "cache": False})
        c = make_key("tavily", "local llm", 3, None, "relevance", {"subreddit": "other"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(
            make_key("arctic", "q", 20, None, "relevance", {"subreddit": "r/Python", "author": "u/Bob", "enrich": "true"}),
            make_key("arctic", "q", 20, None, "relevance", {"subreddit": "python", "author": "bob", "enrich": True}),
        )

    def test_hit(self):
        cache = ResultCache(self.dir, ttl_hours=24, clock=lambda: 1000.0)
        self.assertTrue(cache.put(self.key, result()))
        hit = cache.get(self.key)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.result.items[0].id, "abc")
        self.assertEqual(hit.age_seconds, 0.0)
        self.assertEqual(hit.cached_at, 1000.0)

    def test_expired(self):
        now = [1000.0]
        cache = ResultCache(self.dir, ttl_hours=1, clock=lambda: now[0])
        cache.put(self.key, result())
        now[0] = 1000.0 + 3601
        self.assertIsNone(cache.get(self.key))

    def test_disabled(self):
        cache = ResultCache(self.dir, ttl_hours=0, clock=lambda: 1000.0)
        self.assertFalse(cache.put(self.key, result()))
        self.assertIsNone(cache.get(self.key))
        self.assertFalse((self.dir / f"{self.key}.json").exists())

    def test_corrupt_file_is_miss(self):
        cache = ResultCache(self.dir, ttl_hours=24, clock=lambda: 1000.0)
        (self.dir / f"{self.key}.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(cache.get(self.key))

    def test_valid_json_with_bad_structure_or_time_is_miss(self):
        cache = ResultCache(self.dir, ttl_hours=24, clock=lambda: 1000.0)
        path = self.dir / f"{self.key}.json"
        bad_payloads = [
            {"cached_at": 1000.0, "items": "oops", "meta": {}},
            {"cached_at": 1000.0, "items": [], "meta": {}, "error": {"code": "internal"}},
            {"cached_at": 1000.0, "items": [], "meta": "oops"},
            {"cached_at": float("nan"), "items": [], "meta": {}},
            {"cached_at": float("inf"), "items": [], "meta": {}},
            {"cached_at": "not-a-number", "items": [], "meta": {}},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(cache.get(self.key))

    def test_degraded_uses_short_ttl_and_exposes_flag(self):
        now = [1000.0]
        cache = ResultCache(self.dir, ttl_hours=24, degraded_ttl_hours=1, clock=lambda: now[0])
        self.assertTrue(cache.put(self.key, result(), degraded=True))
        payload = json.loads((self.dir / f"{self.key}.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["degraded"])
        hit = cache.get(self.key)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.degraded)
        now[0] += 3601
        self.assertIsNone(cache.get(self.key))

    def test_degraded_ttl_zero_disables_only_degraded_cache(self):
        cache = ResultCache(self.dir, ttl_hours=24, degraded_ttl_hours=0, clock=lambda: 1000.0)
        self.assertFalse(cache.put(self.key, result(), degraded=True))
        self.assertTrue(cache.put(self.key, result()))
        self.assertFalse(cache.get(self.key).degraded)

    def test_atomic_concurrent_writes(self):
        cache = ResultCache(self.dir, ttl_hours=24, clock=lambda: 1000.0)

        def write(idx):
            cache.put(self.key, result(f"p{idx}"))

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(40)))
        path = self.dir / f"{self.key}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 1)
        self.assertIn(data["items"][0]["id"], {f"p{i}" for i in range(40)})
        self.assertFalse(list(self.dir.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
