"""Tavily / Arctic Shift provider：请求参数与 HTTP 错误码映射。不访问网络。"""

import email.message
import io
import json
import unittest
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from eagle_eye.channels.reddit.providers import (
    AUTH_REQUIRED,
    BAD_QUERY,
    INTERNAL,
    RATE_LIMITED,
    UNAVAILABLE,
    ArcticProvider,
    RedditProviderError,
    TavilyProvider,
    _NoRedirectHandler,
    _no_redirect_opener,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, (dict, list)):
            return json.dumps(self.payload).encode("utf-8")
        return str(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, retry_after=None, x_reset=None, x_reset_at=None, url="https://example.test"):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if x_reset is not None:
        headers["X-RateLimit-Reset"] = str(x_reset)
    if x_reset_at is not None:
        headers["X-RateLimit-Reset-At"] = str(x_reset_at)
    return urllib.error.HTTPError(url, status, "error", headers, io.BytesIO(b""))


class RedirectingHTTPHandler(urllib.request.HTTPHandler):
    """返回一个 302，记录实际收到的请求，用于验证不会跟随重定向。"""

    def __init__(self, location):
        super().__init__()
        self.location = location
        self.requests = []

    def http_open(self, req):
        self.requests.append(req)
        headers = email.message.Message()
        headers["Location"] = self.location
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        response.msg = "Found"
        return response


