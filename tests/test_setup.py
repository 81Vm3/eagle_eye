"""eagle setup：假 Release（file:// 和本地 http.server）下载、sha256 校验、幂等、init、--check；
eagle x extension / eagle skill install。全部在临时目录，不访问网络。"""

import contextlib
import functools
import hashlib
import http.server
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import eagle_eye
from eagle_eye import cli, paths, setup_cmd, skill_cmd
from eagle_eye.channels.x import cli as xcli

# 假 eagle-xhs：version 输出版本号；init -config P 写一份带 cdp_url 的配置；channel describe 输出合法 Descriptor
FAKE_XHS = f"""#!{sys.executable}
import json, sys
VERSION = "@VERSION@"
a = sys.argv[1:]
if a[:1] == ["version"]:
    print(VERSION)
elif a[:1] == ["init"]:
    open(a[a.index("-config") + 1], "w").write("browser:\\n  cdp_url: http://127.0.0.1:9\\n")
elif a[:1] == ["channel"] and a[-1] == "describe":
    print(json.dumps({{"protocol": 1, "name": "xhs", "title": "fake"}}))
else:
    sys.exit(3)
"""


def make_release(root, version, tamper=False, assets=None):
    """在 root 下建假 Release：<当前平台资产> + checksums.txt。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    asset = setup_cmd.asset_name()
    data = FAKE_XHS.replace("@VERSION@", version).encode()
    (root / asset).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if tamper:
        (root / asset).write_bytes(data + b"# tampered\n")
    lines = [f"{'0' * 64}  eagle-xhs-other-arch", f"{digest}  {asset}"]
    if assets is not None:
        lines = assets
    (root / "checksums.txt").write_text("\n".join(lines) + "\n")
    return asset


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*a, **kw)
    return code, out.getvalue(), err.getvalue()


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.rel = self.dir / "release"
        self.env = mock.patch.dict(os.environ, {
            "EAGLE_HOME": str(self.home),
            "EAGLE_XHS_BASE_URL": self.rel.as_uri(),
        })
        self.env.start()
        os.environ.pop("EAGLE_CONFIG", None)
        self.version = eagle_eye.__version__

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parse_checksums(self):
        h = "a" * 64
        self.assertEqual(setup_cmd.parse_checksums(f"{h}  f1\n{h} *f2\nbad line\n"), {"f1": h, "f2": h})

    def test_setup_downloads_verifies_and_inits(self):
        make_release(self.rel, self.version)
        code, out, err = quiet(setup_cmd.run_setup)
        self.assertEqual(code, 0, err)
        binary = paths.xhs_binary_path(self.home)
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertEqual(setup_cmd.binary_version(binary), self.version)
        cfg = self.home / "xhs" / "config.yaml"
        self.assertIn("cdp_url", cfg.read_text())
        for d in ("bin", "xhs", "x"):
            self.assertTrue((self.home / d).is_dir())
        self.assertIn("eagle x extension", out)
        self.assertIn("eagle xhs login", out)
        self.assertIn("eagle search -c xhs", out)
        self.assertNotIn("eagle xhs browser  ", out)  # 不再是必经步骤，只在说明里提到
        self.assertIn("eagle skill install", out)
        self.assertFalse(list((self.home / "bin").glob("*.download")))

        # 幂等：版本一致时不再下载（把 Release 删掉也能成功），已有配置不覆盖
        cfg.write_text("browser:\n  cdp_url: ''\n# mine\n")
        for f in self.rel.iterdir():
            f.unlink()
        code, _, err = quiet(setup_cmd.run_setup)
        self.assertEqual(code, 0, err)
        self.assertIn("跳过下载", err)
        self.assertIn("# mine", cfg.read_text())

    def test_same_file_skips_download_even_if_version_differs(self):
        make_release(self.rel, "dev")  # 二进制自报 dev，与包版本不同
        quiet(setup_cmd.run_setup)
        binary = paths.xhs_binary_path(self.home)
        mtime = binary.stat().st_mtime_ns
        code, _, err = quiet(setup_cmd.run_setup)
        self.assertEqual(code, 0, err)
        self.assertIn("sha256 一致", err)
        self.assertEqual(binary.stat().st_mtime_ns, mtime)

    def test_checksum_mismatch_keeps_old_binary(self):
        make_release(self.rel, self.version, tamper=True)
        binary = paths.xhs_binary_path(self.home)
        binary.parent.mkdir(parents=True)
        binary.write_text("old")
        with self.assertRaises(setup_cmd.SetupError) as cm:
            quiet(setup_cmd.run_setup)
        msg = str(cm.exception)
        self.assertIn("sha256 校验失败", msg)
        self.assertIn(f"releases/download/v{self.version}/{setup_cmd.asset_name()}", msg)
        self.assertEqual(binary.read_text(), "old")
        self.assertFalse(list(binary.parent.glob("*.download")))

    def test_asset_missing_from_checksums(self):
        make_release(self.rel, self.version, assets=["b" * 64 + "  something-else"])
        with self.assertRaises(setup_cmd.SetupError) as cm:
            quiet(setup_cmd.run_setup)
        self.assertIn("checksums.txt 里没有", str(cm.exception))

    def test_http_404_clear_error_and_cli_exit_1(self):
        self.rel.mkdir()
        handler = functools.partial(QuietHandler, directory=str(self.rel))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            os.environ["EAGLE_XHS_BASE_URL"] = f"http://127.0.0.1:{srv.server_address[1]}/v9.9.9/"
            code, _, err = quiet(cli.main, ["setup", "--version", "v9.9.9"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)
        self.assertIn("releases/download/v9.9.9/", err)  # 手动下载地址
        self.assertFalse(paths.xhs_binary_path(self.home).exists())

    def test_http_download_ok(self):
        make_release(self.rel, "1.2.3")
        handler = functools.partial(QuietHandler, directory=str(self.rel))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            os.environ["EAGLE_XHS_BASE_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
            code, _, err = quiet(cli.main, ["setup", "--version", "1.2.3"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 0, err)
        self.assertEqual(setup_cmd.binary_version(paths.xhs_binary_path(self.home)), "1.2.3")

    def test_check_does_not_modify(self):
        code, out, _ = quiet(cli.main, ["setup", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("eagle-xhs", out)
        self.assertIn("运行 eagle setup", out)
        self.assertFalse(self.home.exists())  # --check 不创建任何东西

    def test_check_after_setup(self):
        make_release(self.rel, self.version)
        quiet(setup_cmd.run_setup)
        before = sorted(p.name for p in self.home.rglob("*"))
        code, out, _ = quiet(cli.main, ["setup", "--check"])
        self.assertEqual(code, 0, out)
        self.assertIn("与包版本一致", out)
        self.assertIn("渠道 xhs: 可用", out)
        self.assertIn("http://127.0.0.1:9", out)  # 读取 config 里的 cdp_url 并探测（连不上只是提示）
        self.assertIn("体检通过", out)
        self.assertEqual(before, sorted(p.name for p in self.home.rglob("*")))

    def test_check_version_mismatch_warns(self):
        make_release(self.rel, "0.0.1")
        quiet(setup_cmd.run_setup, version="0.0.1")
        code, out, _ = quiet(setup_cmd.run_check)
        self.assertEqual(code, 0)
        self.assertIn("版本 0.0.1", out)

    def test_read_cdp_url(self):
        p = self.dir / "c.yaml"
        p.write_text("browser:\n  # cdp_url: http://x\n  cdp_url: \"http://127.0.0.1:9333\"  # c\n")
        self.assertEqual(setup_cmd.read_cdp_url(p), "http://127.0.0.1:9333")
        p.write_text("browser:\n  cdp_url: \"\"\n")
        self.assertEqual(setup_cmd.read_cdp_url(p), "")
        self.assertIsNone(setup_cmd.read_cdp_url(self.dir / "missing.yaml"))


    def _setup_with_config(self, text):
        make_release(self.rel, self.version)
        quiet(setup_cmd.run_setup)
        (self.home / "xhs" / "config.yaml").write_text(text)

    def test_check_headless_finds_chrome_skips_cdp(self):
        self._setup_with_config("browser:\n  headless: true\n  chrome_path: \"\"\n  cdp_url: http://127.0.0.1:9\n")
        with mock.patch.object(setup_cmd, "probe_cdp") as probe, \
                mock.patch.object(setup_cmd, "find_chrome", return_value=("/usr/bin/google-chrome", "PATH")):
            code, out, _ = quiet(setup_cmd.run_check)
        self.assertEqual(code, 0, out)
        probe.assert_not_called()
        self.assertIn("[✓] Chrome（无头模式）: /usr/bin/google-chrome", out)
        self.assertNotIn("调试端口", out)

    @contextlib.contextmanager
    def _chrome_env(self, candidates=(), on_path=None):
        """完全隔离 run_check 里的 Chrome 查找：平台默认位置只用 candidates，PATH 查找只认 on_path（{命令名: 路径}）。
        注意 find_chrome 的 which 默认参数在定义时绑定了 shutil.which，patch shutil.which 无效，必须注入。"""
        on_path = dict(on_path or {})
        which = mock.Mock(side_effect=on_path.get)
        real = setup_cmd.find_chrome
        with mock.patch.object(setup_cmd, "chrome_candidates", return_value=[str(c) for c in candidates]), \
                mock.patch.object(setup_cmd, "find_chrome", functools.partial(real, which=which)):
            yield which

    def _fake_chrome(self, name="chrome"):
        exe = self.dir / "chrome-bin" / name
        exe.parent.mkdir(exist_ok=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        return exe

    def test_check_headless_no_chrome_warns_with_hint(self):
        self._setup_with_config("browser:\n  headless: true\n")
        with mock.patch.object(setup_cmd, "probe_cdp") as probe, \
                self._chrome_env(candidates=[self.dir / "no-such-chrome"]) as which:
            code, out, _ = quiet(setup_cmd.run_check)
        self.assertTrue(which.called)  # 确实走到了（被隔离的）PATH 查找
        self.assertEqual(code, 0, out)  # 小红书是可选渠道：找不到 Chrome 只警告，不影响 exit code
        probe.assert_not_called()
        self.assertIn("[!] Chrome（无头模式）", out)
        self.assertIn("体检通过", out)
        self.assertIn("请安装 Chrome", out)
        self.assertIn("browser.chrome_path", out)

    def test_check_headless_chrome_on_path_ok(self):
        exe = self._fake_chrome("google-chrome")
        self._setup_with_config("browser:\n  headless: true\n")
        with mock.patch.object(setup_cmd, "probe_cdp") as probe, \
                self._chrome_env(on_path={"chromium": str(exe)}):
            code, out, _ = quiet(setup_cmd.run_check)
        self.assertEqual(code, 0, out)
        probe.assert_not_called()
        self.assertIn(f"[✓] Chrome（无头模式）: {exe}（PATH", out)

    def test_check_headless_chrome_platform_default_ok(self):
        exe = self._fake_chrome()
        self._setup_with_config("browser:\n  headless: true\n")
        with mock.patch.object(setup_cmd, "probe_cdp") as probe, \
                self._chrome_env(candidates=[self.dir / "missing", exe]) as which:
            code, out, _ = quiet(setup_cmd.run_check)
        self.assertEqual(code, 0, out)
        probe.assert_not_called()
        which.assert_not_called()  # 平台默认位置优先于 PATH
        self.assertIn(f"[✓] Chrome（无头模式）: {exe}（平台默认位置", out)

    def test_check_headless_false_probes_cdp(self):
        self._setup_with_config("browser:\n  headless: false\n  cdp_url: http://127.0.0.1:9\n")
        with mock.patch.object(setup_cmd, "probe_cdp", return_value=(False, "连不上")) as probe, \
                mock.patch.object(setup_cmd, "find_chrome") as find:
            code, out, _ = quiet(setup_cmd.run_check)
        self.assertEqual(code, 0, out)
        probe.assert_called_once_with("http://127.0.0.1:9")
        find.assert_not_called()
        self.assertIn("[!] Chrome 调试端口: http://127.0.0.1:9", out)
        self.assertIn("eagle xhs browser", out)

    def test_read_browser_config(self):
        p = self.dir / "c.yaml"
        p.write_text(
            "db_path: data/xhs.db\n"
            "browser:\n"
            "  # headless: false\n"
            "  headless: true   # 注释\n"
            "  chrome_path: '/opt/My Chrome/chrome'\n"
            "  cdp_url: http://127.0.0.1:9222\n"
            "  cdp_launch:\n"
            "    - google-chrome\n"
            "    - --headless: x\n"
            "  silent: false\n"
            "channel:\n"
            "  headless: false\n")
        b = setup_cmd.read_browser_config(p)
        self.assertEqual(b["headless"], "true")
        self.assertEqual(b["chrome_path"], "/opt/My Chrome/chrome")
        self.assertEqual(b["cdp_url"], "http://127.0.0.1:9222")
        self.assertIsNone(b["cdp_launch"])
        self.assertNotIn("--headless", b)
        self.assertIsNone(setup_cmd.read_browser_config(self.dir / "missing.yaml"))

    def test_resolve_headless_compat(self):
        r = setup_cmd.resolve_headless
        self.assertTrue(r({"headless": "true", "cdp_url": "http://x"})[0])  # 显式为准（init 生成的配置）
        self.assertFalse(r({"headless": "False"})[0])
        self.assertFalse(r({"headless": "no"})[0])
        self.assertFalse(r({"cdp_url": "http://127.0.0.1:9222"})[0])  # 未写 + 有 cdp_url：旧配置
        self.assertTrue(r({"cdp_url": None})[0])
        self.assertTrue(r({})[0])
        self.assertTrue(r(None)[0])
        v, why = r({"headless": "maybe"})
        self.assertTrue(v)
        self.assertIn("无法识别", why)

    def test_find_chrome_order(self):
        exe = self.dir / "chrome"
        exe.write_text("")
        which = mock.Mock(side_effect=lambda n: f"/usr/bin/{n}" if n == "chromium" else None)
        # chrome_path 优先（相对路径按配置目录解析）
        self.assertEqual(setup_cmd.find_chrome("chrome", base=self.dir, plat="linux", which=which), (str(exe), "browser.chrome_path"))
        # chrome_path 写了但不存在：直接失败，不再往下找
        path, detail = setup_cmd.find_chrome(str(self.dir / "nope"), plat="linux", which=which)
        self.assertIsNone(path)
        self.assertIn("不存在", detail)
        # 平台默认位置 > PATH
        with mock.patch.object(setup_cmd, "chrome_candidates", return_value=[str(self.dir / "missing"), str(exe)]):
            self.assertEqual(setup_cmd.find_chrome(None, plat="darwin", which=which), (str(exe), "平台默认位置"))
        # Linux：PATH 按 google-chrome、google-chrome-stable、chromium… 顺序
        self.assertEqual(setup_cmd.find_chrome("", plat="linux", which=which), ("/usr/bin/chromium", "PATH"))
        self.assertEqual([c.args[0] for c in which.call_args_list][-3:], ["google-chrome", "google-chrome-stable", "chromium"])
        path, detail = setup_cmd.find_chrome(None, plat="linux", which=lambda n: None)
        self.assertIsNone(path)
        self.assertIn("microsoft-edge", detail)

    def test_chrome_candidates(self):
        mac = setup_cmd.chrome_candidates("darwin", {"HOME": "/Users/u"})
        self.assertEqual(mac[0], "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.assertEqual(mac[1], "/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.assertTrue(mac[-1].endswith("Microsoft Edge"))
        win = setup_cmd.chrome_candidates("win32", {})
        self.assertEqual(win[0], r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertEqual(setup_cmd.chrome_candidates("linux", {}), [])


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class ProbeCdpTest(unittest.TestCase):
    def serve(self, body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_chrome_ok(self):
        url = self.serve(b'{"Browser": "Chrome/140", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}')
        self.assertEqual(setup_cmd.probe_cdp(url), (True, "Chrome/140"))

    def test_not_chrome(self):
        ok, detail = setup_cmd.probe_cdp(self.serve(b'{"Browser": "x"}'))
        self.assertFalse(ok)
        self.assertIn("webSocketDebuggerUrl", detail)
        self.assertFalse(setup_cmd.probe_cdp(self.serve(b"<html>"))[0])

    def test_silent_port_times_out(self):
        import socket
        import time

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)  # 只 listen 不应答
        self.addCleanup(s.close)
        t = time.monotonic()
        ok, _ = setup_cmd.probe_cdp(f"http://127.0.0.1:{s.getsockname()[1]}", timeout=0.5)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t, 5)


class ExtensionSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extension_prints_path(self):
        code, out, _ = quiet(xcli.main, ["extension"])
        self.assertEqual(code, 0)
        self.assertTrue((Path(out.strip()) / "manifest.json").is_file())

    def test_extension_copy_to(self):
        dest = self.dir / "a" / "ext"
        code, out, err = quiet(xcli.main, ["extension", "--copy-to", str(dest)])
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(out.strip()), dest.resolve())
        src = paths.bundled("extensions", "x-cookie")
        self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted(p.name for p in src.iterdir() if p.is_file()))
        # 只覆盖同名文件
        (dest / "extra.txt").write_text("keep")
        quiet(xcli.main, ["extension", "--copy-to", str(dest)])
        self.assertTrue((dest / "extra.txt").exists())

    def test_extension_open_uses_platform_opener(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(xcli.subprocess, "call") as call:
            code, out, _ = quiet(xcli.main, ["extension", "--open"])
        self.assertEqual(code, 0)
        self.assertEqual(call.call_args[0][0], ["open", out.strip()])

    def test_extension_open_linux_with_and_without_xdg_open(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(xcli.subprocess, "call") as call, \
                mock.patch.object(xcli.shutil, "which", side_effect=lambda n: "/x/xdg-open" if n == "xdg-open" else None):
            code, out, _ = quiet(xcli.main, ["extension", "--open"])
        self.assertEqual(code, 0)
        self.assertEqual(call.call_args[0][0], ["/x/xdg-open", out.strip()])
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(xcli.subprocess, "call") as call, \
                mock.patch.object(xcli.shutil, "which", return_value=None):
            code, _, err = quiet(xcli.main, ["extension", "--open"])
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("手动打开", err)

    def test_skill_install_symlink(self):
        target = self.dir / "skills"
        code, out, err = quiet(cli.main, ["skill", "install", "--target", str(target)])
        self.assertEqual(code, 0, err)
        dest = target / "eagle-search"
        self.assertEqual(Path(out.strip()), dest)
        self.assertTrue(dest.is_symlink())
        self.assertTrue((dest / "SKILL.md").is_file())
        # 再装一次：同一链接 → 提示已安装
        code, _, err = quiet(cli.main, ["skill", "install", "--target", str(target)])
        self.assertEqual(code, 0)
        self.assertIn("已安装", err)

    def test_skill_install_copy_and_no_overwrite(self):
        target = self.dir / "skills"
        code, _, _ = quiet(skill_cmd.install, target, copy=True)
        self.assertEqual(code, 0)
        dest = target / "eagle-search"
        self.assertFalse(dest.is_symlink())
        (dest / "SKILL.md").write_text("mine")
        code, _, err = quiet(skill_cmd.install, target)
        self.assertEqual(code, 1)
        self.assertIn("不覆盖", err)
        self.assertEqual((dest / "SKILL.md").read_text(), "mine")

    def test_skill_default_target(self):
        with mock.patch.object(Path, "home", return_value=self.dir):
            self.assertEqual(skill_cmd.default_target(), self.dir / ".agents" / "skills")

    def _install_with_home(self, argv):
        home = self.dir / "home"
        with mock.patch.object(Path, "home", return_value=home):
            code, out, err = quiet(cli.main, ["skill", "install", *argv])
        return home, code, out, err

    def test_skill_install_default_is_agents_dir(self):
        home, code, out, err = self._install_with_home([])
        self.assertEqual(code, 0, err)
        dest = home / ".agents" / "skills" / "eagle-search"
        self.assertEqual(Path(out.strip()), dest)
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), skill_cmd.skill_dir())
        self.assertFalse((home / ".claude").exists())

    def test_skill_install_agent_presets(self):
        home, code, out, err = self._install_with_home(["--agent", "claude", "--copy"])
        self.assertEqual(code, 0, err)
        dest = home / ".claude" / "skills" / "eagle-search"
        self.assertEqual(Path(out.strip()), dest)
        self.assertFalse(dest.is_symlink())
        self.assertTrue((dest / "SKILL.md").is_file())
        self.assertFalse((home / ".agents").exists())
        home, code, out, _ = self._install_with_home(["--agent", "agents"])
        self.assertEqual(Path(out.strip()), home / ".agents" / "skills" / "eagle-search")

    def test_skill_install_target_and_agent_conflict(self):
        # argparse 互斥 / 未知预设 → exit 2，不创建任何目录
        for argv in (["--agent", "claude", "--target", str(self.dir / "x")], ["--agent", "unknown"]):
            with self.assertRaises(SystemExit) as cm:
                self._install_with_home(argv)
            self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.dir / "x").exists())
        self.assertFalse((self.dir / "home").exists())
        # 直接调用时同样拒绝
        code, _, _ = quiet(skill_cmd.install, self.dir / "x", agent="claude")
        self.assertEqual(code, 2)

    def test_skill_install_existing_dir_not_overwritten(self):
        dest = self.dir / "skills" / "eagle-search"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("other")
        code, out, err = quiet(cli.main, ["skill", "install", "--target", str(self.dir / "skills")])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("不覆盖", err)
        self.assertFalse(dest.is_symlink())
        self.assertEqual((dest / "SKILL.md").read_text(), "other")

    def test_skill_path(self):
        code, out, _ = quiet(cli.main, ["skill", "path"])
        self.assertEqual(code, 0)
        p = Path(out.strip())
        self.assertEqual(p, skill_cmd.skill_dir())
        self.assertTrue((p / "SKILL.md").is_file())

    def test_skill_show_prints_body_without_frontmatter(self):
        code, out, _ = quiet(cli.main, ["skill", "show"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# eagle-search\n\n"))
        self.assertNotIn("\n---\n", out.split("## 1.")[0])
        self.assertNotIn("name: eagle-search", out)
        self.assertIn("eagle channels", out)
        self.assertIn("eagle search", out)
        body = (skill_cmd.skill_dir() / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n")
        self.assertTrue(out.endswith(body))

    def test_split_frontmatter(self):
        self.assertEqual(skill_cmd.split_frontmatter("---\nname: a\ndescription: d\n---\n\nbody\n"), ("d", "body\n"))
        self.assertEqual(skill_cmd.split_frontmatter("no fm\n"), ("", "no fm\n"))

    def test_skill_md_frontmatter_follows_agent_skills_spec(self):
        """agentskills.io/specification：只用标准字段；name 规则；description ≤ 1024；compatibility ≤ 500。"""
        text = (skill_cmd.skill_dir() / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        fm = text.split("---", 2)[1].strip().splitlines()
        fields = dict(line.split(":", 1) for line in fm)
        fields = {k: v.strip() for k, v in fields.items()}
        allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
        self.assertLessEqual(set(fields), allowed)
        name = fields["name"]
        self.assertEqual(name, skill_cmd.skill_dir().name)
        self.assertLessEqual(len(name), 64)
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertTrue(0 < len(fields["description"]) <= 1024)
        if "compatibility" in fields:
            self.assertTrue(0 < len(fields["compatibility"]) <= 500)
        # 正文与具体 Agent 无关：没有 Claude 专有说法或 "! 命令" 语法
        self.assertNotRegex(text, r"(?i)claude")
        self.assertNotIn("`! ", text)


if __name__ == "__main__":
    unittest.main()
