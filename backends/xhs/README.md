# eagle-xhs（小红书搜索渠道）

eagle_eye 的小红书渠道后端（Go）：按 JSON-over-stdio 协议给 Agent 提供通用的小红书关键词搜索。

- `eagle-xhs channel describe|search`：eagle_eye 渠道协议（见下文「作为 eagle_eye 渠道」）
- `eagle-xhs login`：打开**有界面**的 Chrome（专用 profile），手动扫码登录一次，登录后关闭窗口
- `eagle-xhs browser [-config path] [-timeout 30s]`：启动带远程调试端口的专用 Chrome（端口已可连时直接返回），端口就绪后退出，Chrome 继续运行。
  只用于 `browser.headless: false`（接管模式）或调试；**默认的 headless 模式不需要它**
- `eagle-xhs init [-config path]`：写入适合当前平台的默认配置（文件已存在时不覆盖，exit 0）
- `eagle-xhs version`：stdout 首行只输出版本号（如 `0.1.0`；本地未注入时为 `dev` 或 `git describe` 结果）

开发时二进制构建到仓库根目录 `bin/eagle-xhs`（`make build`，版本由 `git describe` 注入）。发布版由 GitHub Release 提供（见「发布」）。单个 Go 二进制，纯 Go SQLite（无 cgo），只依赖一个 Chrome。

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

### 浏览器模式（browser.headless）

| | `headless: true`（默认） | `headless: false`（接管模式） |
|---|---|---|
| channel search | 用专用 profile（`user_data_dir`，默认 `data/chrome-profile`）自己启动 `--headless=new` 的 Chrome，**不弹窗口**，用完关闭 Chrome | 接管 `cdp_url` 上已运行的专用 Chrome（连不上时按 `cdp_launch` 拉起；未配置 `cdp_url` → `unavailable`），新开一个标签页工作，结束只关这个标签页 |
| 需要 `eagle xhs browser` / `cdp_url` | 不需要 | 需要 |
| login | 有界面 Chrome + 同一个专用 profile（不接管 `cdp_url`），登录后关闭窗口 | 在 `cdp_url` 的 Chrome 里新开标签页扫码（未配置 `cdp_url` 时用 `user_data_dir` 启动有界面 Chrome） |
| silent / 窗口可见性检测 | 不适用，自动跳过 | 生效 |

兼容规则：配置里**显式写了** `headless` 以它为准；**没写**时，配置了 `cdp_url` 的旧配置按 `false` 处理（保持原来接管 Chrome 的用法），否则按 `true`。
`eagle-xhs init` 和 `config.example.yaml` 都显式写 `headless: true`；旧版 init 生成的配置里写的是 `headless: false`，仍按接管模式运行，想改用无头模式把它改成 `true` 即可（登录态在同一个专用 profile 里，无需重新登录）。

无头模式要点：

- 浏览器：`browser.chrome_path` > 本平台默认位置（与 init 的查找顺序相同）> PATH；**不使用 `cdp_launch[0]`**（WSL 里那可能是 Windows 的 chrome.exe），找不到直接 `unavailable`，不自动下载 Chromium。
- 专用 profile 同一时刻只能被一个 Chrome 使用：`eagle xhs browser` 启动的有界面实例开着时，无头搜索与 login 都会报“专用 Chrome profile 正被另一个 Chrome 使用”（search 为 `unavailable`），关掉它再试。
  login 与 channel search 之间照旧由 `data/browser.lock` 互斥（`busy`）。
- 结束时正常关闭 Chrome（等它把登录态写回 profile，超时 10 秒才强杀），绝不删除 profile 目录。

### 搜索流程（快速模式）

**不模拟真人节奏**：没有首页停留、逐字输入、随机停顿、阅读停留、拟人鼠标轨迹，也没有跨进程导航节流；所有等待都是事件驱动的
（轮询页面 `__INITIAL_STATE__`，数据就绪立即继续，各步骤有超时上限）。代价是**风控/封号风险高于拟人操作**。

