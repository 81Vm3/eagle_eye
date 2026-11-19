"""统一 CLI：--opt 解析、channels / search 输出与退出码、原生子命令分发。用假外部渠道，不访问网络。"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eagle_eye import cli

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fixtures" / "fake_channel.py"


def toml(nondefault=(), **modes):
    out = []
    for name, mode in modes.items():
        out.append(f'[channels.{name}]\ntype = "external"\ncommand = ["{sys.executable}", "{FAKE}", "{mode}"]\n'
                   + ("default = false\n" if name in nondefault else "")
                   + f'native_command = ["{sys.executable}", "-c", "import sys; sys.exit(0 if sys.argv[1:] == [\'list\', \'-n\', \'5\'] else 4)"]\n')
    return "\n".join(out)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, cfg, *argv):
        path = Path(self.tmp.name) / "eagle.toml"
        path.write_text(cfg)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--config", str(path), *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_parse_opts(self):
        self.assertEqual(cli.parse_opts(["x.pages=2", "keywords=a,b", "flag=true", "l=[1,2]", "s=a=b"]),
                         {"x.pages": 2, "keywords": "a,b", "flag": True, "l": [1, 2], "s": "a=b"})
        with self.assertRaises(ValueError):
            cli.parse_opts(["nokey"])

    def test_search_json_partial_failure(self):
        code, out, err = self.run_cli(toml(a="ok", b="error"), "search", "hello", "world", "--format", "json",
                                      "--opt", "a.n=3", "--limit", "10", "--hours", "6")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["query"]["text"], "hello world")
        self.assertEqual(len(d["items"]), 3)
        self.assertEqual(d["errors"]["b"]["code"], "cooldown")
        self.assertEqual(d["meta"]["a"]["query"]["options"], {"n": 3})
        self.assertEqual(d["meta"]["a"]["query"]["since_hours"], 6)
        self.assertIn("[b] 失败 cooldown", err)

    def test_search_markdown(self):
        code, out, _ = self.run_cli(toml(a="ok"), "search", "q", "-c", "a")
        self.assertEqual(code, 0)
        self.assertIn("# 搜索: q", out)
        self.assertIn("## 1. [a] t0", out)
        self.assertIn("https://example.com/q/0", out)
        self.assertIn("忽略参数: since_hours", out)

    def test_all_failed_exit_1(self):
        code, out, err = self.run_cli(toml(a="error", b="badjson"), "search", "q", "--format", "json")
        self.assertEqual(code, 1)
        self.assertEqual(set(json.loads(out)["errors"]), {"a", "b"})
        self.assertIn("全部渠道失败", err)

    def test_default_false_needs_explicit_channel(self):
        cfg = toml(nondefault=("slow",), a="ok", slow="ok")
        code, out, err = self.run_cli(cfg, "search", "q", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["channels"], ["a"])
        code, out, _ = self.run_cli(cfg, "search", "q", "-c", "slow", "--format", "json")
        self.assertEqual((code, json.loads(out)["channels"]), (0, ["slow"]))
        code, out, _ = self.run_cli(cfg, "search", "q", "-c", "a,slow", "--format", "json")
        self.assertEqual(json.loads(out)["channels"], ["a", "slow"])

    def test_no_default_channels_exit_2(self):
        code, _, err = self.run_cli(toml(nondefault=("slow",), slow="ok"), "search", "q")
        self.assertEqual(code, 2)
        self.assertIn("-c", err)

    def test_unknown_channel_and_bad_opt(self):
        self.assertEqual(self.run_cli(toml(a="ok"), "search", "q", "-c", "zzz")[0], 2)
        self.assertEqual(self.run_cli(toml(a="ok"), "search", "q", "--opt", "bad")[0], 2)

    def test_channels_json(self):
        cfg = toml(a="ok") + '\n[channels.gone]\ntype = "external"\ncommand = ["bin/nope"]\ntitle = "没了"\nenabled = false\n'
        code, out, _ = self.run_cli(cfg, "channels", "--format", "json")
        self.assertEqual(code, 0)
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertTrue(rows["a"]["available"])
        self.assertEqual(rows["a"]["descriptor"]["title"], "假渠道")
        self.assertFalse(rows["gone"]["available"])
        self.assertFalse(rows["gone"]["enabled"])
        self.assertTrue(rows["a"]["default"])
        self.assertFalse(rows["gone"]["default"])  # 未启用也不默认参与
        self.assertEqual(rows["gone"]["descriptor"]["title"], "没了")
        self.assertIn("unavailable", rows["gone"]["reason"])
        code, out, _ = self.run_cli(cfg, "channels")
        self.assertIn("❌ 不可用", out)
        self.assertIn("默认参与搜索: 是", out)

    def test_native_dispatch(self):
        code, _, _ = self.run_cli(toml(a="ok"), "a", "list", "-n", "5")
        self.assertEqual(code, 0)

    def test_x_native_topics(self):
        from eagle_eye.channels.x import topics

        tdir = Path(self.tmp.name) / "topics"
        tdir.mkdir()
        cfg = (ROOT / "eagle.toml").read_text()
        # 用户主题目录在 $EAGLE_HOME/x/topics，换成临时 EAGLE_HOME，不读真实用户主题
        with mock.patch.object(topics, "TOPICS_DIR", tdir), \
                mock.patch.dict(os.environ, {"EAGLE_HOME": str(Path(self.tmp.name) / "eagle_home")}):
            code, out, err = self.run_cli(cfg, "x", "topics")  # 空目录：stderr 提示，不报错
            self.assertEqual((code, out), (0, ""))
            self.assertIn("还没有预设主题", err)
            (tdir / "sample.toml").write_text('description = "示例主题"\nqueries = ["example"]\n')
            code, out, _ = self.run_cli(cfg, "x", "topics")
        self.assertEqual(code, 0)
        self.assertEqual(out, "sample\t示例主题\n")


if __name__ == "__main__":
    unittest.main()
