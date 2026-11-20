# eagle_eye

给 Agent 用的统一搜索/信息中间层：一条命令并发搜索多个信息渠道（目前是 **X (Twitter)**、**小红书** 和 **Reddit**），
结果统一成同一种格式、合并去重，供人阅读（Markdown）或 Agent 处理（JSON）。以后会接更多渠道。

由原 X 搜索（Python）和小红书（Go）两个项目合并而来，只保留通用搜索能力。

## macOS 快速开始

```bash
# 1. 装 uv（已有可跳过；也可 brew install uv）
curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. 安装 eagle 命令（Python ≥ 3.11，uv 会自动准备；twscrape 一并装好）
uv tool install git+https://github.com/81Vm3/eagle_eye
# 3. 一步准备：建 EAGLE_HOME（~/Library/Application Support/eagle_eye）、下载并校验小红书渠道二进制、生成小红书配置
eagle setup
# 4. X：打印并打开 cookie 同步扩展目录 → Chrome 扩展页（chrome://extensions）打开「开发者模式」→「加载已解压的扩展程序」选该目录
eagle x extension --open
#    浏览器里登录 x.com，再运行（扩展会自动推送 cookie）
eagle x login
# 5. 小红书（可选，需本机装有 Chrome）：首次扫码登录（弹出一次专用 Chrome 窗口，登录后自动关闭），之后在后台无头搜索
eagle xhs login
eagle search -c xhs '露营装备' --limit 5
# 6. 让你的 Agent 用上 eagle（见下文「给 Agent」）：支持 Agent Skills 的工具装 Skill（默认软链到 ~/.agents/skills/eagle-search；
#    Claude Code 用 --agent claude，其他工具按其文档用 --target DIR）
eagle skill install
# 体检 / 看渠道
eagle setup --check
eagle channels
```

`eagle` 不在 PATH 时运行 `uv tool update-shell` 后重开终端。升级：`uv tool upgrade eagle-eye && eagle setup`。
数据目录、配置查找顺序、Release 资产等约定见 [docs/distribution.md](docs/distribution.md)。

## Linux 快速开始

```bash
# 1. 装 uv（已有可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. 安装 eagle 命令（Python ≥ 3.11，uv 会自动准备；twscrape 一并装好）
uv tool install git+https://github.com/81Vm3/eagle_eye
# 3. 一步准备：建 EAGLE_HOME（${XDG_CONFIG_HOME:-~/.config}/eagle_eye）、下载并校验小红书渠道二进制、生成小红书配置
eagle setup
# 4. X：打印扩展目录（有图形界面时 --open 用 xdg-open 打开）→ Chrome 扩展页（chrome://extensions）打开「开发者模式」
#    →「加载已解压的扩展程序」选该目录；浏览器里登录 x.com，再运行（扩展会自动推送 cookie）
eagle x extension --open
eagle x login
# 5. 小红书（可选）：先装 Chrome（见下方），首次扫码登录（弹出一次专用 Chrome 窗口，登录后自动关闭），之后在后台无头搜索
eagle xhs login
eagle search -c xhs '露营装备' --limit 5
# 6. 让你的 Agent 用上 eagle（见下文「给 Agent」）
eagle skill install
# 体检 / 看渠道
eagle setup --check
eagle channels
```

小红书渠道需要本机有 Chrome / Chromium（找不到时报 `unavailable`，不会自动下载），任选其一：

```bash
# Debian / Ubuntu（x86_64）：Google Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
# Fedora
sudo dnf install chromium
# 装在非标准位置时，在 $EAGLE_HOME/xhs/config.yaml 设置 browser.chrome_path
```

- Google Chrome 没有 Linux arm64 版，arm64 机器请装发行版的 Chromium。
- `eagle xhs login` 需要图形界面（桌面会话或 WSLg）；没有图形界面的服务器无法扫码登录，搜索本身是无头的。
- 只用 X 渠道的话不需要 Chrome，`eagle setup --check` 里 Chrome 那一项会显示 `!`，不影响使用。