按上面的模式拿到浏览器后：

1. 直接打开搜索结果页 `https://www.xiaohongshu.com/search_result?keyword=<encodeURIComponent(关键词)>&source=web_explore_feed`，
   轮询 `__INITIAL_STATE__.search` 直到出现与关键词对应的卡片（上限 20 秒；超时时未登录 → `auth_required`）。
   无结果：`searchValue` 已与关键词一致且页面表明加载完成（`hasMore === false` 且不在加载中，或出现无结果标识，**该判断未实测**）→ 立即返回空 items；
   判断不出来 → 等满 20 秒后同样返回空 items。两种情况都在 `meta.notes` 说明，不报错
2. 只读 `__INITIAL_STATE__` 里的卡片；不够 `limit` 时滚动到页面底部，等新卡片出现（上限 6 秒，没有新卡片即停止），最多 `channel.max_scrolls` 次
3. `detail`：用卡片的笔记 URL（带 `xsec_token`，`xsec_source=pc_search`）在**新标签页**直接打开前 N 篇，最多并行 `channel.detail_concurrency`（默认 3）个，
   轮询 `__INITIAL_STATE__.note` 读到正文、发布时间、标签、IP 属地后立即关闭标签页（每篇上限 15 秒）；**不点赞、不做任何互动**。
   任一标签页被重定向到验证页 → 立即取消其余所有标签页，整次搜索返回 `cooldown`

耗时估计（**未实测**，不含 Chrome 冷启动）：detail=0 约 5–10 秒，detail=3 约 10–20 秒。

| Query 字段 | 处理 |
|---|---|
| `text` | 必填，1–100 字；空/过长 → `bad_query` |
| `limit` | 默认 20，正整数；超过 50 按 50 处理（`meta.notes` 说明） |
| `options.detail` | 默认 3，0–10 的整数，超范围 → `bad_query`；并行读取（`channel.detail_concurrency`） |
| `since_hours` | 按发布时间过滤。卡片本身没有时间，但**小红书笔记 ID 是 ObjectId，前 8 位十六进制就是发布时间（Unix 秒）**——与详情页 `note.time` 对比过，精确到秒一致。所以未打开的笔记也能过滤；打开过的以详情时间为准；ID 格式不符（无法确定时间）的条目保留并在 `meta.notes` 里计数 |
| `sort` | `relevance`：小红书综合排序原序。`time` / `score`：在扫描到的综合排序结果内**本地重排**（未使用小红书的筛选面板，那需要点 UI 且无法离线验证），`meta.notes` 说明 |

Item 映射：`id`=noteId；`url` 带 `xsec_token`（`xsec_source=pc_search`）；`author{id,name,url}`；`metrics{likes,comments,collects}`（“1.2万”→12000，卡片上没有的为 `null`，`reposts`/`views` 恒为 `null`）；`score` = 赞 + 收藏（收藏未知时只算赞）；`created_at` 详情时间或由 ID 推算（`extra.created_at_source` = `detail` / `note_id`）；`extra`：`detail`（是否读了正文）、`note_type`、`tags`、`ip_location`、`shares`、`detail_error`。

### 风控约束（channel search 路径上全部生效）

| 约束 | 实现 | 返回 |
|---|---|---|
| 风控冷却 | 先查 kv `cooldown_until`；搜索中被重定向到验证页 → 进入冷却（`cooldown_hours` 默认 6h，连续触发翻倍，上限 48h，持久化，重启也不会跳过）；一次完整成功的搜索把连续触发计数 `cooldown_streak` 清零 | `cooldown` + `retry_after_seconds` |
| 登录失效 | 绝不自动登录，需人工 `eagle-xhs login` | `auth_required` |
| 浏览器 | 无头：找不到 Chrome、专用 profile 被占用、启动失败；接管：未配置 `cdp_url`、连不上、窗口最小化；两种模式下 Chrome 中途关闭 | `unavailable` |
| 浏览器互斥 | `data/browser.lock`（flock，进程退出自动释放）：`login` 与 `channel search` 任何时刻只有一个进程驱动浏览器；channel search 拿不到锁立即返回，不等待 | `busy` |
| 总时限 | `channel.timeout_seconds`（默认 120） | `internal` |

