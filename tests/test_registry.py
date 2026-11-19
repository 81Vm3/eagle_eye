"""registry：读 eagle.toml、按 type 构造、构造失败降级为 BrokenChannel。"""

import os
import tempfile
import unittest
from pathlib import Path

from eagle_eye.channels.external import ExternalChannel
from eagle_eye.channels.x.adapter import XChannel
from eagle_eye.core.channel import BrokenChannel
from eagle_eye.core.models import ChannelError
from eagle_eye.core.registry import ROOT, Registry

TOML = """
[channels.x]
type = "python"
class = "eagle_eye.channels.x.adapter:XChannel"

[channels.ext]
type = "external"
command = ["bin/eagle-nothing", "channel"]
cwd = "sub"
enabled = false

[channels.badtype]
type = "nope"
default = false

[channels.badclass]
type = "python"
class = "eagle_eye.core.models:Query"

[channels.badimport]
type = "python"
class = "no_such_module:X"
"""


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "eagle.toml"
        self.path.write_text(TOML)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_and_build(self):
        reg = Registry.load(self.path)
        self.assertEqual(reg.names(), ["x", "ext", "badtype", "badclass", "badimport"])
        self.assertNotIn("ext", reg.enabled_names())
        # default 缺省为 true；enabled=false 的渠道也不算默认
        self.assertEqual(reg.default_names(), ["x", "badclass", "badimport"])
        self.assertFalse(reg.is_default("ext"))
        self.assertFalse(reg.is_default("badtype"))
        self.assertIsInstance(reg.get("x"), XChannel)
        ext = reg.get("ext")
        self.assertIsInstance(ext, ExternalChannel)
        self.assertEqual(ext.command[0], str(Path(self.tmp.name).resolve() / "bin/eagle-nothing"))
        self.assertEqual(ext.cwd, str(Path(self.tmp.name).resolve() / "sub"))
        self.assertIs(reg.get("x"), reg.get("x"))  # 缓存

    def test_broken_channels_degrade(self):
        reg = Registry.load(self.path)
        for name in ("badtype", "badclass", "badimport"):
            ch = reg.get(name)
            self.assertIsInstance(ch, BrokenChannel)
            ok, reason = ch.status()
            self.assertFalse(ok)
            self.assertIn("unavailable", reason)
            with self.assertRaises(ChannelError):
                ch.search(None)

    def test_unknown_and_missing(self):
        reg = Registry.load(self.path)
        with self.assertRaises(KeyError):
            reg.get("zzz")
        with self.assertRaises(FileNotFoundError):
            Registry.load(Path(self.tmp.name) / "none.toml")

    def test_env_override(self):
        old = os.environ.get("EAGLE_CONFIG")
        os.environ["EAGLE_CONFIG"] = str(self.path)
        try:
            self.assertEqual(Registry.load().path, self.path)
        finally:
            if old is None:
                os.environ.pop("EAGLE_CONFIG")
            else:
                os.environ["EAGLE_CONFIG"] = old

    def test_repo_config(self):
        reg = Registry.load(ROOT / "eagle.toml")
        self.assertEqual(reg.names()[:2], ["x", "xhs"])
        xhs = reg.get("xhs")
        self.assertEqual(xhs.command[:2], [str(ROOT / "bin/eagle-xhs"), "channel"])
        self.assertEqual(xhs.command[2:], ["-config", "config.yaml"])  # 相对子进程 cwd，不做转换
        self.assertEqual(xhs.cwd, str(ROOT / "backends/xhs"))
        self.assertEqual(xhs.timeout, 900)
        self.assertEqual(reg.default_names(), ["x"])  # xhs 必须显式 -c xhs
        self.assertTrue(reg.is_enabled("xhs"))


if __name__ == "__main__":
    unittest.main()
