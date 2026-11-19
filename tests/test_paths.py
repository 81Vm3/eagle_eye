"""paths：EAGLE_HOME 各平台默认、配置查找优先级、{home}/{exe} 展开、包内资源。不访问网络。"""

import os
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import eagle_eye
from eagle_eye import paths
from eagle_eye.channels.x import topics, tws
from eagle_eye.core.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
HOME = Path("/h/user")


class EagleHomeTest(unittest.TestCase):
    def test_env_wins(self):
        for plat in ("darwin", "linux", "win32"):
            self.assertEqual(paths.eagle_home({"EAGLE_HOME": "/x/y"}, plat, HOME), Path("/x/y"))

    def test_platform_defaults(self):
        self.assertEqual(paths.eagle_home({}, "darwin", HOME), HOME / "Library/Application Support/eagle_eye")
        self.assertEqual(paths.eagle_home({}, "linux", HOME), HOME / ".config/eagle_eye")
        self.assertEqual(paths.eagle_home({"XDG_CONFIG_HOME": "/xdg"}, "linux", HOME), Path("/xdg/eagle_eye"))
        self.assertEqual(paths.eagle_home({"APPDATA": "/appdata"}, "win32", HOME), Path("/appdata/eagle_eye"))
        self.assertEqual(paths.eagle_home({}, "win32", HOME), HOME / "AppData/Roaming/eagle_eye")
        # 其他类 Unix 平台按 Linux 规则
        self.assertEqual(paths.eagle_home({}, "freebsd14", HOME), HOME / ".config/eagle_eye")

    def test_monkeypatched_sys_platform(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=HOME):
            self.assertEqual(paths.eagle_home(), HOME / "Library/Application Support/eagle_eye")
            self.assertEqual(paths.exe_suffix(), "")
            self.assertEqual(paths.xhs_binary_path(), HOME / "Library/Application Support/eagle_eye/bin/eagle-xhs")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"APPDATA": "/ad"}, clear=True):
            self.assertEqual(paths.exe_suffix(), ".exe")
            self.assertEqual(paths.xhs_binary_path(), Path("/ad/eagle_eye/bin/eagle-xhs.exe"))

    def test_goos_goarch(self):
        self.assertEqual(paths.goos_goarch("darwin", "arm64"), ("darwin", "arm64"))
        self.assertEqual(paths.goos_goarch("darwin", "x86_64"), ("darwin", "amd64"))
        self.assertEqual(paths.goos_goarch("linux", "aarch64"), ("linux", "arm64"))
        self.assertEqual(paths.goos_goarch("win32", "AMD64"), ("windows", "amd64"))
        with self.assertRaises(RuntimeError):
            paths.goos_goarch("linux", "riscv64")


class ConfigLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_priority(self):
        env = {"EAGLE_HOME": str(self.home)}
        self.assertEqual(paths.config_path(env), (paths.DEFAULT_CONFIG, "包内默认"))
        (self.home / "eagle.toml").write_text("")
        self.assertEqual(paths.config_path(env), (self.home / "eagle.toml", "EAGLE_HOME"))
        other = Path(self.tmp.name) / "other.toml"
        self.assertEqual(paths.config_path({**env, "EAGLE_CONFIG": str(other)}), (other, "EAGLE_CONFIG"))

    def test_expand(self):
        h = Path("/eh")
        self.assertEqual(paths.expand("{home}/bin/eagle-xhs{exe}", h, "darwin"), "/eh/bin/eagle-xhs")
        self.assertEqual(paths.expand("{home}/bin/eagle-xhs{exe}", h, "win32"), "/eh/bin/eagle-xhs.exe")
        self.assertEqual(paths.expand(["{home}/x", "channel", "{other}"], h, "linux"), ["/eh/x", "channel", "{other}"])
        self.assertEqual(paths.expand(5, h), 5)

    def test_default_config_expanded_by_registry(self):
        with mock.patch.dict(os.environ, {"EAGLE_HOME": str(self.home)}):
            os.environ.pop("EAGLE_CONFIG", None)
            reg = Registry.load()
            self.assertEqual(reg.path, paths.DEFAULT_CONFIG)
            self.assertEqual(reg.names(), ["x", "xhs"])
            xhs = reg.get("xhs")
            exe = "eagle-xhs" + paths.exe_suffix()
            self.assertEqual(xhs.command, [str(self.home / "bin" / exe), "channel", "-config", "config.yaml"])
            self.assertEqual(xhs.native_command, [str(self.home / "bin" / exe)])
            self.assertEqual(xhs.cwd, str(self.home / "xhs"))
            self.assertFalse(reg.is_default("xhs"))
            # 二进制不存在：不崩溃，describe 报 unavailable
            from eagle_eye.core.models import ChannelError
            with self.assertRaises(ChannelError) as cm:
                xhs.describe()
            self.assertEqual(cm.exception.code, "unavailable")

    def test_repo_config_unchanged(self):
        reg = Registry.load(ROOT / "eagle.toml")
        self.assertEqual(reg.get("xhs").command[0], str(ROOT / "bin" / "eagle-xhs"))
        self.assertEqual(reg.get("xhs").cwd, str(ROOT / "backends" / "xhs"))


class MiscTest(unittest.TestCase):
    def test_version_matches_pyproject(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            self.assertEqual(tomllib.load(f)["project"]["version"], eagle_eye.__version__)

    def test_bundled_resources_found(self):
        self.assertTrue((paths.bundled("extensions", "x-cookie") / "manifest.json").is_file())
        self.assertTrue((paths.bundled("skills", "eagle-search") / "SKILL.md").is_file())
        with self.assertRaises(FileNotFoundError):
            paths.bundled("nope")

    def test_source_root_detected(self):
        self.assertEqual(paths.source_root(), ROOT)
        self.assertEqual(paths.runs_dir(), ROOT / "runs")

    def test_twscrape_db_default(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "legacy.db"
            env = {"EAGLE_HOME": str(Path(d) / "home")}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(tws.default_db(legacy), Path(d) / "home" / "x" / "twscrape.db")
                legacy.write_text("")
                self.assertEqual(tws.default_db(legacy), legacy)
            with mock.patch.dict(os.environ, {**env, "XSEARCH_TWS_DB": "/a.db"}, clear=True):
                self.assertEqual(tws.db_path(), Path("/a.db"))
            with mock.patch.dict(os.environ, {**env, "XSEARCH_TWS_DB": "/a.db", "EAGLE_X_TWS_DB": "/b.db"}, clear=True):
                self.assertEqual(tws.db_path(), Path("/b.db"))

    def test_user_topics_dir(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"EAGLE_HOME": d}):
            tdir = Path(d) / "x" / "topics"
            tdir.mkdir(parents=True)
            (tdir / "mine.toml").write_text('queries = ["a"]\ndescription = "d"\n')
            self.assertIn("mine", topics.names())
            self.assertEqual(topics.load("mine")["description"], "d")

    def test_docs_have_no_personal_paths(self):
        for rel in ("README.md", "AGENTS.md", "Makefile", "skills/eagle-search/SKILL.md", "docs/distribution.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"/mnt/c/Users/p3g4s|C:\\\\Users\\\\p3g4s|~/eagle_eye/bin/eagle", text), rel)


if __name__ == "__main__":
    unittest.main()
