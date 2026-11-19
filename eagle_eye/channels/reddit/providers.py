"""Tavily / Arctic Shift HTTP 客户端。

只用标准库，不打印请求参数；错误消息里也不包含 API key。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ... import __version__
from ...core.models import AUTH_REQUIRED, BAD_QUERY, INTERNAL, RATE_LIMITED, UNAVAILABLE

DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
DEFAULT_ARCTIC_BASE_URL = "https://arctic-shift.photon-reddit.com"
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = f"eagle-eye/{__version__}"


class RedditProviderError(Exception):
    """Provider 层错误，code 直接使用协议错误码。"""

    def __init__(self, code, message="", retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


def redact_secret(message, secret):
    message = str(message)
    if secret:
        message = message.replace(secret, "[redacted]")
    return message[:500]


def _parse_timestamp(value):
    """把 epoch 秒、毫秒或 ISO 时间戳解析成秒；解析不了返回 None。"""
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds / 1000 if seconds > 1e12 else seconds
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _retry_after(headers):
    now = time.time()
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw is not None:
            text = str(raw).strip()
            try:
                return max(1, int(float(text)))
            except (TypeError, ValueError):
                try:
                    parsed = parsedate_to_datetime(text)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return max(1, int(parsed.timestamp() - now))
                except (TypeError, ValueError, OverflowError):
                    pass
        # Arctic Shift 的 X-RateLimit-Reset 是「距重置的秒数」；大于 1e9 时
        # 按 Unix 时间戳兜底（大于 1e12 视为毫秒），兼容少数按时间戳返回的后端。
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            try:
                seconds = float(str(reset).strip())
            except (TypeError, ValueError):
                seconds = None
            if seconds is not None:
                if seconds > 1e12:
                    seconds = seconds / 1000 - now
                elif seconds > 1e9:
                    seconds -= now
                return max(1, int(seconds))
        reset_at = headers.get("X-RateLimit-Reset-At")
        if reset_at is not None:
            timestamp = _parse_timestamp(reset_at)
            if timestamp is not None:
                return max(1, int(timestamp - now))
    return 60


def _http_error(err, service, bad_query_statuses=(), requires_auth=True):
    status = int(getattr(err, "code", 0) or 0)
    headers = getattr(err, "headers", {}) or {}
    if status in bad_query_statuses:
        raise RedditProviderError(BAD_QUERY, f"{service} HTTP {status}（请求参数不合法）") from None
    if status in (401, 403):
        if not requires_auth:
            raise RedditProviderError(
                UNAVAILABLE,
                f"{service} 拒绝访问（HTTP {status}，可能被 Cloudflare 拦截或限制）",
            ) from None
        raise RedditProviderError(AUTH_REQUIRED, f"{service} HTTP {status}（API key 无效或无权访问）") from None
    if status in (402, 429, 432, 433, 434):
        raise RedditProviderError(
            RATE_LIMITED, f"{service} HTTP {status}（限速或额度不足）", _retry_after(headers)
        ) from None
    if status == 422:
        raise RedditProviderError(INTERNAL, f"{service} HTTP 422（请求参数或数据不可用）") from None
    if 500 <= status <= 599:
        raise RedditProviderError(INTERNAL, f"{service} HTTP {status}") from None
    raise RedditProviderError(INTERNAL, f"{service} HTTP {status or '未知状态'}") from None


def _decode_json(raw, service):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RedditProviderError(INTERNAL, f"{service} 返回了非法 JSON: {type(e).__name__}") from None


def _extract_posts(payload, service):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "posts", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise RedditProviderError(INTERNAL, f"{service} 响应缺少 data 数组")


def _date_only(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止 Tavily 跟随重定向，避免 Authorization 头被转发到第三方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _no_redirect_opener():
    return urllib.request.build_opener(_NoRedirectHandler())


class TavilyProvider:
    """Tavily Search API 的最小封装。"""

    def __init__(self, api_key, base_url=DEFAULT_TAVILY_BASE_URL, timeout=DEFAULT_TIMEOUT, opener=None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_TAVILY_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._open = opener or _no_redirect_opener().open

    def _call(self, payload):
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        try:
            with self._open(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            _http_error(e, "Tavily", bad_query_statuses=(400, 422))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise RedditProviderError(
                UNAVAILABLE, redact_secret(f"Tavily 连接失败: {type(reason).__name__}: {reason}", self.api_key)
            ) from None
        data = _decode_json(raw, "Tavily")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RedditProviderError(INTERNAL, "Tavily 响应缺少 results 数组")
        return data["results"]

    def search(self, text, limit=20, subreddit=None, since_hours=None, now=None):
        max_results = min(max(int(limit or 20), 1), 20)
        query = f"site:reddit.com/r/{subreddit} {text}" if subreddit else text
        payload = {
            "query": query,
            "include_domains": ["reddit.com"],
            "search_depth": "basic",
            "max_results": max_results,
            "include_published_date": True,
        }
        if since_hours:
            current = (now() if callable(now) else now) if now is not None else time.time()
            cutoff = current - float(since_hours) * 3600
            payload["start_date"] = _date_only(cutoff)
        return self._call(payload)


class ArcticProvider:
    """Arctic Shift posts/search 与 posts/ids 的最小封装。"""

    def __init__(self, base_url=DEFAULT_ARCTIC_BASE_URL, timeout=DEFAULT_TIMEOUT, opener=None):
        self.base_url = (base_url or DEFAULT_ARCTIC_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._open = opener or urllib.request.urlopen

    def _get(self, path, params, timeout=None):
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT},
            method="GET",
        )
        try:
            with self._open(req, timeout=self.timeout if timeout is None else float(timeout)) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            _http_error(e, "Arctic Shift", requires_auth=False)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise RedditProviderError(
                UNAVAILABLE, f"Arctic Shift 连接失败: {type(reason).__name__}: {reason}"
            ) from None
        data = _decode_json(raw, "Arctic Shift")
        return _extract_posts(data, "Arctic Shift")

    def search(self, text, subreddit=None, author=None, since_hours=None, limit=20, now=None):
        params = {
            "query": text,
            "limit": min(max(int(limit or 100), 1), 100),
            "sort": "desc",
        }
        if subreddit:
            params["subreddit"] = subreddit
        if author:
            params["author"] = author
        if since_hours:
            current = (now() if callable(now) else now) if now is not None else time.time()
            params["after"] = int(current - float(since_hours) * 3600)
        return self._get("/api/posts/search", params)

    def fetch_by_ids(self, ids, timeout=None):
        ids = [str(i) for i in ids if i]
        if not ids:
            return []
        return self._get("/api/posts/ids", {"ids": ",".join(ids[:500])}, timeout=timeout)
