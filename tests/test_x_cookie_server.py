"""cookie 接收服务测试：只监听本机，不访问外网，不需要 twscrape。"""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from eagle_eye.channels.x import cookie_server

GOOD = {"auth_token": "a" * 40, "ct0": "b" * 32, "twid": "u%3D42"}


class ValidateTest(unittest.TestCase):
    def test_account_name(self):
        self.assertEqual(cookie_server.account_name("u%3D1234"), "x_1234")
        self.assertEqual(cookie_server.account_name(None), "x_default")

    def test_validate(self):
        self.assertEqual(cookie_server.validate(GOOD), ("a" * 40, "b" * 32, "x_42"))
        for bad in [None, {}, {"auth_token": "a" * 40}, {"auth_token": "a;b" * 10, "ct0": "b" * 32}]:
            with self.assertRaises(ValueError):
                cookie_server.validate(bad)


class ServeTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"XSEARCH_COOKIE_PORT": "18699"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("EAGLE_X_COOKIE_PORT", None)  # 用户环境里的 EAGLE_X_ 优先级更高
        self.saved = []

        async def fake_save(name, auth, ct0, db=None):
            self.saved.append((name, auth, ct0))

        self.result = []
        self.t = threading.Thread(
            target=lambda: self.result.extend(cookie_server.serve(timeout=10, log=lambda m: None, save=fake_save))
        )
        self.t.start()
        time.sleep(0.3)

    def post(self, body, origin):
        req = urllib.request.Request(
            "http://127.0.0.1:18699/cookie", data=json.dumps(body).encode(), headers={"Origin": origin}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_flow(self):
        with urllib.request.urlopen("http://127.0.0.1:18699/ping", timeout=5) as r:
            self.assertTrue(json.load(r)["want"])
        self.assertEqual(self.post(GOOD, "https://evil.example")[0], 403)
        self.assertEqual(self.post({"ct0": "x"}, "chrome-extension://id")[0], 400)
        self.assertEqual(self.post({"ct0": "x"}, "extension://id")[0], 400)  # Edge 风格的 Origin 也放行
        code, body = self.post(GOOD, "chrome-extension://id")
        self.assertEqual((code, body["account"]), (200, "x_42"))
        self.t.join(5)
        self.assertFalse(self.t.is_alive())  # 收到一次就退出
        self.assertEqual(self.result, ["x_42"])
        self.assertEqual(self.saved, [("x_42", "a" * 40, "b" * 32)])
