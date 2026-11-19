"""Reddit 注册与 CLI：仓库/安装模式配置、默认不参与、channels 输出。不访问网络。"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eagle_eye import cli
from eagle_eye.core.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


class RedditCliTest(unittest.TestCase):
    def test_repo_registration_is_default_false(self):
        reg = Registry.load(ROOT / "eagle.toml")
        self.assertIn("reddit", reg.names())
        self.assertFalse(reg.is_default("reddit"))
        self.assertNotIn("reddit", reg.default_names())
        reddit = reg.get("reddit")
        d = reddit.describe().to_dict()
        self.assertEqual(d["name"], "reddit")
        for key in ("subreddit", "author", "enrich", "cache"):
            self.assertIn(key, d["options"])

    def test_install_default_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.dict(os.environ, {"EAGLE_HOME": str(home)}, clear=True):
                reg = Registry.load()
                self.assertIn("reddit", reg.names())
                self.assertFalse(reg.is_default("reddit"))
                self.assertEqual(reg.get("reddit").describe().name, "reddit")

    def test_channels_json_shows_reddit_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / "eagle.toml"
            cfg.write_text(
                '[channels.reddit]\n'
                'type = "python"\n'
                'class = "eagle_eye.channels.reddit.adapter:RedditChannel"\n'
                'default = false\n',
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"EAGLE_HOME": str(home), "EAGLE_REDDIT_TAVILY_API_KEY": "dummy"},
                clear=True,
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--config", str(cfg), "channels", "--format", "json"])
            self.assertEqual(code, 0)
            row = json.loads(out.getvalue())[0]
            self.assertEqual(row["name"], "reddit")
            self.assertTrue(row["available"])
            self.assertFalse(row["default"])
            self.assertEqual(set(row["descriptor"]["options"]), {"subreddit", "author", "enrich", "cache"})
            self.assertEqual(err.getvalue(), "")

    def test_channels_json_without_key_still_shows_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / "eagle.toml"
            cfg.write_text(
                '[channels.reddit]\n'
                'type = "python"\n'
                'class = "eagle_eye.channels.reddit.adapter:RedditChannel"\n'
                'default = false\n',
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"EAGLE_HOME": str(home)}, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--config", str(cfg), "channels", "--format", "json"])
            self.assertEqual(code, 0)
            row = json.loads(out.getvalue())[0]
            self.assertFalse(row["available"])
            self.assertIn("auth_required", row["reason"])
            self.assertEqual(set(row["descriptor"]["options"]), {"subreddit", "author", "enrich", "cache"})

    def test_no_default_search_does_not_select_reddit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / "eagle.toml"
            cfg.write_text(
                '[channels.reddit]\n'
                'type = "python"\n'
                'class = "eagle_eye.channels.reddit.adapter:RedditChannel"\n'
                'default = false\n',
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"EAGLE_HOME": str(home), "EAGLE_REDDIT_TAVILY_API_KEY": "dummy"},
                clear=True,
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--config", str(cfg), "search", "q", "--format", "json"])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("没有默认参与搜索的渠道", err.getvalue())


if __name__ == "__main__":
    unittest.main()
