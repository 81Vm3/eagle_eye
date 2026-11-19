"""ExternalChannel：用 tests/fixtures/fake_channel.py 模拟外部渠道的成功 / 错误 / 超时 / 坏 JSON。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from eagle_eye.channels.external import ExternalChannel, resolve_arg
from eagle_eye.core.models import ChannelError, Query

ROOT = Path(__file__).resolve().parents[1]
FAKE = "tests/fixtures/fake_channel.py"  # 相对仓库根目录，验证路径解析


def make(mode, **cfg):
    logs = []
    ch = ExternalChannel("fake", {"command": [sys.executable, FAKE, mode], **cfg}, ROOT, log=logs.append)
    return ch, logs


class ExternalChannelTest(unittest.TestCase):
    def test_describe_ignores_unknown_fields(self):
        ch, _ = make("ok")
        d = ch.describe()
        self.assertEqual((d.name, d.title, d.options["n"]["default"]), ("fake", "假渠道", 2))
        self.assertEqual(ch.status(), (True, ""))

    def test_search_ok_with_cwd_and_stderr_stream(self):
        ch, logs = make("ok", cwd="tests")
        res = ch.search(Query("hello", limit=5, since_hours=3, options={"n": 3}))
        self.assertEqual([i.id for i in res.items], ["0", "1", "2"])
        self.assertTrue(all(i.channel == "fake" for i in res.items))
        self.assertEqual(res.meta["cwd"], str(ROOT / "tests"))
        self.assertEqual(res.meta["query"], {"protocol": 1, "text": "hello", "limit": 5, "since_hours": 3, "sort": "relevance", "options": {"n": 3}})
        self.assertEqual(res.items[1].metrics.likes, 1)
        self.assertTrue(any("启动" in m and m.startswith("[fake]") for m in logs))

    def test_error_object_with_nonzero_exit(self):
        ch, _ = make("error")
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        e = cm.exception
        self.assertEqual((e.code, e.retry_after_seconds, e.channel), ("cooldown", 3600, "fake"))

    def test_error_object_with_zero_exit(self):
        ch, _ = make("error0")
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertEqual(cm.exception.code, "busy")

    def test_timeout_kills_process(self):
        ch, _ = make("timeout", timeout_seconds=1)
        t0 = time.monotonic()
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(cm.exception.code, "internal")
        self.assertIn("超时", cm.exception.message)
        self.assertIn("启动", cm.exception.message)  # 附 stderr 尾部

    def test_bad_json_is_internal_with_stderr_tail(self):
        ch, _ = make("badjson")
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        self.assertEqual(cm.exception.code, "internal")
        self.assertIn("不是合法 JSON", cm.exception.message)
        self.assertIn("panic: something", cm.exception.message)

    def test_crash_without_output(self):
        ch, _ = make("crash")
        with self.assertRaises(ChannelError) as cm:
            ch.search(Query("q"))
        msg = cm.exception.message
        self.assertEqual(cm.exception.code, "internal")
        self.assertIn("exit 3", msg)
        self.assertIn("stderr line 29", msg)
        self.assertNotIn("stderr line 5\n", msg)  # 只保留末尾若干行

    def test_bad_describe(self):
        ch, _ = make("badjson")
        ok, reason = ch.status()
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("internal"))

    def test_invalid_items_skipped(self):
        ch, _ = make("invalid")
        res = ch.search(Query("q"))
        self.assertEqual([i.id for i in res.items], ["1"])
        self.assertEqual(res.meta["invalid_items"], 1)

    def test_missing_binary_is_unavailable(self):
        ch = ExternalChannel("xhs", {"command": ["bin/does-not-exist", "channel"], "title": "小红书"}, ROOT)
        for call in (ch.describe, lambda: ch.search(Query("q"))):
            with self.assertRaises(ChannelError) as cm:
                call()
            self.assertEqual(cm.exception.code, "unavailable")
        ok, reason = ch.status()
        self.assertFalse(ok)
        self.assertIn("unavailable", reason)
        self.assertEqual(ch.fallback_descriptor().title, "小红书")
        with self.assertRaises(ChannelError):
            ch.native(["list"])  # 没有 native_command

    def test_not_executable(self):
        with tempfile.NamedTemporaryFile(dir=ROOT / "tests", suffix=".sh") as f:
            ch = ExternalChannel("t", {"command": [f.name]}, ROOT)
            with self.assertRaises(ChannelError) as cm:
                ch.describe()
            self.assertEqual(cm.exception.code, "unavailable")

    def test_native_passthrough(self):
        ch = ExternalChannel("t", {"command": ["true"], "native_command": [sys.executable, "-c", "import sys; sys.exit(len(sys.argv))"]}, ROOT)
        self.assertEqual(ch.native(["a", "b"]), 3)

    def test_resolve_arg(self):
        root = "/repo"
        self.assertEqual(resolve_arg("bin/eagle-xhs", root), "/repo/bin/eagle-xhs")
        self.assertEqual(resolve_arg("backends/xhs/config.yaml", root), "/repo/backends/xhs/config.yaml")
        for keep in ("channel", "-config", "/abs/path", "https://a.b/c", "-x=a/b"):
            self.assertEqual(resolve_arg(keep, root), keep)

    def test_bad_config(self):
        for cmd in (None, [], "bin/x", [1]):
            with self.assertRaises(ValueError):
                ExternalChannel("t", {"command": cmd}, ROOT)


if __name__ == "__main__":
    unittest.main()