### WSL

- 按上面的 Linux 步骤在 WSL 里装 Chrome 并 `eagle xhs login`（WSLg 会弹出窗口），无头搜索在 WSL 里运行。
- 用 Windows 上的 Chrome/Edge 装 X 扩展时，浏览器不能直接加载 WSL 路径，复制一份出去再加载：
  `eagle x extension --copy-to /mnt/c/Users/<用户名>/eagle-x-cookie`（改了扩展后重新复制，再在扩展页点「重新加载」）。
  X 的 cookie 接收服务监听 `127.0.0.1:18631`，需要 WSL 开启 mirrored 网络，Windows 上的浏览器才能访问到。
- mirrored 网络下 WSL 与 Windows 共用 `127.0.0.1`：若改用接管模式（`browser.headless: false` + `cdp_url`），
  调试端口不要用 9222——Windows 上日常 Chrome 开了远程调试时会占用它。

Windows 原生暂不支持（预留了 `.exe` 资产和 `%APPDATA%\eagle_eye`，尚未适配）。

## 渠道说明

- **X**：默认用 twscrape（你自己的 X 账号 cookie，建议小号）。账号库：已用过原 `x_resource_search` 的继续用 `~/.config/xsearch/twscrape.db`，
  不用重新登录；否则在 `$EAGLE_HOME/x/twscrape.db`。`XSEARCH_*` 环境变量继续有效，也可用 `EAGLE_X_*`。
  也可以改用付费的 twitterapi.io：`export TWITTERAPI_IO_KEY=...`。
- **小红书**：配置在 `$EAGLE_HOME/xhs/config.yaml`（`eagle setup` 生成）。默认 `browser.headless: true`：搜索时用专用 profile
  （`xhs/data/chrome-profile`）在后台启动无头 Chrome，用完关闭，不弹窗口；`eagle xhs login` 用同一个 profile 打开有界面窗口扫码。
  无头 Chrome 仍可能被平台识别（未实测），介意的话设 `headless: false`，改为接管 `eagle xhs browser` 启动的有界面 Chrome。
  报「不可用」（`unavailable`）的常见原因：找不到 Chrome（安装它，或设置 `browser.chrome_path`）；专用 profile 被占用（关掉 `eagle xhs browser` 打开的窗口）。
  详见 `backends/xhs/README.md`。
- **Reddit**：默认不参与搜索，必须显式 `-c reddit`。全站关键词走 Tavily Search API（`include_domains=["reddit.com"]`），
  指定 `subreddit` 或 `author` 时优先走免费的 Arctic Shift（不消耗 Tavily 额度）；Tavily 结果还会用 Arctic Shift 补全
  author、score、评论数、时间和正文。Arctic Shift（及 Tavily）不可用时返回协议错误码，不会伪装结果。
  Reddit 是非官方数据接口，字段可能缺失或不完整；使用时遵守 Tavily 条款和 Reddit 内容政策。

配置 Tavily key（二选一；环境变量优先）：

```bash
export EAGLE_REDDIT_TAVILY_API_KEY=tvly-...   # 只放当前会话

# 或写入 $EAGLE_HOME/reddit/config.toml（建议 chmod 600，不要提交到 git）
mkdir -p "$EAGLE_HOME/reddit"
cat > "$EAGLE_HOME/reddit/config.toml" <<'EOF'
api_key = "tvly-..."
cache_ttl_hours = 24
degraded_cache_ttl_hours = 1   # 回退/补全失败/author 被忽略的结果只缓存 1 小时
enrich_timeout_seconds = 5     # Arctic 补全单独用短超时
EOF
chmod 600 "$EAGLE_HOME/reddit/config.toml"
```

没有 Tavily key 时，普通全站查询报 `auth_required`；只带 `subreddit`/`author` 的 Arctic 查询仍可用。
结果缓存在 `$EAGLE_HOME/reddit/cache/`（默认 24 小时，`cache_ttl_hours = 0` 关闭），命令里的 `--opt cache=false`
只跳过本次读取、结果仍会写回缓存。降级结果（Arctic 回退 Tavily、补全失败、Tavily 无法按 `author` 过滤）使用
`degraded_cache_ttl_hours`（默认 1 小时）并在 `meta.degraded` 标记；`since_hours` 查询命中缓存后会按当前时间重新过滤。