渠道本身不限制每日搜索次数和两次搜索的间隔，也不再做导航节流或拟人停顿，只靠真实浏览器 + 风控后熔断冷却来控制风险，因此不会返回 `rate_limited`。文中的耗时是估计值，未经实测验证。

SQLite 多进程：DSN 先设 `busy_timeout(10000)` 再设 `journal_mode(WAL)`，多个进程同时开库不会报 `database is locked`（有并发单测）。数据库只有一张 `kv` 表（冷却截止时间、连续触发次数；旧版本留下的 `last_nav_at` 已不再读写，可忽略）。

配置里的相对路径（`db_path`、`user_data_dir`）都**相对配置文件所在目录**解析，无论从哪个 cwd 启动，同一份配置一定落到同一个数据库和同一把锁上。

## 风控设计：为什么不直接用签名接口方案

封号的主要信号：无头浏览器、外部注入 cookie、高频调用、逆向签名直接打接口、每次请求直接跳 URL。本项目现在是**快速模式**，
在速度和风险之间偏向速度——保留浏览器指纹、登录方式、只读页面数据这几项，放弃了拟人交互和节流：

| 风险点 | 本项目做法 |
|---|---|
| 浏览器指纹 | 默认：本机真实 Chrome 的 `--headless=new`（新版无头，与有界面同一内核；不用旧 headless）+ 固定 profile；UA 里的 `HeadlessChrome` 换成同版本的 `Chrome`（版本、系统取自浏览器本身），`userAgentData` / `sec-ch-ua*` 用浏览器自己报告的值（见下；搜索页和并行的详情标签页都套用）；窗口固定 1440×900、屏幕 1920×1080。接管模式：有界面的真实 Chrome（`cdp_url`），详情标签页在后台打开。两种模式都去掉 `enable-automation`、关闭 `AutomationControlled`，关闭 rod 的默认设备模拟（否则 UA/视口会被改写） |
| 登录 | 只在浏览器里手动扫码一次，登录态留在 profile 里；不读写、不注入 cookie |
| 请求方式 | 不调用任何接口、不算签名；只读页面已渲染的 `window.__INITIAL_STATE__` |
| 交互 | **不模拟真人**：直接导航到搜索结果页和笔记页（URL 直达），程序化滚动到底部，详情页多标签页并行打开；不发送鼠标键盘输入 |
| 频率 | **无节流**：没有导航间隔、每分钟上限和随机停顿；不限制搜索次数。一次 detail=3 的搜索在十几秒内加载约 4 个页面 |
| 风控信号 | 检测到验证页立刻熔断冷却，并行的标签页全部中止 |

UA 改写细节（无头模式）：工作标签页用 CDP `Emulation.setUserAgentOverride`。UA 字符串 = `Browser.getVersion` 的 UA 把 `HeadlessChrome/` 换成 `Chrome/`；
`userAgentData`（brands、fullVersionList、platformVersion、architecture、bitness…）先在临时标签页 `chrome://version`（浏览器内置页、安全上下文、不联网，读完即关）读浏览器自己报告的值——
新版 headless 的品牌列表本来就是 `"Google Chrome"`，原样使用；读不到或含 `HeadlessChrome` 时按 Chromium 的 GREASE 算法按主版本号生成（单测对照了 120/124/131/138/157 的真实值）。
`navigator.platform`、Accept-Language 不改（无头时本来就是真实值，与 login 用的同一个 profile 一致）。
本机用 Chrome 157 `--headless=new` 在 about:blank / chrome:// 页面上验证过：`navigator.userAgent` 不含 Headless、`navigator.webdriver=false`、brands 与有界面一致、屏幕 1920×1080、窗口 1440×900、`visibilityState=visible`。

