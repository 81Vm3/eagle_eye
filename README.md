# eagle_eye

给 Agent 用的统一搜索/信息中间层：一条命令并发搜索多个信息渠道（目前是 **X (Twitter)** 和 **小红书**），
结果统一成同一种格式、合并去重，供人阅读（Markdown）或 Agent 处理（JSON）。以后会接更多渠道。

由原 X 搜索（Python）和小红书（Go）两个项目合并而来，只保留通用搜索能力。

## 准备

```bash
make venv     # 用 uv 建 .venv 并装 twscrape（X 渠道默认后端）
make build    # 构建小红书渠道 → bin/eagle-xhs（Go 1.26；不在 PATH 时自动用 /usr/local/go/bin/go。不构建时小红书渠道显示「不可用」，不影响 X）
```

- **X**：在 Edge/Chrome 加载 `eagle_eye/channels/x/chrome-extension/`，登录 x.com，运行 `bin/eagle x login`，扩展会自动推送 cookie。
  已经用过 `x_resource_search` 的不用重新登录（账号库仍是 `~/.config/xsearch/twscrape.db`，`XSEARCH_*` 环境变量继续有效，也可用 `EAGLE_X_*`）。
  也可以改用付费的 twitterapi.io：`export TWITTERAPI_IO_KEY=...`。
- **小红书**：`cp backends/xhs/config.example.yaml backends/xhs/config.yaml`，再 `bin/eagle xhs login` 扫码登录。渠道搜索只接管 `browser.cdp_url` 指向的 Chrome（新开一个标签页工作），没配 `cdp_url` 时报「不可用」。详见 `backends/xhs/README.md`。

## 用法

```bash
bin/eagle channels                                   # 有哪些渠道、是否可用、查询语法、代价
bin/eagle search 'Claude Code' --limit 10            # 默认渠道（目前只有 X）并发搜索，Markdown 输出
bin/eagle search 'Claude Code lang:zh' -c x --hours 24 --sort score --format json
bin/eagle search '露营装备' -c xhs --limit 5 --opt detail=3   # 小红书默认不参与，必须显式 -c xhs
bin/eagle search 'MCP' -c x,xhs --opt x.pages=2       # 渠道私有参数，可带渠道前缀
```

stdout 只有结果，进度和错误在 stderr；部分渠道失败会在结果里列出原因，全部失败时退出码为 1。

渠道原生命令：

```bash
bin/eagle x topics                      # 列出 X 预设主题（仓库不自带，目录为空时只提示）
bin/eagle x run <主题名或 toml 路径> [--summarize]   # 结果写到 runs/x/<主题>/<时间>/
bin/eagle x article <推文ID>             # 抓 X 长文
bin/eagle x search 'from:dotey' --hours 48
bin/eagle xhs login                     # 小红书扫码登录，原样透传给 bin/eagle-xhs（cwd=backends/xhs，默认读 config.yaml）
```

小红书的 `login` 和渠道搜索共用一把浏览器锁（`backends/xhs/data/browser.lock`），同一时刻只有一个能驱动浏览器：
只有在 `login` 进行中、或另一个小红书搜索正在进行时，`eagle search -c xhs` 才会立即返回 `busy`。

### X 预设主题

主题是一份 TOML，把一组长期要搜的 X 查询和本地过滤规则存起来，`eagle x run <名字>` 一次跑完。
放在 `eagle_eye/channels/x/topics/<名字>.toml`（`eagle x topics` 列出这里的主题），也可以直接 `eagle x run path/to/topic.toml`。仓库不自带任何主题。

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
| `summarize_command` | `["claude", "-p"]` | 生成 digest.md 的命令 |

## 渠道代价

| 渠道 | 速度 | 限额 |
|---|---|---|
| X | 几秒一页（约 20 条） | 每个账号约 50 次搜索 / 15 分钟 |
| 小红书 | 单次 1–3 分钟（真实浏览器、拟人节奏，页面加载跨进程节流）；`detail` 每多 1 篇约多 20–30 秒 | 渠道不限搜索次数；触发风控验证会熔断冷却（默认 6 小时，连续触发翻倍），期间返回 `cooldown` |

小红书的耗时是保守的经验估计，未经实测验证；节奏和冷却时长可在 `backends/xhs/config.yaml`（`behavior` 段）调整。

小红书慢、有封号风险，在 `eagle.toml` 里设了 `default = false`：不带 `-c` 的 `eagle search` 不会搜它，必须显式 `-c xhs`（或 `-c x,xhs`）。`eagle channels` 会标明每个渠道是否默认参与。

小红书参数：`--opt detail=N`（0–10，默认 3，打开前 N 篇读正文）；`--hours` 有效（发布时间由笔记 ID 推算）；`--sort time|score` 是在综合排序结果内本地重排。`cooldown` / `auth_required` 不要自动重试，后者需人工 `bin/eagle xhs login`。

## 给 Agent

Claude Code 的 Skill 在 `.claude/skills/eagle-search/SKILL.md`。工程约定、目录和新增渠道步骤见 [AGENTS.md](AGENTS.md)，
架构见 [docs/architecture.md](docs/architecture.md)，渠道协议见 [docs/channel-protocol.md](docs/channel-protocol.md)。

## 开发

```bash
make test        # Python 单测 + Go 单测（都不访问网络）
make test-py     # = python3 -m unittest discover -s tests -t .
```

测试和 CI 里**绝不要跑真实的小红书渠道搜索**（有封号风险）：用 `tests/fixtures/fake_channel.py` 模拟外部渠道，或只测 describe / bad_query / 占住 browser.lock 时的 busy。
