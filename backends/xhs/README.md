# eagle-xhs（小红书搜索渠道）

eagle_eye 的小红书渠道后端（Go）：按 JSON-over-stdio 协议给 Agent 提供通用的小红书关键词搜索。

- `eagle-xhs channel describe|search`：eagle_eye 渠道协议（见下文「作为 eagle_eye 渠道」）
- `eagle-xhs login`：打开浏览器，手动扫码登录一次

二进制构建到仓库根目录 `bin/eagle-xhs`（`make build`，即 `go build -o ../../bin/eagle-xhs .`）。单个 Go 二进制，纯 Go SQLite（无 cgo），只依赖一个 Chrome。

## 作为 eagle_eye 渠道

协议见仓库根目录 `docs/channel-protocol.md`。在 `eagle.toml` 中登记：

```toml
[channels.xhs]
type = "external"
command = ["bin/eagle-xhs", "channel", "-config", "backends/xhs/config.yaml"]
cwd = "backends/xhs"
timeout_seconds = 900
enabled = true
```

```bash
eagle-xhs channel [-config path] [-v] describe              # stdout: Descriptor
echo '{"protocol":1,"text":"Claude Code 教程","limit":20,"options":{"detail":3}}' \
  | eagle-xhs channel -config config.yaml search            # stdout: SearchResponse / 错误
make describe / make search Q='Claude Code 教程' DETAIL=3     # 同上（search 会真实访问小红书）
```

stdout 只输出一个 JSON 对象，日志全部在 stderr（`-v` 调试日志）；失败时 exit 1，stdout 仍是 `{"protocol":1,"error":{...}}`。`describe` 不读配置。

### 搜索流程

只接管 `browser.cdp_url` 指向的已运行 Chrome（连不上时按 `cdp_launch` 拉起；未配置 `cdp_url` 直接返回 `unavailable`，不会自己启动带 profile 的浏览器），**新开一个标签页**工作，结束时只关这个标签页；`silent` 模式下结束时窗口放回原处。

1. 打开首页确认已登录 → 停顿 → 搜索框逐字输入 + 回车
2. 只读页面已渲染的 `__INITIAL_STATE__` 里的卡片，不够 `limit` 时像人一样滚动（最多 `channel.max_scrolls` 次）
3. `detail`：在列表里点开排在最前的 N 篇，拟人阅读停留（`read_seconds`），读正文、发布时间、标签、IP 属地，Esc 关闭；**不点赞、不做任何互动**

| Query 字段 | 处理 |
|---|---|
| `text` | 必填，1–100 字；空/过长 → `bad_query` |
| `limit` | 默认 20，正整数；超过 50 按 50 处理（`meta.notes` 说明） |
| `options.detail` | 默认 3，0–10 的整数，超范围 → `bad_query`；每篇约 20–30 秒 |
| `since_hours` | 按发布时间过滤。卡片本身没有时间，但**小红书笔记 ID 是 ObjectId，前 8 位十六进制就是发布时间（Unix 秒）**——与详情页 `note.time` 对比过，精确到秒一致。所以未打开的笔记也能过滤；打开过的以详情时间为准；ID 格式不符（无法确定时间）的条目保留并在 `meta.notes` 里计数 |
| `sort` | `relevance`：小红书综合排序原序。`time` / `score`：在扫描到的综合排序结果内**本地重排**（未使用小红书的筛选面板，那需要点 UI 且无法离线验证），`meta.notes` 说明 |

Item 映射：`id`=noteId；`url` 带 `xsec_token`（`xsec_source=pc_search`）；`author{id,name,url}`；`metrics{likes,comments,collects}`（“1.2万”→12000，卡片上没有的为 `null`，`reposts`/`views` 恒为 `null`）；`score` = 赞 + 收藏（收藏未知时只算赞）；`created_at` 详情时间或由 ID 推算（`extra.created_at_source` = `detail` / `note_id`）；`extra`：`detail`（是否读了正文）、`note_type`、`tags`、`ip_location`、`shares`、`detail_error`。

### 防封约束（channel search 路径上全部生效）

| 约束 | 实现 | 返回 |
|---|---|---|
| 风控冷却 | 先查 kv `cooldown_until`；搜索中被重定向到验证页 → 进入冷却（`cooldown_hours` 默认 6h，连续触发翻倍，上限 48h，持久化，重启也不会跳过）；一次完整成功的搜索把连续触发计数 `cooldown_streak` 清零 | `cooldown` + `retry_after_seconds` |
| 登录失效 | 绝不自动登录，需人工 `eagle-xhs login` | `auth_required` |
| 浏览器 | 未配置 `cdp_url`、连不上、窗口最小化、Chrome 中途关闭 | `unavailable` |
| 浏览器互斥 | `data/browser.lock`（flock，进程退出自动释放）：`login` 与 `channel search` 任何时刻只有一个进程驱动浏览器；channel search 拿不到锁立即返回，不等待 | `busy` |
| 导航节流 | 最近一次页面导航时刻持久化在 kv `last_nav_at`，新进程接着它遵守 `nav_interval_seconds` / `nav_per_minute` | — |
| 总时限 | `channel.timeout_seconds`（默认 600） | `internal` |