## 用法

```bash
eagle channels                                       # 有哪些渠道、是否可用、查询语法、代价
eagle search 'AI Agent' --limit 10                   # 默认渠道（目前只有 X）并发搜索，Markdown 输出
eagle search 'AI Agent lang:zh' -c x --hours 24 --sort score --format json
eagle search '露营装备' -c xhs --limit 5 --opt detail=3   # 小红书默认不参与，必须显式 -c xhs
eagle search 'local llm' -c reddit --limit 5              # Reddit 默认不参与，必须显式 -c reddit
eagle search 'MCP' -c reddit --opt subreddit=LocalLLaMA  # 限定 subreddit：优先 Arctic Shift，不耗 Tavily 额度
eagle search 'MCP' -c x,xhs --opt x.pages=2           # 渠道私有参数，可带渠道前缀
```

stdout 只有结果，进度和错误在 stderr；部分渠道失败会在结果里列出原因，全部失败时退出码为 1。

渠道原生命令：

```bash
eagle x topics                          # 列出 X 预设主题（不自带，目录为空时只提示）
eagle x run <主题名或 toml 路径> [--summarize]   # 结果写到 $EAGLE_HOME/runs/x/<主题>/<时间>/（开发模式在仓库 runs/）
eagle x article <推文ID>                 # 抓 X 长文
eagle x search 'from:dotey' --hours 48
eagle x extension [--copy-to DIR] [--open]   # cookie 同步扩展目录
eagle xhs login                         # 小红书扫码登录（原样透传给 eagle-xhs，cwd=$EAGLE_HOME/xhs，默认读 config.yaml）
eagle xhs browser                       # 启动带远程调试端口的专用 Chrome：仅 headless: false 或调试时用，开着时会占用专用 profile
eagle setup [--check]                   # 准备 / 体检
eagle skill install [--target DIR] [--copy]
```

小红书的 `login` 和渠道搜索共用一把浏览器锁（`xhs/data/browser.lock`），同一时刻只有一个能驱动浏览器：
只有在 `login` 进行中、或另一个小红书搜索正在进行时，`eagle search -c xhs` 才会立即返回 `busy`。

### X 预设主题

主题是一份 TOML，把一组长期要搜的 X 查询和本地过滤规则存起来，`eagle x run <名字>` 一次跑完。
放在 `$EAGLE_HOME/x/topics/<名字>.toml`（开发时也可放仓库 `eagle_eye/channels/x/topics/`，同名时前者优先；`eagle x topics` 列出这些主题），也可以直接 `eagle x run path/to/topic.toml`。不自带任何主题。

| 字段 | 默认 | 说明 |
|---|---|---|
| `queries` | 必填 | X 高级搜索查询列表，`since_time` 由程序按时间窗口自动拼上 |
| `description` | `""` | 主题说明，`eagle x topics` 显示，也作报告标题 |
| `window_hours` | `24` | 只看过去 N 小时（`--hours` 可覆盖） |
| `pages_per_query` | `2` | 每条查询翻几页，每页约 20 条 |
| `query_type` | `"Latest"` | `Latest` 或 `Top` |
| `query_suffix` | `"-is:retweet"` | 追加到每条查询末尾 |
| `fetch_articles` | `0` | 为最热的 N 条纯链接推文抓 X 长文正文 |
| `keywords` | `[]` | 本地粗筛：正文/长文至少命中一个（不区分大小写），空表示不筛 |
| `exclude` | `[]` | 命中任一即丢弃 |
| `min_score` | `0` | 最低热度（赞 + 2×转推） |
| `max_results` | `150` | 最多保留条数 |
| `sort` | `"score"` | `score` 或 `time` |
| `prompt` | `""` | `--summarize` 时把 results.md 经 stdin 交给 `summarize_command`，这段作为提示词 |
| `summarize_command` | `["claude", "-p"]` | 生成 digest.md 的命令：results.md 从 stdin 传入，`prompt` 作为最后一个参数；可换成你所用 Agent / LLM 的任何同类 CLI，找不到命令时跳过总结 |

