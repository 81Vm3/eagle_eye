"""X 渠道的环境变量：EAGLE_X_<名> 优先，兼容原 xsearch 的 XSEARCH_<名>。

| 名 | 默认 | 含义 |
|---|---|---|
| BACKEND | auto | twscrape / twitterapi / auto |
| INTERVAL | 5.5 | twitterapi.io 请求间隔（秒） |
| TWS_DB | ~/.config/xsearch/twscrape.db | twscrape 账号库（沿用原路径，已有 cookie 不用重新登录） |
| TWS_INTERVAL | 2 | twscrape 请求间隔（秒） |
| TWS_WAIT | 60 | 所有账号被限速时最多等待（秒） |
| COOKIE_PORT | 18631 | cookie 接收服务端口 |
"""

import os


def env(name, default=None):
    for key in (f"EAGLE_X_{name}", f"XSEARCH_{name}"):
        v = os.environ.get(key)
        if v:
            return v
    return default
