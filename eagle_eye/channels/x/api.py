"""twitterapi.io 的最小客户端：带限速和重试的 GET。"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .settings import env

BASE = "https://api.twitterapi.io"
RETRYABLE = {429, 500, 502, 503, 504}


class XError(RuntimeError):
    """X 渠道错误。code 对应协议错误码（auth_required / rate_limited / unavailable / internal ...）。"""

    def __init__(self, message, code="internal", retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def http_code(status):
    if status in (401, 403):
        return "auth_required"
    if status in (402, 429):  # 402 = twitterapi.io 额度用完
        return "rate_limited"
    if status == 400:
        return "bad_query"
    return "internal"


class Client:
    def __init__(self, api_key=None, interval=None, max_retries=3, timeout=60):
        if interval is None:
            interval = float(env("INTERVAL", "5.5"))
        self.api_key = api_key or os.environ.get("TWITTERAPI_IO_KEY")
        if not self.api_key:
            raise XError("缺少环境变量 TWITTERAPI_IO_KEY", "auth_required")
        self.interval = interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._last = 0.0

    def _throttle(self):
        # 免费档限速 1 次 / 5 秒，所有请求之间统一留间隔
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def get(self, path, **params):
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"x-api-key": self.api_key, "User-Agent": "eagle-eye/0.1"},
        )
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in RETRYABLE and attempt < self.max_retries:
                    time.sleep(self.interval * (attempt + 1))
                    continue
                body = e.read().decode(errors="replace")[:300]
                raise XError(f"{path} HTTP {e.code}: {body}", http_code(e.code)) from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    time.sleep(self.interval * (attempt + 1))
                    continue
                raise XError(f"{path} 网络错误: {e}", "unavailable") from e

    def search(self, query, cursor="", query_type="Latest"):
        return self.get(
            "/twitter/tweet/advanced_search",
            query=query,
            queryType=query_type,
            cursor=cursor,
        )

    def article(self, tweet_id):
        return self.get("/twitter/article", tweet_id=tweet_id)


def make_client(backend=None):
    """按 EAGLE_X_BACKEND / XSEARCH_BACKEND（twscrape / twitterapi / auto）选后端。

    auto（默认）：装了 twscrape 且登记过账号就用 twscrape，否则用 twitterapi.io。
    """
    from . import tws

    backend = (backend or env("BACKEND") or "auto").lower()
    if backend == "auto":
        backend = "twscrape" if tws.available() else "twitterapi"
    if backend == "twscrape":
        return tws.TwscrapeClient()
    if backend == "twitterapi":
        return Client()
    raise XError(f"未知后端 {backend}（可选 twscrape / twitterapi / auto）", "bad_query")