无头仍可能被识别的点（未实测小红书侧是否检测）：WebGL/GPU 渲染器可能与有界面不同（无头常走软件渲染）；Worker / Service Worker 里的 `navigator.userAgent` 不经过页面级 UA 覆盖，仍可能是 `HeadlessChrome`；
字体、插件列表、`window.chrome` 等细节与有界面 Chrome 可能有差异。介意的话把 `browser.headless` 设为 `false` 用接管模式。

快速模式的风险高于旧版拟人模式：URL 直达、无停顿、多标签页并行都比真人操作更容易被识别。没有任何方案能保证不封号，**务必用小号**，并且少量、低频地使用；触发 `cooldown` 后不要重试。

## macOS 使用（安装模式）

1. `eagle setup`：自动下载本平台的 `eagle-xhs` 到 `$EAGLE_HOME/bin/`，并执行 `eagle-xhs init` 生成
   `$EAGLE_HOME/xhs/config.yaml`（macOS 的 `EAGLE_HOME` 默认是 `~/Library/Application Support/eagle_eye`）。**无需手动配置。**
   生成的配置：`headless: true`；浏览器依次查找
   `/Applications/Google Chrome.app`、`~/Applications/Google Chrome.app`、`/Applications/Chromium.app`、`/Applications/Microsoft Edge.app`；
   专用 profile 在 `$EAGLE_HOME/xhs/data/chrome-profile`，与你日常用的 Chrome 互不影响。
2. 首次使用 `eagle xhs login`：弹出有界面的专用 Chrome，扫码登录小红书，登录后窗口自动关闭；登录态保存在专用 profile 里，之后无需再登录。
3. `eagle search -c xhs '关键词'`：在后台启动无头 Chrome 搜索，不弹窗口、不占 Dock。**不需要 `eagle xhs browser`。**

macOS 注意事项（只适用于 `headless: false` 的接管模式）：

- `eagle xhs browser` 启动带调试端口的专用 Chrome（Dock 里会多一个 Chrome 图标）；channel search 连不上调试端口时也会按 `cdp_launch` 自动启动它。
- 不要最小化专用 Chrome 窗口（最小化后页面不可见，channel search 返回 `unavailable`）；被其他窗口挡住、切到别的桌面没关系
  （`cdp_launch` 带了 `--disable-backgrounding-occluded-windows` 等参数）。
- `browser.silent` 在 macOS 上默认关闭：静默模式把窗口移到屏幕外，macOS 的窗口管理可能把窗口拉回屏幕内，这一点没有实机验证。
  开启后如果窗口被拉回，程序只打 warning 并照常搜索（窗口可见），建议关掉 silent。
- 浏览器锁（flock）、SQLite（纯 Go）在 macOS 上与 Linux 行为相同。

Windows 未做专门适配：二进制可以构建，但浏览器文件锁目前只支持 unix（Windows 上 login / channel search 会报锁不可用），推荐在 WSL 中使用。

## 发布

推送 tag `vX.Y.Z`（与 `pyproject.toml` 的 version 一致）后，`.github/workflows/release.yml` 用 `CGO_ENABLED=0` 交叉编译
`eagle-xhs-<goos>-<goarch>[.exe]`（darwin-arm64、darwin-amd64、linux-amd64、linux-arm64、windows-amd64）和 `checksums.txt`（`sha256sum` 格式），
上传到 GitHub Release，版本号 = tag 去掉 `v`。本地验证同样的产物：

```bash
make dist VERSION=0.1.0     # → dist/（不入库）
cd dist && sha256sum -c checksums.txt
```

## 使用（开发模式，仓库内）