渠道本身不限制每日搜索次数和两次搜索的间隔，只靠真实浏览器 + 拟人节奏（导航节流、随机停顿）+ 风控后熔断冷却来控制风险，因此不会返回 `rate_limited`。文中的耗时估计是保守的经验值，未经实测验证。

SQLite 多进程：DSN 先设 `busy_timeout(10000)` 再设 `journal_mode(WAL)`，多个进程同时开库不会报 `database is locked`（有并发单测）。数据库只有一张 `kv` 表（冷却截止时间、连续触发次数、最近导航时刻）。

配置里的相对路径（`db_path`、`user_data_dir`）都**相对配置文件所在目录**解析，无论从哪个 cwd 启动，同一份配置一定落到同一个数据库和同一把锁上。

## 防封设计：为什么不直接用签名接口方案

封号的主要原因是“看起来不像人”：无头浏览器、外部注入 cookie、每次请求直接跳 URL、高频调用、逆向签名直接打接口。本项目的做法：

| 风险点 | 本项目做法 |
|---|---|
| 浏览器指纹 | 接管你日常用的真实 Chrome（`cdp_url`），或有界面的真实 Chrome + 固定 profile。去掉 `enable-automation`，关闭 rod 的默认设备模拟（否则 UA/视口会被改写） |
| 登录 | 只在浏览器里手动扫码一次，登录态留在 profile 里；不读写、不注入 cookie |
| 请求方式 | 不调用任何接口、不算签名；只读页面已渲染的 `window.__INITIAL_STATE__` |
| 交互 | 真实鼠标轨迹、滚轮滚动、搜索框逐字输入、在列表里点开笔记、Esc 关弹窗；输入前确认标签页可见并拥有焦点，不伪造焦点，绝不向不可见页面发送合成输入 |
| 频率 | 跨进程导航节流（`nav_interval_seconds`、`nav_per_minute`）、随机停顿；不限制搜索次数，按需使用 |
| 风控信号 | 检测到验证页立刻熔断冷却 |

没有任何方案能保证不封号，**建议用小号**，并且一开始少量、低频地使用。

## 使用

```bash
cd backends/xhs
make build                             # = go build -o ../../bin/eagle-xhs .
cp config.example.yaml config.yaml     # 设置 browser.cdp_url 等

# 1. 登录（只需一次；在打开的 Chrome 窗口里扫码即可）
../../bin/eagle-xhs login -config config.yaml

# 2. 渠道元数据 / 一次搜索（真实访问小红书，有封号风险）
../../bin/eagle-xhs channel -config config.yaml describe
echo '{"protocol":1,"text":"露营装备推荐"}' | ../../bin/eagle-xhs channel -config config.yaml -v search
```

### 接管已有的 Chrome（channel search 必需）

```bash
# Linux / macOS
google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.xhs-chrome
# Windows（WSL2 下需开启 networkingMode=mirrored 才能用 127.0.0.1 访问）
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\xhs-chrome
```

在打开的 Chrome 里登录小红书（或执行 `eagle-xhs login`），然后在 `config.yaml` 里设置 `browser.cdp_url: http://127.0.0.1:9222`。程序只会新开一个标签页工作，退出时只关闭这个标签页。

也可以配置 `browser.cdp_launch`（启动命令 + 参数）：调试端口连不上时程序会自动执行它启动 Chrome，见 `config.example.yaml`。

> 注意：Chrome 新版本要求远程调试必须配合非默认的 `--user-data-dir`。

### 后台静默运行（silent）

`browser.silent: true` 时，程序把 Chrome 窗口移到屏幕外（不是无头、不是最小化），你看不到它，但它照常工作，指纹与有界面运行完全一致；结束时自动放回原处（被强杀时下次启动会修正）。`login` 不受影响，窗口正常显示。

前提：Chrome 必须带 `--disable-features=CalculateNativeWinOcclusion` 启动（`cdp_launch` 示例里已配置），否则 Windows 会把屏幕外/被遮挡的窗口判成不可见，Chrome 不再处理鼠标键盘事件。程序会自检，不满足时直接报错并提示：**把专用 Chrome（xhs-chrome）完全关闭，让程序按 `cdp_launch` 重新启动它**。

不要最小化这个窗口：最小化后页面不可见，channel search 返回 `unavailable`。

### WSL

不设置 `cdp_url` 时，`login` 会在 WSL 里启动 Chrome（需要 WSLg 显示窗口）。本机没有 Chrome 时 rod 会自动下载 Chromium，但更推荐 `sudo apt install google-chrome-stable`，或者用上面的方式接管 Windows 上的 Chrome。

## 目录

```
main.go                     CLI：channel / login
internal/channel            eagle_eye 渠道协议：Query 解析、搜索流程、Item 映射、错误码
internal/guard              冷却 / 节流的共享键名与判断（跨进程）
internal/lock               浏览器互斥文件锁（flock）
internal/config             配置与默认值
internal/xhs                浏览器启动/接管、拟人交互、导航节流、页面数据读取
internal/store              SQLite（kv）
```

## 页面结构变更

小红书前端改版时，最可能失效的是 `internal/xhs/client.go` 里的选择器和 `__INITIAL_STATE__` 路径。