## 渠道代价

| 渠道 | 速度 | 限额 |
|---|---|---|
| X | 几秒一页（约 20 条） | twscrape：按 X 返回的限速头（`x-rate-limit-remaining/reset`）自动锁定并等待，按账号、按接口计，15 分钟窗口重置；具体次数以 X 实际返回为准（社区经验约每 15 分钟几十次，未经实测）。twitterapi.io：免费档 0.2 QPS（1 次 / 5 秒，未充值的新账号；充值/老客户更高） |
| 小红书 | 快速模式（直达搜索结果页、详情页默认 3 个标签并行读取）：估计 `detail=0` 约 5–10 秒、`detail=3` 约 10–20 秒（不含 Chrome 冷启动） | 渠道不限搜索次数，但不模拟真人节奏，风控/封号风险更高（建议小号）；触发风控验证会熔断冷却（默认 6 小时，连续触发翻倍），期间返回 `cooldown` |
| Reddit | 约 1–3 秒（Tavily/Arctic 的网络耗时；本地缓存命中不发请求） | Tavily 免费档约 1,000 信用/月，basic 搜索 1 信用/次，最多 20 条；Arctic Shift 免费但无 SLA；缓存默认 24 小时，命中不消耗额度 |

`TWS_INTERVAL=2`（请求间隔秒）、`TWS_WAIT=60`（所有账号被锁时最多等待秒）、`INTERVAL=5.5`（twitterapi.io 请求间隔）是本项目客户端参数，不是平台限额（见 `eagle_eye/channels/x/settings.py`）。

小红书的耗时是估计值，未经实测验证。可调参数（小红书 config.yaml）：`channel.detail_concurrency`（详情并发，默认 3）、`channel.max_scrolls`、
`channel.timeout_seconds`（默认 120 秒）、`behavior.cooldown_hours`。

小红书风控/封号风险较高，在 `eagle.toml` 里设了 `default = false`：不带 `-c` 的 `eagle search` 不会搜它，必须显式 `-c xhs`（或 `-c x,xhs`）。`eagle channels` 会标明每个渠道是否默认参与。

Reddit 同样默认不参与（第三方 API 有额度和条款约束）：必须显式 `-c reddit`（或 `-c x,reddit`）。
Reddit 参数：`--opt subreddit=LocalLLaMA`、`--opt author=spez` 走 Arctic Shift 搜索并补全字段；`--opt enrich=false`
跳过 Tavily 结果的 Arctic 补全；`--opt cache=false` 跳过本次读缓存（结果仍写回）。`--hours` 与 `--sort time|score`
都在本地过滤/重排；命中缓存时仍按当前时间重新应用 `--hours`。没有结果时返回空 `items` 并在 `meta.notes` 说明。
Arctic 回退/补全失败的结果会在 `meta.degraded=true` 标记，只缓存短 TTL。没有 Tavily key 时普通查询报 `auth_required`
（配置 `EAGLE_REDDIT_TAVILY_API_KEY` 或 `$EAGLE_HOME/reddit/config.toml` 的 `api_key`）；Arctic 查询不需要 key。
Tavily 的 401/403 报 `auth_required`，429/402/432 报 `rate_limited`（默认 60 秒）；Arctic Shift 的 401/403 报 `unavailable`
（它不需要 key，通常是被 Cloudflare 拦截），有 key 时回退 Tavily。连接失败/超时报 `unavailable`，5xx 报 `internal`。

小红书参数：`--opt detail=N`（0–10，默认 3，打开前 N 篇读正文）；`--hours` 有效（发布时间由笔记 ID 推算）；`--sort time|score` 是在综合排序结果内本地重排；关键词没有结果时返回空列表（原因写在 `meta.notes`），不算失败。`cooldown` / `auth_required` 不要自动重试，后者需人工 `eagle xhs login`。