```bash
cd backends/xhs
make build                             # → ../../bin/eagle-xhs
../../bin/eagle-xhs init -config config.yaml   # 生成本平台默认配置（已存在不覆盖）；或 cp config.example.yaml config.yaml

# 1. 登录（只需一次；在打开的 Chrome 窗口里扫码即可，登录后窗口关闭）
../../bin/eagle-xhs login -config config.yaml

# 2. 渠道元数据 / 一次搜索（真实访问小红书，有封号风险）
../../bin/eagle-xhs channel -config config.yaml describe
echo '{"protocol":1,"text":"露营装备推荐"}' | ../../bin/eagle-xhs channel -config config.yaml -v search
```

### 接管已有的 Chrome（`headless: false`）

默认的无头模式不需要本节。想看着浏览器工作、或调试时，把 `browser.headless` 设为 `false`：

最简单的是 `eagle-xhs browser -config config.yaml`（按 `cdp_launch`，未配置时按本平台默认启动）。手动启动：

```bash
# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.xhs-chrome
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/.xhs-chrome"
# Windows（WSL2 下需开启 networkingMode=mirrored 才能用 127.0.0.1 访问）
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\xhs-chrome
```

在打开的 Chrome 里登录小红书（或执行 `eagle-xhs login`），然后在 `config.yaml` 里设置 `browser.cdp_url: http://127.0.0.1:9222`。程序只会新开一个标签页工作，退出时只关闭这个标签页。

也可以配置 `browser.cdp_launch`（启动命令 + 参数）：调试端口连不上时程序会自动执行它启动 Chrome，见 `config.example.yaml`。

> 注意：Chrome 新版本要求远程调试必须配合非默认的 `--user-data-dir`。

### 后台静默运行（silent）

只在 `headless: false` 时有意义（无头模式没有窗口，自动忽略）。`browser.silent: true` 时，程序把 Chrome 窗口移到屏幕外（不是无头、不是最小化），你看不到它，但它照常工作，指纹与有界面运行完全一致；结束时自动放回原处（被强杀时下次启动会修正）。`login` 不受影响，窗口正常显示。

只在 Windows 上验证过；macOS 见上文「macOS 使用」（默认关闭）。

前提：Windows 上 Chrome 必须带 `--disable-features=CalculateNativeWinOcclusion` 启动（`eagle-xhs init` 在 Windows 上生成的 `cdp_launch` 已包含），否则 Windows 会把屏幕外/被遮挡的窗口判成不可见，Chrome 不再处理鼠标键盘事件。程序会自检，不满足时直接报错并提示：**把专用 Chrome（xhs-chrome）完全关闭，让程序按 `cdp_launch` 重新启动它**。

不要最小化这个窗口：最小化后页面不可见，channel search 返回 `unavailable`。

### WSL

无头模式在 WSL 里启动 Linux 版 Chrome（`sudo apt install google-chrome-stable`；找不到 Chrome 时报错，不自动下载 Chromium），`login` 需要 WSLg 显示窗口。
也可以 `headless: false` 用上面的方式接管 Windows 上的 Chrome（`cdp_url` + `cdp_launch`）。

## 目录

```
main.go                     CLI：channel / login / browser / init / version
internal/channel            eagle_eye 渠道协议：Query 解析、搜索流程、Item 映射、错误码
internal/guard              风控冷却的共享键名与判断（跨进程）
internal/lock               浏览器互斥文件锁（flock）
internal/config             配置与默认值
internal/setup              平台默认值：查找 Chrome、cdp_launch 参数、init 生成的默认配置
internal/xhs                浏览器启动/接管（browser.go：三种模式；headless.go：专用 profile 启动无头/有界面 Chrome、UA 改写、profile 占用检测；
                            launch.go：调试端口探测、脱离进程启动 Chrome；client.go：搜索页/详情页直达与 __INITIAL_STATE__ 就绪轮询）
internal/store              SQLite（kv）
```

## 页面结构变更

小红书前端改版时，最可能失效的是 `internal/xhs/client.go` 里的搜索结果页 URL 形式、`__INITIAL_STATE__` 路径（`search.feeds` / `search.searchValue`、`note.noteDetailMap`）和登录判断选择器。
