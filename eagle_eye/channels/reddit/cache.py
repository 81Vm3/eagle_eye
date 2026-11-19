"""Reddit 查询结果缓存：一查询一文件，临时文件 + os.replace 原子写入。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ...core.models import ChannelError, SearchResult


def _normalize_name(value):
    value = str(value).strip()
    lowered = value.lower()
    for prefix in ("r/", "u/", "/r/", "/u/"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    return value.casefold()


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return value


def make_key(provider, text, limit, since_hours, sort, options):
    """规范化查询字段并做 sha256；返回文件名安全的十六进制串。"""
    normalized_text = " ".join(str(text).split()).casefold()
    normalized_options = {}
    for key, value in (options or {}).items():
        if key == "cache":
            continue
        if key in ("subreddit", "author") and isinstance(value, str):
            normalized_options[key] = _normalize_name(value)
        elif key == "enrich":
            normalized_options[key] = _normalize_bool(value)
        elif isinstance(value, str):
            normalized_options[key] = value.strip()
        else:
            normalized_options[key] = value
    payload = {
        "provider": str(provider),
        "text": normalized_text,
        "limit": limit,
        "since_hours": since_hours,
        "sort": sort,
        "options": normalized_options,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CacheHit:
    result: SearchResult
    cached_at: float
    age_seconds: float
    degraded: bool = False


class ResultCache:
    def __init__(self, directory, ttl_hours=24, degraded_ttl_hours=1, clock=time.time):
        self.directory = Path(directory)
        self.ttl_hours = max(0.0, float(ttl_hours))
        self.degraded_ttl_hours = max(0.0, float(degraded_ttl_hours))
        self.clock = clock

    def _path(self, key):
        return self.directory / f"{key}.json"

    def get(self, key):
        if self.ttl_hours <= 0:
            return None
        try:
            with open(self._path(key), "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return None
            cached_at = float(payload["cached_at"])
            if not math.isfinite(cached_at):
                return None
            if not isinstance(payload.get("items"), list):
                return None
            if not isinstance(payload.get("meta"), dict):
                return None
            degraded = bool(payload.get("degraded", False))
            ttl_hours = self.degraded_ttl_hours if degraded else self.ttl_hours
            if ttl_hours <= 0:
                return None
            age = float(self.clock()) - cached_at
            if age < 0 or age > ttl_hours * 3600:
                return None
            result = SearchResult.from_dict(payload)
            return CacheHit(result, cached_at, age, degraded)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, ChannelError):
            return None

    def put(self, key, result, degraded=False):
        ttl_hours = self.degraded_ttl_hours if degraded else self.ttl_hours
        if ttl_hours <= 0:
            return False
        self.directory.mkdir(parents=True, exist_ok=True)
        cached_at = float(self.clock())
        payload = result.to_dict()
        payload["cached_at"] = cached_at
        payload["degraded"] = bool(degraded)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{key[:12]}-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._path(key))
            return True
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
