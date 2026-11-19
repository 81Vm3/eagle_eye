"""本地 cookie 接收服务：配合 chrome-extension/ 把浏览器里 x.com 的登录 cookie 导入 twscrape 账号库。

只监听 127.0.0.1，只接受浏览器扩展（chrome-extension:// / extension://）的请求（网页无法伪造 Origin），
cookie 只写进账号库，不打印、不落其他文件。
"""

import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import tws
from .settings import env

DEFAULT_PORT = 18631
EXT_ORIGINS = ("chrome-extension://", "extension://")
TOKEN_RE = re.compile(r"^[A-Za-z0-9%_\-]{10,512}$")


def port():
    return int(env("COOKIE_PORT") or DEFAULT_PORT)


def account_name(twid):
    """twid cookie 形如 u%3D1234567890 → x_1234567890；拿不到就用 x_default。"""
    m = re.search(r"u=(\d+)", unquote(twid or ""))
    return f"x_{m.group(1)}" if m else "x_default"


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("需要 JSON 对象")
    auth, ct0 = payload.get("auth_token"), payload.get("ct0")
    if not (isinstance(auth, str) and TOKEN_RE.match(auth)):
        raise ValueError("auth_token 缺失或格式不对（是否已登录 x.com？）")
    if not (isinstance(ct0, str) and TOKEN_RE.match(ct0)):
        raise ValueError("ct0 缺失或格式不对")
    return auth, ct0, account_name(payload.get("twid"))


async def save_account(name, auth, ct0, db=None):
    from twscrape import API

    db = db or tws.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    api = API(str(db))
    await api.pool.add_account_cookies(name, f"auth_token={auth}; ct0={ct0}")


def make_handler(on_saved, save=save_account, wanted=lambda: True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 默认日志会打到 stderr，这里静默
            pass

        def _allowed(self):
            # Chrome 是 chrome-extension://，Edge 也用这个，保险起见同时接受 extension://
            return (self.headers.get("Origin") or "").startswith(EXT_ORIGINS)

        def _reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self._allowed():
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/ping":
                return self._reply(404, {"ok": False})
            # app 名保持 "xsearch"：已安装的浏览器扩展靠它识别接收服务
            self._reply(200, {"ok": True, "app": "xsearch", "want": wanted()})

        def do_POST(self):
            if self.path != "/cookie":
                return self._reply(404, {"ok": False})
            if not self._allowed():
                return self._reply(403, {"ok": False, "error": "只接受浏览器扩展的请求"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8192:
                    raise ValueError("请求太大")
                auth, ct0, name = validate(json.loads(self.rfile.read(length) or b"null"))
                asyncio.run(save(name, auth, ct0))
            except (ValueError, json.JSONDecodeError) as e:
                return self._reply(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self._reply(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            self._reply(200, {"ok": True, "account": name})
            on_saved(name)

    return Handler


def serve(timeout=300, keep=False, log=print, save=save_account):
    """启动服务等扩展推送 cookie。keep=False 时收到一次就退出；返回导入的账号名列表。"""
    saved = []
    done = threading.Event()

    def on_saved(name):
        saved.append(name)
        log(f"已导入账号 {name} → {tws.db_path()}")
        if not keep:
            done.set()

    srv = ThreadingHTTPServer(("127.0.0.1", port()), make_handler(on_saved, save, wanted=lambda: not saved))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log(f"等待 Chrome 扩展推送 cookie（http://127.0.0.1:{port()}）…")
    log("扩展会在 30 秒内自动推送；也可以点扩展图标里的「立即同步」。")
    try:
        done.wait(None if keep else timeout)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return saved