class TavilyProviderTest(unittest.TestCase):
    def test_search_payload(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req, timeout))
            return FakeResponse({"results": []})

        provider = TavilyProvider("secret-key", opener=opener, timeout=7)
        provider.search(
            "local llm",
            limit=50,
            subreddit="LocalLLaMA",
            since_hours=48,
            now=lambda: 1_700_000_000,
        )
        req = seen[0][0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.tavily.com/search")
        self.assertEqual(seen[0][1], 7.0)
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("api_key", payload)
        self.assertNotIn("secret-key", json.dumps(payload))
        self.assertEqual(payload["include_domains"], ["reddit.com"])
        self.assertEqual(payload["search_depth"], "basic")
        self.assertEqual(payload["max_results"], 20)
        self.assertTrue(payload["include_published_date"])
        self.assertEqual(payload["query"], "site:reddit.com/r/LocalLLaMA local llm")
        expected = datetime.fromtimestamp(1_700_000_000 - 48 * 3600, timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(payload["start_date"], expected)

    def test_limit_zero_requests_twenty(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req)
            return FakeResponse({"results": []})

        TavilyProvider("k", opener=opener).search("q", limit=0)
        self.assertEqual(json.loads(seen[0].data)["max_results"], 20)

    def test_http_error_mapping_and_retry_after(self):
        cases = [
            (401, AUTH_REQUIRED, None, None),
            (403, AUTH_REQUIRED, None, None),
            (402, RATE_LIMITED, None, 60),
            (429, RATE_LIMITED, None, 60),
            (429, RATE_LIMITED, "120", 120),
            (432, RATE_LIMITED, None, 60),
            (433, RATE_LIMITED, None, 60),
            (434, RATE_LIMITED, None, 60),
            (500, INTERNAL, None, None),
            (503, INTERNAL, None, None),
            (400, BAD_QUERY, None, None),
            (422, BAD_QUERY, None, None),
        ]
        for status, code, retry_header, expected_retry in cases:
            with self.subTest(status=status, retry=retry_header):
                def opener(req, timeout=None, status=status, retry_header=retry_header):
                    raise http_error(status, retry_header)

                provider = TavilyProvider("super-secret-key", opener=opener)
                with self.assertRaises(RedditProviderError) as cm:
                    provider.search("q")
                self.assertEqual(cm.exception.code, code)
                self.assertEqual(cm.exception.retry_after_seconds, expected_retry)
                self.assertNotIn("super-secret-key", str(cm.exception))

    def test_retry_after_http_date_and_rate_limit_reset(self):
        now = 1_700_000_000.0
        future = format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True)
        with mock.patch("eagle_eye.channels.reddit.providers.time.time", return_value=now):
            provider = TavilyProvider(
                "secret", opener=lambda req, timeout=None: (_ for _ in ()).throw(http_error(429, future))
            )
            with self.assertRaises(RedditProviderError) as cm:
                provider.search("q")
            self.assertEqual(cm.exception.retry_after_seconds, 90)

            provider = TavilyProvider(
                "secret", opener=lambda req, timeout=None: (_ for _ in ()).throw(http_error(429, x_reset=30))
            )
            with self.assertRaises(RedditProviderError) as cm:
                provider.search("q")
            self.assertEqual(cm.exception.retry_after_seconds, 30)

            provider = TavilyProvider(
                "secret",
                opener=lambda req, timeout=None: (_ for _ in ()).throw(
                    http_error(429, "not-a-date", x_reset=now + 75)
                ),
            )
            with self.assertRaises(RedditProviderError) as cm:
                provider.search("q")
            self.assertEqual(cm.exception.retry_after_seconds, 75)

    def test_retry_after_reset_at_epoch_and_iso(self):
        now = 1_700_000_000.0
        iso_future = datetime.fromtimestamp(now + 60, timezone.utc).isoformat()
        cases = [
            ({"x_reset_at": int(now) + 45}, 45),
            ({"x_reset_at": iso_future}, 60),
            ({"x_reset_at": "1970-01-01T00:00:00Z"}, 1),
            ({"retry_after": "0"}, 1),
            ({"x_reset": 0}, 1),
        ]
        with mock.patch("eagle_eye.channels.reddit.providers.time.time", return_value=now):
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    provider = TavilyProvider(
                        "secret",
                        opener=lambda req, timeout=None, headers=headers: (_ for _ in ()).throw(
                            http_error(429, **headers)
                        ),
                    )
                    with self.assertRaises(RedditProviderError) as cm:
                        provider.search("q")
                    self.assertEqual(cm.exception.retry_after_seconds, expected)

    def test_cross_domain_redirect_is_not_followed_and_key_not_leaked(self):
        handler = RedirectingHTTPHandler("https://evil.example/collect")
        opener = urllib.request.build_opener(handler, _NoRedirectHandler())
        provider = TavilyProvider(
            "super-secret-key",
            base_url="http://api.tavily.test",
            opener=opener.open,
        )
        with self.assertRaises(RedditProviderError) as cm:
            provider.search("q")
        self.assertEqual(cm.exception.code, INTERNAL)
        self.assertIn("HTTP 302", cm.exception.message)
        self.assertEqual(len(handler.requests), 1)
        self.assertEqual(handler.requests[0].full_url, "http://api.tavily.test/search")
        self.assertEqual(handler.requests[0].get_header("Authorization"), "Bearer super-secret-key")
        self.assertNotIn("super-secret-key", str(cm.exception))

    def test_default_opener_disables_redirects(self):
        opener = _no_redirect_opener()
        self.assertTrue(any(isinstance(handler, _NoRedirectHandler) for handler in opener.handlers))

    def test_connection_and_timeout_are_unavailable(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        provider = TavilyProvider("secret", opener=opener)
        with self.assertRaises(RedditProviderError) as cm:
            provider.search("q")
        self.assertEqual(cm.exception.code, UNAVAILABLE)
        self.assertNotIn("secret", str(cm.exception))

    def test_malformed_json_is_internal(self):
        provider = TavilyProvider("secret", opener=lambda req, timeout=None: FakeResponse("{not json"))
        with self.assertRaises(RedditProviderError) as cm:
            provider.search("q")
        self.assertEqual(cm.exception.code, INTERNAL)


class ArcticProviderTest(unittest.TestCase):
    def test_search_params(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req)
            return FakeResponse({"data": []})

        provider = ArcticProvider(opener=opener)
        provider.search(
            "local llm",
            subreddit="LocalLLaMA",
            author="spez",
            since_hours=24,
            limit=0,
            now=lambda: 1_700_000_000,
        )
        parts = urlsplit(seen[0].full_url)
        params = parse_qs(parts.query)
        self.assertEqual(parts.path, "/api/posts/search")
        self.assertEqual(params["query"], ["local llm"])
        self.assertEqual(params["subreddit"], ["LocalLLaMA"])
        self.assertEqual(params["author"], ["spez"])
        self.assertEqual(params["limit"], ["100"])
        self.assertEqual(params["sort"], ["desc"])
        self.assertEqual(params["after"], [str(1_700_000_000 - 24 * 3600)])

    def test_fetch_by_ids(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req, timeout))
            return FakeResponse({"data": [{"id": "abc"}]})

        posts = ArcticProvider(opener=opener).fetch_by_ids(["abc", "def"], timeout=2.5)
        self.assertEqual(posts, [{"id": "abc"}])
        self.assertEqual(urlsplit(seen[0][0].full_url).path, "/api/posts/ids")
        self.assertEqual(parse_qs(urlsplit(seen[0][0].full_url).query)["ids"], ["abc,def"])
        self.assertEqual(seen[0][1], 2.5)

    def test_422_is_internal(self):
        def opener(req, timeout=None):
            raise http_error(422)

        with self.assertRaises(RedditProviderError) as cm:
            ArcticProvider(opener=opener).search("q")
        self.assertEqual(cm.exception.code, INTERNAL)


if __name__ == "__main__":
    unittest.main()
