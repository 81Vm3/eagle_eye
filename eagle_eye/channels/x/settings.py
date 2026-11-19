"""X 渠道的环境变量：EAGLE_X_<名> 优先，兼容原 xsearch 的 XSEARCH_<名>。

| 名 | 默认 | 含义 |
|---|---|---|
| BACKEND | auto | twscrape / twitterapi / auto |
| INTERVAL | 5.5 | twitterapi.io 请求间隔（秒；本项目客户端参数，免费档平台限速 0.2 QPS） |
| TWS_DB | ~/.config/xsearch/twscrape.db（存在时沿用），否则 $EAGLE_HOME/x/twscrape.db | twscrape 账号库 |
| TWS_INTERVAL | 2 | twscrape 请求间隔（秒；本项目客户端参数，不是 X 的限额） |
| TWS_WAIT | 60 | 所有账号被锁定时最多等待（秒；本项目客户端参数，不是 X 的限额） |
| COOKIE_PORT | 18631 | cookie 接收服务端口 |
"""

import os


def env(name, default=None):
    for key in (f"EAGLE_X_{name}", f"XSEARCH_{name}"):
        v = os.environ.get(key)
        if v:
            return v
    return default