## 给 Agent

任何能执行 shell 命令的 Agent 都能用 `eagle`：stdout 只有结果（`--format json` 便于程序处理），进度和错误在 stderr。
用法说明写在 Skill 文件 `skills/eagle-search/SKILL.md` 里（遵循开放标准 [Agent Skills](https://agentskills.io/specification)，随包附带）。
安装 eagle 不会自动让 Agent 用上它，按你所用的工具选一种接入方式：

**(a) 支持 Agent Skills 的工具：安装 Skill**

```bash
eagle skill install                  # 软链到 ~/.agents/skills/eagle-search（跨工具的用户级约定；升级 eagle-eye 后自动跟着更新）
eagle skill install --agent claude   # Claude Code：装到 ~/.claude/skills/eagle-search
eagle skill install --target DIR     # 其他工具：装到 DIR/eagle-search，DIR 为该工具读取 Skill 的目录（请查阅你所用工具的文档；项目级目录也可以）
eagle skill install --copy           # 改为复制（想自己改 SKILL.md 时；可与上面任一组合）
eagle skill path                     # 打印包内 Skill 目录，也可以自己按工具要求复制/链接
```

目标已存在时不覆盖（先删掉再装）。

**(b) 不支持 Skill 的工具：把说明放进它的指令文件**

```bash
eagle skill show >> AGENTS.md        # 打印 Skill 正文；也可以粘贴到该 Agent 的系统提示词 / 规则文件里
```

**(c) 什么都不装**：直接让 Agent 运行 `eagle --help`、`eagle channels` 自行发现用法（会缺少 Skill 里的代价和错误处理建议）。

## 开发

工程约定、目录和新增渠道步骤见 [AGENTS.md](AGENTS.md)，
架构见 [docs/architecture.md](docs/architecture.md)，渠道协议见 [docs/channel-protocol.md](docs/channel-protocol.md)。

开发模式直接用仓库里的 `bin/eagle`（未设置 `EAGLE_CONFIG` 时用仓库根 `eagle.toml`：小红书用 `bin/eagle-xhs`、`backends/xhs/config.yaml`、`backends/xhs/data/`，
不读 EAGLE_HOME 里的二进制和配置；`eagle x run` 输出在仓库 `runs/`）。上文命令里的 `eagle` 换成 `bin/eagle` 即可；
也可以把 `bin/eagle` 软链到 PATH 里（例如 `ln -s "$PWD/bin/eagle" ~/.local/bin/eagle`，注意别和 `uv tool install` 装的冲突）。

```bash
make venv        # 用 uv 建 .venv 并装 twscrape（bin/eagle 优先用 .venv 的 python）
make build       # 构建小红书渠道 → bin/eagle-xhs（Go 版本见 backends/xhs/go.mod；不在 PATH 时可 make GO=/path/to/go）
make install     # 不发版直接把当前源码装成本机的 eagle（uv tool install --force .），并用本机 Go 构建 eagle-xhs 到 EAGLE_HOME；make uninstall 卸载
cp backends/xhs/config.example.yaml backends/xhs/config.yaml   # 开发模式的小红书配置，再 bin/eagle xhs login
make test        # Python 单测 + Go 单测（都不访问网络）
make test-py     # = python3 -m unittest discover -s tests -t .
make sync-extension EXT_DEST=<浏览器加载的扩展目录>   # = bin/eagle x extension --copy-to EXT_DEST
uv build         # 构建 wheel/sdist 到 dist/（扩展和 Skill 会被打进包内 eagle_eye/_bundled/）
```

测试和 CI 里**绝不要跑真实的小红书渠道搜索**（有封号风险）：用 `tests/fixtures/fake_channel.py` 模拟外部渠道，或只测 describe / bad_query / 占住 browser.lock 时的 busy。
CI（`.github/workflows/ci.yml`）在 ubuntu-latest 和 macos-latest 上跑 Python + Go 单测。
